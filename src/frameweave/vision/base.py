"""Shared vision contract, prompt, response validation, and discovery seams."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

from frameweave.types import Description, Frame, Usage

if TYPE_CHECKING:
    from frameweave.config import Check, Config, FlagSpec

prompt_revision = "1"
PROMPT_TEMPLATE = Path(__file__).with_name("prompt.md").read_text(encoding="utf-8").strip()


class BadReply(Exception):
    """A provider answered, but its reply cannot safely be associated with the frames."""


class VisionFailed(Exception):
    """A vision request failed after applying the retry policy."""


class VisionBackend(Protocol):
    name: str
    model: str

    def describe(
        self,
        batch: list[Frame],
        context: str,
        frames_dir: Path,
        config: Config,
    ) -> tuple[list[Description], Usage]: ...


def render_prompt(n: int, context: str) -> str:
    """Fill the single versioned prompt used by every vision lane."""
    return PROMPT_TEMPLATE.format(n=n, context=context)


def parse_json_list(text: str) -> list[object]:
    """Extract the first JSON list from a reply that may contain CLI status prose."""
    decoder = json.JSONDecoder()
    for index, character in enumerate(text):
        if character != "[":
            continue
        try:
            value, _ = decoder.raw_decode(text[index:])
        except json.JSONDecodeError:
            continue
        if isinstance(value, list):
            return value
    raise BadReply("reply did not contain a JSON list")


def validate(
    batch: list[Frame], parsed: object, *, source: str = ""
) -> list[Description]:
    """Validate ordered frame associations and convert provider JSON to records."""
    if not isinstance(parsed, list) or len(parsed) != len(batch):
        raise BadReply(f"expected one result for each of {len(batch)} frames")

    descriptions: list[Description] = []
    for expected, (frame, item) in enumerate(zip(batch, parsed, strict=True), start=1):
        if not isinstance(item, dict):
            raise BadReply(f"frame {expected} result is not an object")
        index = item.get("frame")
        if isinstance(index, bool) or index != expected:
            raise BadReply(f"expected frame indices 1..{len(batch)} in order")

        summary = item.get("description")
        strings = item.get("text")
        illegible = item.get("illegible", False)
        if not isinstance(summary, str):
            raise BadReply(f"frame {expected} description is not a string")
        if not isinstance(strings, list) or not all(isinstance(value, str) for value in strings):
            raise BadReply(f"frame {expected} text is not a list of strings")
        if not isinstance(illegible, bool):
            raise BadReply(f"frame {expected} illegible is not a boolean")

        descriptions.append(
            Description(
                frame_id=frame.id,
                source=source,
                summary=summary.strip(),
                strings=[_collapse_whitespace(value) for value in strings],
                illegible=illegible,
            )
        )
    return descriptions


def make_backend(lane: str, config: Config) -> VisionBackend:
    """Construct one explicitly named lane; automatic lane selection belongs to issue #26."""
    model = config.vision_model.get(lane, "")
    if lane in {"claude", "codex"}:
        from frameweave.vision.cli import CliBackend

        return CliBackend(lane, model)
    if lane == "gemini":
        from frameweave.vision.gemini import GeminiBackend

        return GeminiBackend(model)
    if lane == "none":
        return NoneBackend()
    raise ValueError(f"unknown vision lane: {lane}")


class NoneBackend:
    """The local-only opt-out: no frames leave the machine."""

    name = "none"
    model = ""

    def describe(
        self,
        batch: list[Frame],
        context: str,
        frames_dir: Path,
        config: Config,
    ) -> tuple[list[Description], Usage]:
        return [], Usage()


def cli_flags() -> list[FlagSpec]:
    """Flags owned by the vision stage, collected by the top-level CLI later."""
    from frameweave.config import FlagSpec

    return [
        FlagSpec(
            "--vision",
            "vision_lane",
            _choice("vision", ("auto", "claude", "codex", "gemini", "none")),
            "Vision lane: auto, claude, codex, gemini, or none.",
        ),
        FlagSpec(
            "--vision-quality",
            "vision_quality",
            _choice("vision quality", ("standard", "high")),
            "Vision detail: standard or high.",
        ),
        FlagSpec("--frames-per-call", "frames_per_call", int, "Frames sent in one vision call."),
    ]


def preflight_checks(config: Config) -> list[Check]:
    """Report CLI versions and whether the metered lane has its API key."""
    from frameweave.config import Check

    checks = [
        _binary_check("claude", config.vision_lane == "claude", Check),
        _binary_check("codex", config.vision_lane == "codex", Check),
    ]
    has_key = bool(os.environ.get("GEMINI_API_KEY"))
    checks.append(
        Check(
            "GEMINI_API_KEY",
            has_key,
            "GEMINI_API_KEY is set" if has_key else "GEMINI_API_KEY is not set",
            "Export GEMINI_API_KEY before using --vision gemini.",
            required=config.vision_lane == "gemini",
        )
    )
    return checks


def failure_message(lane: str, detail: str, config: Config) -> str:
    """Turn every terminal provider failure into the same actionable message."""
    return (
        f"{lane} vision failed: {detail}\n"
        f"--timeout <seconds> (raise the per-call timeout, currently {_number(config.timeout_s)})\n"
        "--vision-quality low (send smaller frames)\n"
        "--frames-per-call <n> (send fewer frames per request, currently "
        f"{_number(config.frames_per_call)})"
    )


def _binary_check(name: str, required: bool, check_type: type[Check]) -> Check:
    path = shutil.which(name)
    remedy = f"Install {name} and ensure it is on PATH before using --vision {name}."
    if path is None:
        return check_type(name, False, f"{name} is not on PATH", remedy, required=required)
    try:
        result = subprocess.run(
            [path, "--version"], capture_output=True, text=True, timeout=10, check=False
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return check_type(
            name,
            False,
            f"could not read {name} version: {exc}",
            remedy,
            required=required,
        )
    output = (result.stdout or result.stderr).strip().splitlines()
    detail = output[0] if output else f"{name} --version exited {result.returncode}"
    return check_type(name, result.returncode == 0, detail, remedy, required=required)


def _choice(label: str, choices: tuple[str, ...]) -> Any:
    def parse(value: str) -> str:
        if value not in choices:
            options = ", ".join(choices)
            raise argparse.ArgumentTypeError(f"{label} must be one of: {options}")
        return value

    return parse


def _collapse_whitespace(value: str) -> str:
    return " ".join(value.split())


def _number(value: float | int) -> str:
    return str(int(value)) if int(value) == value else str(value)
