"""Transcript writer and JSON sidecars.

Atomic publish: each artifact is written to a temp name in ``dest_dir`` and renamed
into place. Paths cited in the transcript and ``meta.json`` are relative to ``dest_dir``.

Call order for completion: callers that need ``incomplete`` (speech requested, zero
segments) set ``meta.completion`` / ``meta.completion_reason`` via
``derive_completion`` before ``write_transcript``. This function appends any
missing-description warnings, assigns missing ``Segment.id`` values, then calls
``derive_completion`` again so those warnings are reflected without clobbering an
already-recorded incomplete run (``speech_requested`` is inferred from a prior
``incomplete`` status).
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from frameweave.types import (
    FORMAT_VERSION,
    TRANSCRIPT_NAME,
    Description,
    Frame,
    Resolved,
    Segment,
)
from frameweave.util.timecode import format as format_time
from frameweave.util.timecode import parse as parse_time

_KIND_RANK = {"said": 0, "seen": 1, "seen+": 2}
_EVENT_HEAD = re.compile(
    r"^\[(?P<time>\d+:\d{2}:\d{2}(?:\.\d+)?)"
    r"(?:-(?P<end>\d+:\d{2}:\d{2}(?:\.\d+)?))?\] "
    r"(?P<kind>[^\s/]+)/"
)

INCOMPLETE_NO_SPEECH = "no speech"
MISSING_DESCRIPTION = "(no description)"


@dataclass
class RunMeta:
    """Per-run facts the writer does not derive from stage artifacts.

    ``tool_version`` and ``generated_at`` are fields, never read from the package
    or from the clock. ``vision`` is ``<lane>:<model> <quality>, <n> per call`` or
    ``none``.
    """

    range_spec: str
    transcript_source: str
    speakers: int | None
    vision: str
    frame_width: int
    completion: str
    completion_reason: str | None
    warnings: list[str]
    stats: dict[str, Any]
    generated_at: str
    tool_version: str
    caption_track: str | None
    command_line: str


def derive_completion(
    segments: list[Segment],
    warnings: list[str],
    speech_requested: bool,
) -> tuple[str, str | None]:
    """Return ``(completion, reason)``.

    ``incomplete`` with ``no speech`` when speech was requested and there are
    zero segments; ``complete-with-warnings`` when ``warnings`` is non-empty;
    otherwise ``complete``.
    """
    if speech_requested and not segments:
        return "incomplete", INCOMPLETE_NO_SPEECH
    if warnings:
        return "complete-with-warnings", None
    return "complete", None


def cli_flags() -> list:
    """CLI flags this module contributes. None yet; config lands in issue #4."""
    return []


def preflight_checks(config: object) -> list:
    """Doctor checks this module contributes. None."""
    return []


def write_transcript(
    dest_dir: Path,
    resolved: Resolved,
    segments: list[Segment],
    frames: list[Frame],
    descriptions: list[Description],
    meta: RunMeta,
    extra_events: list[str] | None = None,
    extra_headers: dict[str, str] | None = None,
) -> Path:
    """Write ``<dest_dir>/transcript.fwv`` and return its path.

    ``extra_events`` are raw body lines (a ``note/dedupe`` line, later #22) inserted
    in time order. ``extra_headers`` are appended after the documented header keys.
    A frame with no description renders summary ``(no description)``, an empty
    ``text:`` line, and appends a warning on ``meta``.

    Assigns missing ``Segment.id`` values as ``s<dddd>`` in list order (mutates the
    caller's list). See module docstring for completion call order.
    """
    dest_dir.mkdir(parents=True, exist_ok=True)
    _assign_segment_ids(segments)
    by_id = {d.frame_id: d for d in descriptions}
    for frame in frames:
        if frame.id not in by_id:
            warning = f"frame {frame.id} has no description"
            if warning not in meta.warnings:
                meta.warnings.append(warning)
    speech_requested = meta.completion == "incomplete"
    meta.completion, meta.completion_reason = derive_completion(
        segments, meta.warnings, speech_requested
    )

    header = _header_lines(resolved, frames, meta, extra_headers or {})
    body = _body_lines(
        resolved,
        segments,
        frames,
        by_id,
        dest_dir,
        extra_events or [],
        fallback_tag=_vision_tag(meta.vision),
    )
    text = "\n".join([*header, "", *body]) + "\n"
    path = dest_dir / TRANSCRIPT_NAME
    _atomic_write(path, text)
    return path


def write_meta(
    dest_dir: Path,
    resolved: Resolved,
    frames: list[Frame],
    meta: RunMeta,
) -> Path:
    """Write ``<dest_dir>/meta.json``: ``Resolved.to_dict()`` plus run fields."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    primary = sum(1 for f in frames if f.kind == "primary")
    extra = sum(1 for f in frames if f.kind == "extra")
    data: dict[str, Any] = resolved.to_dict()
    data.update(
        {
            "range": meta.range_spec,
            "transcript_source": meta.transcript_source,
            "caption_track": meta.caption_track,
            "speakers": meta.speakers,
            "vision": meta.vision,
            "frames": {
                "total": len(frames),
                "primary": primary,
                "extra": extra,
                "width": meta.frame_width,
                # assumed: relocation helpers want an explicit file list; not in format.md
                "files": [_relative_path(f.path, dest_dir) for f in frames],
            },
            "completion": meta.completion,
            "completion_reason": meta.completion_reason,
            "warnings": list(meta.warnings),
            "stats": dict(meta.stats),
            "generated_at": meta.generated_at,
            "tool_version": meta.tool_version,
            "command_line": meta.command_line,
            "format_version": FORMAT_VERSION,
        }
    )
    path = dest_dir / "meta.json"
    _atomic_write(path, json.dumps(data, indent=2, ensure_ascii=False) + "\n")
    return path


def write_cost(dest_dir: Path, ledger_summary: dict) -> Path:
    """Write ``<dest_dir>/cost.json`` from the pipeline's ledger summary dict.

    Schema (issue #14 computes the numbers; this function serializes):

    ``stages``: list of objects, each with ``provider``, ``model``, ``tokens_in``,
    ``tokens_out``, ``tokens_reasoning``, ``seconds``, ``usd``.

    Totals: ``current_run_usd``, ``reused_usd``, ``unknown_usd``, ``total_usd``,
    ``rates_dated``.
    """
    dest_dir.mkdir(parents=True, exist_ok=True)
    path = dest_dir / "cost.json"
    _atomic_write(path, json.dumps(ledger_summary, indent=2, ensure_ascii=False) + "\n")
    return path


def _assign_segment_ids(segments: list[Segment]) -> None:
    """Fill missing ``Segment.id`` as ``s<dddd>`` in file order; leave existing ids."""
    for index, seg in enumerate(segments, start=1):
        if seg.id is None:
            segments[index - 1] = replace(seg, id=f"s{index:04d}")


def _header_lines(
    resolved: Resolved,
    frames: list[Frame],
    meta: RunMeta,
    extra_headers: dict[str, str],
) -> list[str]:
    primary = sum(1 for f in frames if f.kind == "primary")
    extra = sum(1 for f in frames if f.kind == "extra")
    completion = meta.completion
    if completion == "incomplete" and meta.completion_reason:
        completion = f"incomplete: {meta.completion_reason}"
    lines = [
        f"frameweave {FORMAT_VERSION}",
        f"title: {resolved.title}",
        f"channel: {resolved.channel}",
        f"source: {resolved.source}",
        f"duration: {format_time(resolved.duration, tenths=False)}",
        f"range: {meta.range_spec}",
        f"transcript-source: {meta.transcript_source}",
    ]
    if meta.speakers is not None:
        lines.append(f"speakers: {meta.speakers}")
    lines.extend(
        [
            f"vision: {meta.vision}",
            f"frames: {len(frames)} primary {primary} extra {extra} at {meta.frame_width}px",
            f"completion: {completion}",
            f"generated: {meta.generated_at} frameweave {meta.tool_version}",
        ]
    )
    if resolved.chapters:
        rendered = "; ".join(f"{_clock(ch.start)} {ch.title}" for ch in resolved.chapters)
        lines.append(f"chapters: {rendered}")
    for key, value in extra_headers.items():
        lines.append(f"{key}: {value}")
    return lines


def _body_lines(
    resolved: Resolved,
    segments: list[Segment],
    frames: list[Frame],
    by_id: dict[str, Description],
    dest_dir: Path,
    extra_events: list[str],
    fallback_tag: str,
) -> list[str]:
    events: list[tuple[float, int, list[str]]] = []
    for seg in segments:
        events.append((seg.start, _KIND_RANK["said"], [_said_line(seg)]))
    for frame in frames:
        desc = by_id.get(frame.id)
        kind = "seen" if frame.kind == "primary" else "seen+"
        events.append(
            (
                frame.time,
                _KIND_RANK[kind],
                _seen_lines(frame, desc, dest_dir, fallback_tag),
            )
        )
    for raw in extra_events:
        line = raw.rstrip("\n")
        time, kind = _extra_event_key(line)
        events.append((time, _KIND_RANK.get(kind, 3), [line]))
    events.sort(key=lambda item: (item[0], item[1]))

    items: list[tuple[str, Any]] = []
    pending = list(resolved.chapters)
    for time, _rank, lines in events:
        while pending and pending[0].start <= time:
            items.append(("chapter", pending.pop(0)))
        items.append(("event", lines))
    while pending:
        items.append(("chapter", pending.pop(0)))

    out: list[str] = []
    for kind, payload in items:
        if kind == "chapter":
            if out and out[-1] != "":
                out.append("")
            out.append(f"## [{_clock(payload.start)}] {payload.title}")
            out.append("")
        else:
            out.extend(payload)
    while out and out[-1] == "":
        out.pop()
    return out


def _collapse_ws(text: str) -> str:
    """Collapse any whitespace (including newlines) to a single space."""
    return " ".join(text.split())


def _said_line(seg: Segment) -> str:
    text = _collapse_ws(seg.text)
    payload = text
    if seg.speaker:
        # Digit labels only: speakers stage owns numbering; "S1" / "SPEAKER_00" pass through.
        label = f"S{seg.speaker}" if seg.speaker.isdigit() else seg.speaker
        payload = f"{label}: {text}"
    return f"[{_clock(seg.start)}-{_clock(seg.end)}] said/{seg.source}: {payload}"


def _seen_lines(
    frame: Frame,
    desc: Description | None,
    dest_dir: Path,
    fallback_tag: str,
) -> list[str]:
    kind = "seen" if frame.kind == "primary" else "seen+"
    tag = desc.source if desc is not None else fallback_tag
    rel = _relative_path(frame.path, dest_dir)
    if desc is None:
        summary = MISSING_DESCRIPTION
        text_line = "  text:"
    else:
        summary = _collapse_ws(desc.summary)
        if desc.strings:
            quoted = ", ".join(f'"{_collapse_ws(s)}"' for s in desc.strings)
            text_line = f"  text: {quoted}"
        elif desc.illegible:
            text_line = "  text: illegible"
        else:
            text_line = "  text:"
    return [
        f"[{_clock(frame.time)}] {kind}/{tag} #{frame.id} {rel}: {summary}",
        text_line,
    ]


def _extra_event_key(line: str) -> tuple[float, str]:
    match = _EVENT_HEAD.match(line)
    if not match:
        raise ValueError(f"unparseable extra_events line: {line!r}")
    return parse_time(match.group("time")), match.group("kind")


def _clock(seconds: float) -> str:
    rendered = format_time(seconds, tenths=True)
    if rendered.endswith(".0"):
        return rendered[:-2]
    return rendered


def _vision_tag(vision: str) -> str:
    return vision.split()[0] if vision else "none"


def _relative_path(path: str, dest_dir: Path) -> str:
    raw = Path(path)
    if raw.is_absolute():
        try:
            return raw.resolve().relative_to(dest_dir.resolve()).as_posix()
        except ValueError as exc:
            raise ValueError(
                f"frame path {path!r} is outside dest_dir {dest_dir.resolve().as_posix()}"
            ) from exc
    return raw.as_posix()


def _atomic_write(path: Path, text: str) -> None:
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        tmp.write_text(text, encoding="utf-8")
        tmp.replace(path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
