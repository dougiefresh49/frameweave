"""Frame budget planner, ffmpeg extractor, and contact sheets."""

from __future__ import annotations

import math
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from frameweave.config import Check, Config, FlagSpec
from frameweave.types import Frame, Segment
from frameweave.util import timecode
from frameweave.util.retry import Outcome, call

_NEAR_S = 5.0
_JPEG_Q = "4"
_SHEET_CELL = 426
_SHEET_HEIGHT = 240
_FFMPEG_REMEDY = "brew install ffmpeg"
_PAD_COLOR = f"color=c=black:s={_SHEET_CELL}x{_SHEET_HEIGHT}:r=1:d=1"


class FfmpegError(RuntimeError):
    """Nonzero ffmpeg exit (no retry; ffmpeg failures are not transient)."""


@dataclass(frozen=True)
class FramePlan:
    frames: list[Frame]
    windows: list[tuple[float, float]]
    budget: int
    interval_s: float
    merged: bool


def plan(segments: list[Segment], duration_s: float, config: Config) -> FramePlan:
    """Pick frame times under the section 2 budget rule."""
    interval = float(config.frame_interval_s)
    budget = _budget(duration_s, config)
    if budget <= 0 or duration_s <= 0:
        return FramePlan([], [], budget, interval, merged=False)

    spans = _spans(segments, duration_s, interval)
    primaries = _unique_starts(spans)
    if len(primaries) > budget:
        return _merged_plan(spans, segments, duration_s, budget, interval)

    extra_times, effective = _fit_extras(
        spans, [t for t, _ in primaries], interval, budget, duration_s
    )
    primaries_and_extras = _primary_frames(primaries, segments) + _extra_frames(
        extra_times, segments
    )
    frames = _assign_ids(primaries_and_extras)
    return FramePlan(frames, [], budget, effective, merged=False)


def extract(media: Path, plan: FramePlan, dest_dir: Path, config: Config) -> list[Frame]:
    """Write planned frames as JPEGs under ``dest_dir/frames/``."""
    frames_dir = dest_dir / "frames"
    frames_dir.mkdir(parents=True, exist_ok=True)
    width = int(config.frame_width)
    timeout_s = float(config.timeout_s)
    out: list[Frame] = []
    for frame in plan.frames:
        dest = dest_dir / frame.path
        dest.parent.mkdir(parents=True, exist_ok=True)
        if dest.exists() and dest.stat().st_size > 0:
            out.append(frame)
            continue
        tmp = dest.with_name(f"{dest.stem}.tmp.jpg")
        try:
            _run_ffmpeg(
                [
                    "ffmpeg",
                    "-nostdin",
                    "-y",
                    "-ss",
                    f"{frame.time:.1f}",
                    "-i",
                    str(media),
                    "-frames:v",
                    "1",
                    "-vf",
                    f"scale={width}:-2",
                    "-q:v",
                    _JPEG_Q,
                    str(tmp),
                ],
                timeout_s=timeout_s,
            )
            os.replace(tmp, dest)
        finally:
            if tmp.exists():
                tmp.unlink()
        out.append(frame)
    return out


def contact_sheet(frames: list[Frame], dest_dir: Path, per_sheet: int = 9) -> list[Path]:
    """Write ``tile=3x3`` JPEG sheets of extracted frames at 426 px per cell."""
    if not frames or per_sheet <= 0:
        return []
    frames_dir = dest_dir / "frames"
    frames_dir.mkdir(parents=True, exist_ok=True)
    sheets: list[Path] = []
    for index, chunk in enumerate(_chunks(frames, per_sheet), start=1):
        dest = frames_dir / f"sheet-{index:02d}.jpg"
        tmp = dest.with_name(f"{dest.stem}.tmp.jpg")
        cmd = ["ffmpeg", "-nostdin", "-y"]
        for frame in chunk:
            cmd.extend(["-i", str(dest_dir / frame.path)])
        for _ in range(per_sheet - len(chunk)):
            cmd.extend(["-f", "lavfi", "-i", _PAD_COLOR])
        cmd.extend(
            [
                "-frames:v",
                "1",
                "-filter_complex",
                _tile_filter(per_sheet),
                "-map",
                "[out]",
                "-q:v",
                _JPEG_Q,
                str(tmp),
            ]
        )
        try:
            _run_ffmpeg(cmd, timeout_s=120.0)
            os.replace(tmp, dest)
        finally:
            if tmp.exists():
                tmp.unlink()
        sheets.append(dest)
    return sheets


def cli_flags() -> list[FlagSpec]:
    return [
        FlagSpec("--frame-width", "frame_width", int, "Extracted frame width in pixels."),
    ]


def preflight_checks(config: Config) -> list[Check]:
    path = shutil.which("ffmpeg")
    if path is None:
        return [Check("ffmpeg", False, "not on PATH", _FFMPEG_REMEDY)]
    try:
        proc = subprocess.run(
            ["ffmpeg", "-version"], capture_output=True, text=True, timeout=5, check=False
        )
    except (OSError, subprocess.TimeoutExpired):
        return [Check("ffmpeg", False, "not on PATH", _FFMPEG_REMEDY)]
    text = proc.stdout or proc.stderr
    first = text.splitlines()[0] if text else path
    ok = proc.returncode == 0
    return [Check("ffmpeg", ok, first, _FFMPEG_REMEDY)]


def upper_bound(duration_s: float, config: Config) -> int:
    """Most frames a run over ``duration_s`` can extract: the budget (decision 56)."""
    if duration_s <= 0:
        return 0
    return max(0, _budget(duration_s, config))


def _budget(duration_s: float, config: Config) -> int:
    if config.max_frames is not None:
        return int(config.max_frames)
    minutes = duration_s / 60.0
    return max(80, int(2 * minutes))


def _spans(
    segments: list[Segment], duration_s: float, interval_s: float
) -> list[tuple[float, float, int | None]]:
    ordered = sorted(enumerate(segments), key=lambda item: item[1].start)
    spans: list[tuple[float, float, int | None]] = []
    cursor = 0.0
    for index, seg in ordered:
        start = min(max(seg.start, 0.0), duration_s)
        end = min(max(seg.end, start), duration_s)
        if start >= duration_s:
            continue
        if start - cursor > interval_s:
            spans.append((cursor, start, None))
        if end > start:
            spans.append((start, end, index))
        cursor = max(cursor, end)
    if duration_s - cursor > interval_s:
        spans.append((cursor, duration_s, None))
    spans.sort(key=lambda item: item[0])
    return spans


def _unique_starts(
    spans: list[tuple[float, float, int | None]],
) -> list[tuple[float, int | None]]:
    seen: set[float] = set()
    starts: list[tuple[float, int | None]] = []
    for start, _end, index in spans:
        time = round(start, 1)
        if time in seen:
            continue
        seen.add(time)
        starts.append((time, index))
    return starts


def _fit_extras(
    spans: list[tuple[float, float, int | None]],
    primary_times: list[float],
    interval: float,
    budget: int,
    duration_s: float,
) -> tuple[list[float], float]:
    remaining = budget - len(primary_times)
    if remaining <= 0:
        return [], interval
    extras = _interval_times(spans, primary_times, interval, duration_s)
    if len(extras) <= remaining:
        return extras, interval
    lo = interval
    hi = max(duration_s, interval)
    best = hi
    best_extras: list[float] = []
    for _ in range(48):
        mid = (lo + hi) / 2
        cand = _interval_times(spans, primary_times, mid, duration_s)
        if len(cand) > remaining:
            lo = mid
        else:
            best = mid
            best_extras = cand
            hi = mid
    if len(best_extras) > remaining:
        best_extras = best_extras[:remaining]
    return best_extras, best


def _interval_times(
    spans: list[tuple[float, float, int | None]],
    primary_times: list[float],
    interval: float,
    duration_s: float,
) -> list[float]:
    extras: list[float] = []
    seen: set[float] = set(primary_times)
    if interval <= 0:
        return extras
    for start, end, _index in spans:
        if end - start <= interval:
            continue
        k = 1
        while True:
            time = round(start + k * interval, 1)
            if time >= end or time >= duration_s:
                break
            if time not in seen and not _near_primary(time, primary_times):
                extras.append(time)
                seen.add(time)
            k += 1
    extras.sort()
    return extras


def _near_primary(time: float, primaries: list[float]) -> bool:
    return any(abs(time - primary) < _NEAR_S for primary in primaries)


def _merged_plan(
    spans: list[tuple[float, float, int | None]],
    segments: list[Segment],
    duration_s: float,
    budget: int,
    interval: float,
) -> FramePlan:
    min_span = math.ceil(duration_s / budget)
    windows = _merge_windows(spans, float(min_span))
    picked: list[Frame] = []
    for start, _end in windows:
        time = round(start, 1)
        picked.append(
            Frame(
                id="f0000",
                time=time,
                kind="primary",
                path="",
                segment_index=_segment_index(segments, time),
            )
        )
    frames = _assign_ids(picked)
    return FramePlan(frames, windows, budget, float(min_span), merged=True)


def _merge_windows(
    spans: list[tuple[float, float, int | None]], min_span: float
) -> list[tuple[float, float]]:
    if not spans:
        return []
    windows: list[tuple[float, float]] = []
    cur_s, cur_e = spans[0][0], spans[0][1]
    for start, end, _index in spans[1:]:
        if cur_e - cur_s >= min_span:
            windows.append((cur_s, cur_e))
            cur_s, cur_e = start, end
        else:
            cur_e = max(cur_e, end)
    windows.append((cur_s, cur_e))
    return windows


def _primary_frames(
    primaries: list[tuple[float, int | None]], segments: list[Segment]
) -> list[Frame]:
    frames: list[Frame] = []
    for time, index in primaries:
        if index is None:
            index = _segment_index(segments, time)
        frames.append(Frame(id="f0000", time=time, kind="primary", path="", segment_index=index))
    return frames


def _extra_frames(times: list[float], segments: list[Segment]) -> list[Frame]:
    return [
        Frame(
            id="f0000",
            time=time,
            kind="extra",
            path="",
            segment_index=_segment_index(segments, time),
        )
        for time in times
    ]


def _assign_ids(frames: list[Frame]) -> list[Frame]:
    ordered = sorted(frames, key=lambda frame: (frame.time, 0 if frame.kind == "primary" else 1))
    unique: list[Frame] = []
    seen: set[float] = set()
    for frame in ordered:
        if frame.time in seen:
            continue
        seen.add(frame.time)
        unique.append(frame)
    named: list[Frame] = []
    for i, frame in enumerate(unique, start=1):
        fid = f"f{i:04d}"
        stamp = timecode.format(frame.time).replace(":", "-")
        named.append(
            Frame(
                id=fid,
                time=frame.time,
                kind=frame.kind,
                path=f"frames/{fid}-{stamp}.jpg",
                segment_index=frame.segment_index,
            )
        )
    return named


def _segment_index(segments: list[Segment], time: float) -> int | None:
    for index, seg in enumerate(segments):
        if seg.contains(time):
            return index
    return None


def _chunks(frames: list[Frame], size: int) -> list[list[Frame]]:
    return [frames[i : i + size] for i in range(0, len(frames), size)]


def _tile_filter(count: int) -> str:
    cell = (
        f"scale={_SHEET_CELL}:{_SHEET_HEIGHT}:force_original_aspect_ratio=decrease,"
        f"pad={_SHEET_CELL}:{_SHEET_HEIGHT}:(ow-iw)/2:(oh-ih)/2,setsar=1,trim=end_frame=1"
    )
    scaled = []
    labels = []
    for i in range(count):
        scaled.append(f"[{i}:v]{cell}[s{i}]")
        labels.append(f"[s{i}]")
    tiled = "".join(labels) + f"concat=n={count}:v=1:a=0,tile=3x3[out]"
    return ";".join([*scaled, tiled])


def _classify_ffmpeg(exc_or_response: object) -> Outcome:
    if isinstance(exc_or_response, subprocess.TimeoutExpired):
        return Outcome(status="timeout")
    if isinstance(exc_or_response, FileNotFoundError):
        return Outcome(status="fail")
    if isinstance(exc_or_response, subprocess.CompletedProcess):
        return Outcome(status="ok")
    return Outcome(status="fail")


def _run_ffmpeg(args: list[str], timeout_s: float) -> subprocess.CompletedProcess[bytes]:
    def fn() -> subprocess.CompletedProcess[bytes]:
        return subprocess.run(args, capture_output=True, timeout=timeout_s)

    proc = call(fn, timeout_s=timeout_s, classify=_classify_ffmpeg)
    if proc.returncode != 0:
        err = proc.stderr.decode("utf-8", errors="replace").strip()
        raise FfmpegError(f"ffmpeg failed ({proc.returncode}): {err[-500:]}")
    return proc
