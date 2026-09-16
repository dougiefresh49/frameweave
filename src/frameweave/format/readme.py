"""README generator: renders the trust-table README from the sidecar facts.

Reads ``meta.json`` and ``cost.json`` as plain dicts (issue #12 defines their
shape and writes them; this module never imports the writer) and the
transcript file as text. Never reads a frame image or calls a model. Section
order and content follow ``docs/PLAN.md`` section 3, "Sidecars and README".

``frameweave.config`` (``FlagSpec``, ``Check``, ``Config``) is issue #4's
module. It had not merged into this worktree's base when this file was
written, so ``cli_flags`` and ``preflight_checks`` import it lazily, inside
the function body, instead of at module load time: importing this module
never requires ``frameweave.config`` to exist, only calling those two seams
does.
"""

from __future__ import annotations

import json
import re
import tomllib
from pathlib import Path
from typing import TYPE_CHECKING, Any

from frameweave.util import timecode

if TYPE_CHECKING:
    from frameweave.config import Check, Config, FlagSpec

_MANGLES_PATH = Path(__file__).parent / "mangles.toml"
_UNKNOWN = "unknown"

_SAID_RE = re.compile(
    r"^\[(?P<start>[\d:.]+)(?:-(?P<end>[\d:.]+))?\] said/(?P<tag>[^ :]+): (?P<payload>.*)$"
)
_SPEAKER_RE = re.compile(r"^(?P<speaker>S\d+): ")

_CONTENTS_DESCRIPTIONS: tuple[tuple[str, str], ...] = (
    ("frames/", "the extracted frame images the transcript cites"),
    ("meta.json", "resolved facts, description, links, chapters, and completion status"),
    ("cost.json", "the request ledger summary: tokens and dollars per stage"),
    ("README.md", "this file"),
)


def render(
    dest_dir: Path,
    transcript: Path,
    meta: dict[str, Any],
    cost: dict[str, Any],
    glossary: dict[str, Any] | None = None,
) -> str:
    """Render the README body as a string. Pure: no filesystem writes."""
    text = transcript.read_text(encoding="utf-8")
    sections = [
        _title_section(meta),
        _source_section(meta),
        _start_here_section(),
        _contents_section(transcript.name),
        _format_legend_section(),
        _trust_table_section(meta),
    ]
    if meta.get("speakers"):
        sections.append(_speakers_section(meta, text))
    sections.append(_mangles_section(text, glossary))
    sections.append(_limits_section(meta))
    sections.append(_provenance_section(meta, cost))
    return "\n\n".join(sections) + "\n"


def write_readme(
    dest_dir: Path,
    transcript: Path,
    meta: dict[str, Any],
    cost: dict[str, Any],
    glossary: dict[str, Any] | None = None,
) -> Path:
    """Render and publish ``README.md`` under ``dest_dir``: write temp, then rename."""
    content = render(dest_dir, transcript, meta, cost, glossary)
    dest = dest_dir / "README.md"
    tmp = dest_dir / ".README.md.tmp"
    tmp.write_text(content, encoding="utf-8")
    tmp.replace(dest)
    return dest


# --- field access: every field is optional; missing or None renders "unknown" ---


def _field(d: dict[str, Any] | None, key: str, default: Any = _UNKNOWN) -> Any:
    if not isinstance(d, dict):
        return default
    value = d.get(key)
    return default if value is None else value


def _sub(d: dict[str, Any] | None, key: str) -> dict[str, Any]:
    value = d.get(key) if isinstance(d, dict) else None
    return value if isinstance(value, dict) else {}


# --- sections, in the order docs/PLAN.md section 3 defines ---


def _title_section(meta: dict[str, Any]) -> str:
    channel = _field(meta, "channel")
    title = _field(meta, "title")
    heading = f'# {channel} — "{title}"'
    summary = (
        "This folder is one frameweave run: a plain-text transcript that interleaves "
        "what was said with what was on screen, the frames it cites, and the sidecars "
        "that back every claim in it."
    )
    return f"{heading}\n\n{summary}"


def _format_duration(value: Any) -> Any:
    """Numeric duration (seconds) as ``HH:MM:SS``; strings and other values pass through."""
    if isinstance(value, bool):
        return value
    if isinstance(value, int | float):
        return timecode.format(float(value), tenths=False)
    return value


def _source_section(meta: dict[str, Any]) -> str:
    lines = [
        f"- Source: {_field(meta, 'source')}",
        f"- Duration: {_format_duration(_field(meta, 'duration'))}",
        f"- Published: {_field(meta, 'published')}",
        f"- Range: {_field(meta, 'range')}",
        f"- Analyzed: {_field(meta, 'generated_at')}",
    ]
    return "## Source\n\n" + "\n".join(lines)


def _start_here_section() -> str:
    lines = [
        "1. Read `transcript.fwv`. It is plain text.",
        "2. Run `grep '^## '` on it for the video map.",
        "3. Open a frame image under `frames/` when a `text:` string matters.",
    ]
    return "## Start here\n\n" + "\n".join(lines)


def _contents_section(transcript_name: str) -> str:
    rows = [
        (transcript_name, "the transcript: speech, frames, and on-screen text in one file"),
        *_CONTENTS_DESCRIPTIONS,
    ]
    header = "| file | what it is |\n|---|---|"
    body = "\n".join(f"| `{name}` | {desc} |" for name, desc in rows)
    return "## Contents\n\n" + header + "\n" + body


def _format_legend_section() -> str:
    lines = [
        "- `said/<source>`: what was spoken, verbatim from its source tag.",
        "  The tag names the source: a caption track, automatic captions, or speech-to-text.",
        "- `seen/<lane>:<model>`: a primary frame, described by the vision model in one sentence.",
        "  Taken at the start of each speech segment, and in silent stretches no segment covers.",
        "- `seen+/<lane>:<model>`: an extra frame sampled inside a segment that already has "
        "its primary frame.",
        "  A reader that wants one frame per segment skips these lines.",
        "- `text:`: on-screen strings the model read, quoted exactly as read, or `illegible`.",
        "  Follows a `seen` or `seen+` line as an indented continuation.",
        "",
        "The full grammar, including the association rule and the additive rule, is in "
        "`docs/format.md`.",
    ]
    return "## Format legend\n\n" + "\n".join(lines)


def _trust_table_section(meta: dict[str, Any]) -> str:
    rows = [
        ("`said`", "verbatim from its source, named on each line (a caption track, "
         "automatic captions, or speech-to-text)"),
        ("`seen`", "model-described: one sentence from the vision model, not verbatim"),
        ("`text`", "model-read: verify numbers and identifiers against the frame image "
         "before reusing them"),
        ("frames", "ground truth: the image itself"),
    ]
    if meta.get("speakers"):
        rows.append(
            ("speakers", "model-assigned labels, listed below with each label's "
             "first-utterance time")
        )
    header = "| kind | trust |\n|---|---|"
    body = "\n".join(f"| {kind} | {trust} |" for kind, trust in rows)
    return "## Trust table\n\n" + header + "\n" + body


def _speakers_section(meta: dict[str, Any], transcript_text: str) -> str:
    first_times = _first_speaker_times(transcript_text)
    try:
        count = int(_field(meta, "speakers", default=0))
    except (TypeError, ValueError):
        count = 0
    total = max(count, len(first_times))
    if total == 0:
        return "## Speakers\n\nNo speaker labels found in the transcript."
    lines = []
    for i in range(1, total + 1):
        label = f"S{i}"
        if label in first_times:
            lines.append(f"- `{label}`: first line at {first_times[label]}")
        else:
            lines.append(f"- `{label}`: not found in the transcript")
    return "## Speakers\n\n" + "\n".join(lines)


def _first_speaker_times(transcript_text: str) -> dict[str, str]:
    times: dict[str, str] = {}
    for line in transcript_text.splitlines():
        match = _SAID_RE.match(line)
        if not match:
            continue
        speaker_match = _SPEAKER_RE.match(match.group("payload"))
        if not speaker_match:
            continue
        label = speaker_match.group("speaker")
        times.setdefault(label, match.group("start"))
    return times


def _load_mangles() -> list[dict[str, str]]:
    data = tomllib.loads(_MANGLES_PATH.read_text(encoding="utf-8"))
    return data.get("mangle", [])


def _occurs(heard: str, text: str) -> bool:
    pattern = re.compile(r"\b" + re.escape(heard) + r"\b", re.IGNORECASE)
    return pattern.search(text) is not None


def _mangles_section(transcript_text: str, glossary: dict[str, Any] | None) -> str:
    rows = [
        (m["heard"], m["meant"], m["context"])
        for m in _load_mangles()
        if _occurs(m["heard"], transcript_text)
    ]
    if glossary:
        entries = glossary.get("entries", [])
        if isinstance(entries, list):
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                rows.append(
                    (
                        entry.get("heard", _UNKNOWN),
                        entry.get("meant", _UNKNOWN),
                        entry.get("context", _UNKNOWN),
                    )
                )
    if not rows:
        body = "None of the seeded caption mangles occur in this transcript."
    else:
        header = "| heard | meant | context |\n|---|---|---|"
        body = header + "\n" + "\n".join(f"| {h} | {m} | {c} |" for h, m, c in rows)
    return "## Caption mangles\n\n" + body


def _limits_section(meta: dict[str, Any]) -> str:
    frames = _sub(meta, "frames")
    stats = _sub(meta, "stats")
    lines = [
        f"- Frames: {_field(frames, 'total')} total ({_field(frames, 'primary')} primary, "
        f"{_field(frames, 'extra')} extra) at {_field(frames, 'width')}px. Extra frames "
        f"sample the interval gap inside a segment; vision: {_field(meta, 'vision')}."
    ]
    windows = _field(stats, "windows", default=0)
    if isinstance(windows, int | float) and windows > 0:
        lines.append(
            f"- Segments were grouped into {windows} windows because frames exceeded "
            "the per-call budget."
        )
    lines.append(
        "- Transcript source weaknesses: automatic captions mis-hear names and code; "
        "speech-to-text has no punctuation guarantee."
    )
    completion = _field(meta, "completion")
    reason = _field(meta, "completion_reason", default=None)
    if reason:
        lines.append(f"- Completion: {completion} ({reason}).")
    else:
        lines.append(f"- Completion: {completion}.")
    warnings = _field(meta, "warnings", default=[])
    if isinstance(warnings, list) and warnings:
        lines.append("- Warnings:")
        lines.extend(f"  - {w}" for w in warnings)
    else:
        lines.append("- Warnings: none.")
    return "## Limits\n\n" + "\n".join(lines)


def _provenance_section(meta: dict[str, Any], cost: dict[str, Any]) -> str:
    lines = [
        f"- Tool version: {_field(meta, 'tool_version')}",
        f"- Generated at: {_field(meta, 'generated_at')}",
    ]
    stages = cost.get("stages") if isinstance(cost, dict) else None
    if isinstance(stages, list) and stages:
        lines.append("- Stages:")
        for stage in stages:
            name = _field(stage, "stage")
            provider = _field(stage, "provider")
            model = _field(stage, "model")
            lines.append(f"  - {name}: {provider}/{model}")
    else:
        lines.append(f"- Stages: {_UNKNOWN}")
    total_usd = _field(cost, "total_usd")
    if total_usd == _UNKNOWN:
        cost_line = f"- Total cost: {_UNKNOWN}"
    elif isinstance(total_usd, int | float) and not isinstance(total_usd, bool):
        cost_line = f"- Total cost: ${float(total_usd):.2f}"
    else:
        cost_line = f"- Total cost: ${total_usd}"
    unknown_usd = _field(cost, "unknown_usd", default=0)
    if isinstance(unknown_usd, int | float) and unknown_usd:
        cost_line += (
            f" (${unknown_usd} of this is unknown: timed-out requests with no usage returned)"
        )
    lines.append(cost_line)
    lines.append(f"- Command line: `{_field(meta, 'command_line')}`")
    return "## Provenance\n\n" + "\n".join(lines)


# --- seams the CLI and doctor collect ---


def cli_flags() -> list[FlagSpec]:
    from frameweave.config import FlagSpec

    return [
        FlagSpec(
            name="--glossary",
            dest="glossary",
            type=Path,
            help="path to a JSON glossary file of extra caption mangles for the README",
            default=None,
        )
    ]


def preflight_checks(config: Config) -> list[Check]:
    from frameweave.config import Check

    glossary = getattr(config, "glossary", None)
    if not glossary:
        return []
    remedy = 'expected shape: {"entries": [{"heard": str, "meant": str, "context": str}, ...]}'
    path = Path(glossary)
    if not path.exists():
        return [Check(name="glossary", ok=False, detail=f"{path} does not exist", remedy=remedy)]
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return [
            Check(name="glossary", ok=False, detail=f"{path} did not parse: {exc}", remedy=remedy)
        ]
    entries = data.get("entries") if isinstance(data, dict) else None
    ok = (
        isinstance(data, dict)
        and isinstance(entries, list)
        and all(isinstance(entry, dict) for entry in entries)
    )
    if ok:
        detail = f"{path} parses"
    elif not isinstance(entries, list):
        detail = f"{path} is missing an 'entries' list"
    else:
        detail = f"{path} has a non-dict entry in 'entries'"
    return [Check(name="glossary", ok=ok, detail=detail, remedy=remedy)]
