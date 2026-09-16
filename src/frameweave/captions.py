"""Fetch, normalize, and segment English caption tracks."""

from __future__ import annotations

import html
import json
import re
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from frameweave.config import Check, Config, FlagSpec
from frameweave.types import Segment, Usage
from frameweave.util import timecode
from frameweave.util.retry import Outcome, call

_RATE_LIMIT_REASON = "captions rate-limited (429); speech falls through to STT"
_NO_SUBTITLES_REASON = "no English subtitles; speech falls through to STT"
_TAG = re.compile(r"<[^>]+>")
_SENTENCE_END = re.compile(r"[.!?][\"']?$")
_AUTO_MARKERS = frozenset({"auto", "automatic", "asr", "orig"})
_MANUAL_MARKERS = frozenset({"manual", "human"})
_FORMATS = frozenset({"json", "json3", "vtt"})


@dataclass(frozen=True)
class CaptionsResult:
    segments: list[Segment]
    source: str
    track: str | None
    reason: str | None
    usage: Usage


@dataclass(frozen=True)
class _Cue:
    start: float
    end: float
    text: str


@dataclass(frozen=True)
class _Track:
    path: Path
    language: str
    kind: str
    cues: list[_Cue]


def fetch(
    video_url: str,
    dest_dir: Path,
    config: Config,
    runner=None,
) -> CaptionsResult:
    """Fetch the best English track without downloading media."""
    mode = getattr(config, "captions_mode", "auto")
    if mode == "none":
        return _publish_none(dest_dir, "captions disabled", Usage())
    if mode not in {"auto", "manual"}:
        return _publish_none(dest_dir, f"unsupported captions mode: {mode}", Usage())

    dest_dir.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".captions-", dir=dest_dir))
    output_template = staging / "captions"
    command = [
        sys.executable,
        "-m",
        "yt_dlp",
        "--skip-download",
        "--write-subs",
        "--write-auto-subs",
        "--sub-langs",
        "en.*,en",
        "--sub-format",
        "json3/vtt",
        "-o",
        str(output_template),
        video_url,
    ]
    run = subprocess.run if runner is None else runner
    calls = 0
    started = time.monotonic()
    last: object | None = None

    def invoke():
        nonlocal calls, last
        calls += 1
        try:
            last = run(
                command,
                capture_output=True,
                text=True,
                timeout=config.timeout_s,
                check=False,
            )
        except Exception as exc:
            last = exc
            raise
        return last

    try:
        call(invoke, timeout_s=config.timeout_s, classify=_classify)
        usage = Usage(calls=calls, seconds=time.monotonic() - started)
        if _is_rate_limited(_result_text(last)):
            return _publish_none(dest_dir, _RATE_LIMIT_REASON, usage)
        tracks = [_read_track(path) for path in _caption_files(staging)]
        chosen = _choose_track(tracks, mode)
        if chosen is None:
            reason = _NO_SUBTITLES_REASON
            detail = _result_text(last)
            if _is_rate_limited(detail):
                reason = _RATE_LIMIT_REASON
            elif _says_no_subtitles(detail):
                reason = _NO_SUBTITLES_REASON
            return _publish_none(dest_dir, reason, usage)

        final_path = dest_dir / f"captions.{chosen.language}.{chosen.path.suffix.lstrip('.')}"
        chosen.path.replace(final_path)
        result = _result_for_track(chosen, usage)
        _publish_json(dest_dir / "captions.json", _result_dict(result))
        (dest_dir / "captions.none").unlink(missing_ok=True)
        return result
    except Exception as exc:
        usage = Usage(calls=calls, seconds=time.monotonic() - started)
        result_detail = _result_text(last)
        detail = f"{result_detail}\n{exc}"
        if _is_rate_limited(detail):
            reason = _RATE_LIMIT_REASON
        elif _says_no_subtitles(detail):
            reason = _NO_SUBTITLES_REASON
        elif isinstance(last, subprocess.TimeoutExpired):
            reason = "captions fetch timed out; speech falls through to STT"
        else:
            message = _last_line(result_detail) or _last_line(str(exc)) or type(exc).__name__
            reason = f"captions unavailable: {message}; speech falls through to STT"
        return _publish_none(dest_dir, reason, usage)
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def expose(dest_dir: Path) -> CaptionsResult:
    """Parse an already-downloaded raw track and atomically expose ``captions.json``."""
    tracks = [_read_track(path) for path in _caption_files(dest_dir)]
    chosen = _choose_track(tracks, "auto")
    if chosen is None:
        return _publish_none(dest_dir, _NO_SUBTITLES_REASON, Usage())
    result = _result_for_track(chosen, Usage())
    _publish_json(dest_dir / "captions.json", _result_dict(result))
    (dest_dir / "captions.none").unlink(missing_ok=True)
    return result


def cli_flags() -> list[FlagSpec]:
    return [
        FlagSpec(
            "--captions",
            "captions_mode",
            str,
            "Caption source: auto (prefer manual), manual, or none.",
            default="auto",
        )
    ]


def preflight_checks(config: Config) -> list[Check]:
    return []


def _classify(value: object) -> Outcome:
    if isinstance(value, subprocess.TimeoutExpired):
        return Outcome("timeout")
    if isinstance(value, Exception):
        return Outcome("fail")
    return Outcome("ok" if getattr(value, "returncode", 1) == 0 else "fail")


def _caption_files(directory: Path) -> list[Path]:
    if not directory.is_dir():
        return []
    return sorted(
        path
        for path in directory.iterdir()
        if path.is_file()
        and path.name.startswith("captions.")
        and path.name != "captions.json"
        and path.suffix.lstrip(".").lower() in _FORMATS
    )


def _read_track(path: Path) -> _Track:
    language, marked_kind = _track_identity(path)
    suffix = path.suffix.lower()
    cues = _parse_vtt(path) if suffix == ".vtt" else _parse_json3(path)
    kind = marked_kind or ("automatic" if _looks_rolling(cues) else "manual")
    return _Track(path=path, language=language, kind=kind, cues=cues)


def _track_identity(path: Path) -> tuple[str, str | None]:
    pieces = path.name.split(".")[1:-1]
    kind: str | None = None
    language_parts: list[str] = []
    for piece in pieces:
        lowered = piece.lower()
        if lowered in _AUTO_MARKERS:
            kind = "automatic"
        elif lowered in _MANUAL_MARKERS:
            kind = "manual"
        else:
            language_parts.append(piece)
    language = ".".join(language_parts) or "en"
    return language, kind


def _choose_track(tracks: list[_Track], mode: str) -> _Track | None:
    english = [track for track in tracks if track.cues and _is_english(track.language)]
    english.sort(key=lambda track: (_language_rank(track.language), track.path.name))
    manual = [track for track in english if track.kind == "manual"]
    automatic = [track for track in english if track.kind == "automatic"]
    if manual:
        return manual[0]
    if mode == "auto" and automatic:
        return automatic[0]
    return None


def _is_english(language: str) -> bool:
    lowered = language.lower()
    return lowered == "en" or lowered.startswith(("en-", "en_", "en."))


def _language_rank(language: str) -> tuple[int, str]:
    lowered = language.lower()
    return (0 if lowered == "en" else 1, lowered)


def _parse_json3(path: Path) -> list[_Cue]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    cues: list[_Cue] = []
    for event in payload.get("events", []):
        if not isinstance(event, dict):
            continue
        raw_segments = event.get("segs")
        if not isinstance(raw_segments, list):
            continue
        text = _clean_text(
            "".join(
                str(segment.get("utf8", ""))
                for segment in raw_segments
                if isinstance(segment, dict)
            )
        )
        if not text:
            continue
        start = float(event.get("tStartMs", 0)) / 1000
        duration = float(event.get("dDurationMs", 0)) / 1000
        cues.append(_Cue(start=start, end=start + max(duration, 0.0), text=text))
    return _fill_missing_ends(cues)


def _parse_vtt(path: Path) -> list[_Cue]:
    text = path.read_text(encoding="utf-8-sig").replace("\r\n", "\n")
    cues: list[_Cue] = []
    for block in re.split(r"\n\s*\n", text):
        lines = [line.strip() for line in block.splitlines()]
        timing_index = next((index for index, line in enumerate(lines) if "-->" in line), None)
        if timing_index is None:
            continue
        left, right = lines[timing_index].split("-->", 1)
        end_token = right.strip().split()[0]
        cue_text = _clean_text(" ".join(lines[timing_index + 1 :]))
        if not cue_text:
            continue
        cues.append(
            _Cue(
                start=timecode.parse(left.strip().replace(",", ".")),
                end=timecode.parse(end_token.replace(",", ".")),
                text=cue_text,
            )
        )
    return cues


def _fill_missing_ends(cues: list[_Cue]) -> list[_Cue]:
    filled: list[_Cue] = []
    for index, cue in enumerate(cues):
        end = cue.end
        if end <= cue.start and index + 1 < len(cues):
            end = max(cue.start, cues[index + 1].start)
        filled.append(_Cue(cue.start, end, cue.text))
    return filled


def _clean_text(value: str) -> str:
    return " ".join(html.unescape(_TAG.sub("", value)).replace("\xa0", " ").split())


def _looks_rolling(cues: list[_Cue]) -> bool:
    if len(cues) < 2:
        return False
    overlaps = sum(
        _overlap_size(previous.text, current.text) > 0
        for previous, current in zip(cues, cues[1:], strict=False)
    )
    return overlaps >= 2 and overlaps / (len(cues) - 1) >= 0.2


def _deduplicate(cues: list[_Cue]) -> list[_Cue]:
    kept: list[_Cue] = []
    seen: set[str] = set()
    previous = ""
    for cue in cues:
        normalized = _clean_text(cue.text)
        if not normalized or normalized in seen:
            previous = normalized or previous
            continue
        words = normalized.split()
        overlap = _overlap_size(previous, normalized)
        new_text = " ".join(words[overlap:])
        seen.add(normalized)
        previous = normalized
        if new_text:
            kept.append(_Cue(cue.start, cue.end, new_text))
    return kept


def _overlap_size(previous: str, current: str) -> int:
    before = previous.split()
    after = current.split()
    for size in range(min(len(before), len(after)), 0, -1):
        if before[-size:] == after[:size]:
            return size
    return 0


def _result_for_track(track: _Track, usage: Usage) -> CaptionsResult:
    cues = _deduplicate(track.cues) if track.kind == "automatic" else track.cues
    source = "captions-auto" if track.kind == "automatic" else "captions"
    return CaptionsResult(
        segments=_segment(cues, source),
        source=source,
        track=f"{track.language} ({track.kind})",
        reason=None,
        usage=usage,
    )


def _segment(cues: list[_Cue], source: str) -> list[Segment]:
    if not cues:
        return []
    groups: list[list[_Cue]] = []
    start = 0
    while start < len(cues):
        remaining_duration = cues[-1].end - cues[start].start
        if remaining_duration <= 40:
            groups.append(cues[start:])
            break
        candidates: list[int] = []
        for index in range(start, len(cues)):
            duration = cues[index].end - cues[start].start
            if 20 <= duration <= 40:
                candidates.append(index)
            if duration > 40:
                break
        if not candidates:
            boundary = start
        else:
            sentence_ends = [
                index for index in candidates if _SENTENCE_END.search(cues[index].text)
            ]
            pool = sentence_ends or candidates
            boundary = min(pool, key=lambda index: abs((cues[index].end - cues[start].start) - 30))
        groups.append(cues[start : boundary + 1])
        start = boundary + 1

    return [
        Segment(
            id=f"s{index:04d}",
            start=group[0].start,
            end=group[-1].end,
            text=" ".join(cue.text for cue in group),
            source=source,
        )
        for index, group in enumerate(groups, start=1)
    ]


def _result_dict(result: CaptionsResult) -> dict[str, object]:
    return {
        "segments": [segment.to_dict() for segment in result.segments],
        "source": result.source,
        "track": result.track,
        "reason": result.reason,
    }


def _publish_none(dest_dir: Path, reason: str, usage: Usage) -> CaptionsResult:
    result = CaptionsResult([], "none", None, reason, usage)
    dest_dir.mkdir(parents=True, exist_ok=True)
    _publish_json(dest_dir / "captions.none", _result_dict(result))
    (dest_dir / "captions.json").unlink(missing_ok=True)
    return result


def _publish_json(path: Path, payload: dict[str, object]) -> None:
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _result_text(result: object | None) -> str:
    if result is None:
        return ""
    if isinstance(result, Exception):
        return str(result)
    return "\n".join(
        str(value)
        for value in (getattr(result, "stdout", ""), getattr(result, "stderr", ""))
        if value
    )


def _is_rate_limited(text: str) -> bool:
    lowered = text.lower()
    return "429" in lowered or "rate limit" in lowered or "too many requests" in lowered


def _says_no_subtitles(text: str) -> bool:
    lowered = text.lower()
    return any(
        phrase in lowered
        for phrase in (
            "no subtitles",
            "doesn't have subtitles",
            "does not have subtitles",
            "no automatic captions",
            "no caption",
        )
    )


def _last_line(text: str) -> str:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return lines[-1] if lines else ""
