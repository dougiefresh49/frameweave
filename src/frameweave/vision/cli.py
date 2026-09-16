"""Subscription-backed vision lanes through the owner's logged-in CLIs."""

from __future__ import annotations

import json
import re
import subprocess
import tempfile
import time
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from frameweave.types import Description, Frame, Usage
from frameweave.util.retry import (
    Outcome,
    RequestFailed,
    RequestTimeout,
    RetryExhausted,
    call,
)
from frameweave.vision.base import (
    BadReply,
    VisionFailed,
    failure_message,
    parse_json_list,
    render_prompt,
    validate,
)

if TYPE_CHECKING:
    from frameweave.config import Config

Runner = Callable[..., subprocess.CompletedProcess[str]]
Lane = Literal["claude", "codex"]

_QUOTA = re.compile(r"rate[ -]?limit|quota|\b429\b|overloaded", re.IGNORECASE)
_RETRY_AFTER = re.compile(r"retry-after\s*:?\s*(\d+(?:\.\d+)?)", re.IGNORECASE)
_TOKENS = re.compile(r"tokens used\s*\n\s*([\d,]+)", re.IGNORECASE)
_mcp_config_path: Path | None = None


class CliBackend:
    """One adapter for Claude Code and Codex CLI vision requests."""

    def __init__(
        self,
        lane: Lane,
        model: str,
        *,
        runner: Runner = subprocess.run,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if lane not in {"claude", "codex"}:
            raise ValueError(f"unsupported CLI vision lane: {lane}")
        if lane == "codex" and not model:
            raise ValueError("the codex vision lane requires an explicit model")
        self.name = lane
        self.model = model
        self._runner = runner
        self._sleep = sleep
        self._clock = clock

    def describe(
        self,
        batch: list[Frame],
        context: str,
        frames_dir: Path,
        config: Config,
    ) -> tuple[list[Description], Usage]:
        if not batch:
            return [], Usage()
        paths = [_frame_path(frame, frames_dir) for frame in batch]
        prompt = render_prompt(len(batch), context)
        argv = self._argv(paths, prompt, frames_dir, config)
        bad_replies = 0
        calls = 0
        spent = Usage()
        last_detail = "request failed"
        started = self._clock()

        def invoke() -> tuple[subprocess.CompletedProcess[str], list[Description], Usage]:
            nonlocal calls, spent
            calls += 1
            result = self._runner(
                argv,
                cwd=frames_dir,
                capture_output=True,
                text=True,
                stdin=subprocess.DEVNULL,
                timeout=config.timeout_s,
                check=False,
            )
            if result.returncode != 0:
                return result, [], Usage()
            reply_text, usage = self._extract_reply(result)
            spent = spent + usage
            descriptions = validate(
                batch, parse_json_list(reply_text), source=f"{self.name}:{self.model}"
            )
            return result, descriptions, spent

        def classify(value: object) -> Outcome:
            nonlocal bad_replies, last_detail
            if isinstance(value, subprocess.TimeoutExpired):
                last_detail = str(value) or "timed out"
                return Outcome("timeout")
            if isinstance(value, BadReply):
                last_detail = str(value) or "bad reply"
                bad_replies += 1
                return Outcome("retry" if bad_replies == 1 else "fail")
            if isinstance(value, Exception):
                last_detail = str(value) or value.__class__.__name__
                return Outcome("fail")
            result = value[0] if isinstance(value, tuple) else value
            if not isinstance(result, subprocess.CompletedProcess):
                last_detail = "unexpected vision response"
                return Outcome("fail")
            if result.returncode == 0:
                return Outcome("ok")
            output = f"{result.stdout or ''}\n{result.stderr or ''}"
            last_detail = (result.stderr or result.stdout or f"exit {result.returncode}").strip()
            if _QUOTA.search(output):
                retry_after = _retry_after(output)
                return Outcome("retry", retry_after=retry_after)
            return Outcome("fail")

        try:
            _, descriptions, usage = call(
                invoke,
                attempts=2,
                timeout_s=config.timeout_s,
                classify=classify,
                sleep=self._sleep,
                frames_per_call=config.frames_per_call,
            )
        except (RequestFailed, RequestTimeout, RetryExhausted) as exc:
            raise VisionFailed(failure_message(self.name, last_detail, config)) from exc

        elapsed = self._clock() - started
        return descriptions, Usage(
            calls=calls,
            tokens_in=usage.tokens_in,
            tokens_out=usage.tokens_out,
            tokens_reasoning=usage.tokens_reasoning,
            seconds=elapsed,
            usd=0.0,
        )

    def _argv(
        self, paths: list[Path], prompt: str, frames_dir: Path, config: Config
    ) -> list[str]:
        if self.name == "codex":
            effort = getattr(config, "vision_effort", "low")
            argv = [
                "codex",
                "exec",
                "--skip-git-repo-check",
                "--sandbox",
                "read-only",
                "-m",
                self.model,
                "-c",
                f'model_reasoning_effort="{effort}"',
            ]
            for path in paths:
                argv.extend(("-i", str(path)))
            return [*argv, "--", prompt]

        mcp_config = _ensure_empty_mcp_config()
        read_request = "Read these image file(s) with the Read tool, in order:\n"
        read_request += "\n".join(str(path) for path in paths)
        read_request += f"\n\nThen: {prompt}"
        return [
            "claude",
            "-p",
            "--model",
            self.model,
            "--allowedTools",
            "Read",
            "--tools",
            "Read",
            "--output-format",
            "json",
            "--strict-mcp-config",
            "--mcp-config",
            str(mcp_config),
            "--disable-slash-commands",
            "--setting-sources",
            "",
            "--add-dir",
            str(frames_dir.resolve()),
            "--system-prompt",
            "You read image files with the Read tool and answer with JSON only.",
            read_request,
        ]

    def _extract_reply(
        self, result: subprocess.CompletedProcess[str]
    ) -> tuple[str, Usage]:
        """Return reply text and usage; accumulate usage before JSON validation."""
        if self.name == "claude":
            try:
                envelope = json.loads(result.stdout)
            except (json.JSONDecodeError, TypeError) as exc:
                raise BadReply("Claude CLI did not return its JSON envelope") from exc
            if not isinstance(envelope, dict) or not isinstance(envelope.get("result"), str):
                raise BadReply("Claude CLI JSON envelope has no result text")
            provider_usage = envelope.get("usage", {})
            if not isinstance(provider_usage, dict):
                provider_usage = {}
            tokens_in = sum(
                _integer(provider_usage.get(key))
                for key in (
                    "input_tokens",
                    "cache_read_input_tokens",
                    "cache_creation_input_tokens",
                )
            )
            usage = Usage(
                tokens_in=tokens_in,
                tokens_out=_integer(provider_usage.get("output_tokens")),
            )
            return envelope["result"], usage

        output = f"{result.stderr or ''}\n{result.stdout or ''}"
        match = _TOKENS.search(output)
        tokens = int(match.group(1).replace(",", "")) if match else 0
        return result.stdout or "", Usage(tokens_in=tokens)


def _frame_path(frame: Frame, frames_dir: Path) -> Path:
    path = Path(frame.path)
    if path.is_absolute():
        return path
    return (frames_dir / path.name).resolve()


def _ensure_empty_mcp_config() -> Path:
    """Write the empty MCP config once under a scratch temp dir (not the frames folder)."""
    global _mcp_config_path
    if _mcp_config_path is not None and _mcp_config_path.exists():
        return _mcp_config_path
    handle = tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        prefix="frameweave-empty-mcp-",
        suffix=".json",
        delete=False,
    )
    with handle:
        handle.write('{"mcpServers": {}}\n')
    _mcp_config_path = Path(handle.name)
    return _mcp_config_path


def _retry_after(output: str) -> float | None:
    match = _RETRY_AFTER.search(output)
    return float(match.group(1)) if match else None


def _integer(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0
