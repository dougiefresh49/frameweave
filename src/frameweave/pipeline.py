"""Stage registry, run keys, progress, and cleanup signal handling.

Each stage writes one artifact. Keyed stages live under
``<cache>/runs/<video_id>/<run_key>/``; source stages under
``<cache>/sources/<video_id>/``. A stage is reused when its artifact exists
and dependency artifact digests match the stored ``inputs.json``.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import signal
import time
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from frameweave import __version__
from frameweave.captions import CaptionsResult
from frameweave.captions import fetch as fetch_captions_track
from frameweave.config import (
    Check,
    Config,
    ConfigError,
    FlagSpec,
    channel_slug,
    load,
    output_dir,
    require_out,
    slugify,
)
from frameweave.config import (
    run_key as make_run_key,
)
from frameweave.format.readme import write_readme
from frameweave.format.writer import (
    RunMeta,
    derive_completion,
    write_cost,
    write_meta,
    write_transcript,
)
from frameweave.frames import extract as extract_frames
from frameweave.frames import plan as plan_frames
from frameweave.ledger import Ledger, dollars_for_stage, reconcile
from frameweave.range import RangeSpec, overlaps
from frameweave.range import parse as parse_range
from frameweave.sources.http import HttpSource
from frameweave.sources.local import LocalFileSource
from frameweave.sources.youtube import YouTubeSource, claim
from frameweave.stt import audio as stt_audio
from frameweave.stt.base import SttBackend, SttResult, clamp_to_words, merge_chunks
from frameweave.stt.local import WhisperXBackend, is_silent
from frameweave.types import (
    Description,
    Frame,
    LedgerEntry,
    Resolved,
    Segment,
    Source,
    StageResult,
    Usage,
)
from frameweave.util import timecode
from frameweave.vision.base import VisionBackend, make_backend
from frameweave.vision.choose import (
    COEFFICIENTS,
    Plan,
    choice_to_dict,
    choose,
    lane_actual_from_run,
    load_snapshot,
)

_CONTEXT_CAP = 2000


def _progress_noop(_msg: str) -> None:
    return None

# Populated while a run is active so signal handlers can clean the current run.
_ACTIVE: dict[str, Any] = {}


@dataclass(frozen=True)
class Stage:
    name: str
    run: Callable[[RunContext], StageResult]
    depends_on: tuple[str, ...]
    artifact: str
    keyed: bool


@dataclass
class RunContext:
    config: Config
    raw_input: str
    source: Source
    resolved: Resolved | None
    source_dir: Path
    run_dir: Path
    run_key: str
    range_spec: RangeSpec
    ledger: Ledger
    progress: Callable[[str], None]
    redo: set[str]
    frames_only: bool = False
    stt: SttBackend | None = None
    vision: VisionBackend | None = None
    warnings: list[str] = field(default_factory=list)
    output_path: Path | None = None
    completion: str = "complete"
    cost_usd: float = 0.0
    out_override: Path | None = None
    range_start: str | None = None
    range_end: str | None = None
    range_chapter: str | None = None
    range_url_t: float | None = None
    range_locked: bool = False
    range_deferred: bool = False
    requested_vision_lane: str = "auto"
    lane_choice: dict[str, Any] | None = None
    lane_actual: dict[str, Any] | None = None


@dataclass(frozen=True)
class RunOutcome:
    output_path: Path
    completion: str
    cost_usd: float
    run_key: str
    warnings: list[str]


def cli_flags() -> list[FlagSpec]:
    return [
        FlagSpec(
            "--redo",
            "redo",
            str,
            "Force this stage and every stage after it to rerun (repeatable).",
        ),
        FlagSpec(
            "--frames-only",
            "frames_only",
            bool,
            "Skip captions, transcript, and describe; speech not requested.",
        ),
    ]


def preflight_checks(config: Config) -> list[Check]:
    runs = Path(config.cache_dir) / "runs"
    ok, detail = _creatable_writable(runs)
    return [
        Check(
            name="cache runs dir",
            ok=ok,
            detail=detail,
            remedy=f"Ensure {runs} is creatable and writable",
            required=True,
        )
    ]


def run(
    raw_input: str,
    config: Config,
    *,
    progress: Callable[[str], None] | None = None,
    redo: tuple[str, ...] | list[str] | set[str] = (),
    frames_only: bool = False,
    source: Source | None = None,
    stt: SttBackend | None = None,
    vision: VisionBackend | None = None,
    out_override: Path | None = None,
    resolved: Resolved | None = None,
    range_spec: RangeSpec | None = None,
    start: str | None = None,
    end: str | None = None,
    chapter: str | None = None,
    url_t: float | None = None,
) -> RunOutcome:
    """Execute the stage registry and return the assembled output path."""
    progress_fn = progress or _progress_noop
    redo_set = _cascade_redo(set(redo))
    requested_lane = config.vision_lane
    # Fail before any stage spends quota when the output root is missing,
    # unless --out gave an exact folder (out_override).
    if out_override is None:
        require_out(config)
    stt = _prepare_stt(config, stt)
    reconcile(config.cache_dir)

    picked = source or _pick_source(raw_input)
    resolved = resolved if resolved is not None else picked.resolve(raw_input)
    # Resolve auto with an upper-bound plan so run_key has a concrete lane;
    # describe re-runs choose with the actual frame plan (issue #26).
    config, early_choice = _with_resolved_lane(config, resolved=resolved)
    source_dir = Path(config.cache_dir) / "sources" / resolved.video_id
    source_dir.mkdir(parents=True, exist_ok=True)
    locked = range_spec is not None
    deferred = (
        not locked
        and float(resolved.duration) <= 0.0
        and callable(getattr(picked, "resolved_after_fetch", None))
    )
    rs = range_spec or parse_range(
        start, end, chapter, url_t, resolved, defer_bounds=deferred
    )
    if deferred:
        # Key and run dir wait until duration is known (post-fetch refresh).
        key = "_pending_range"
    else:
        key = make_run_key(config, rs.label)
    run_dir = Path(config.cache_dir) / "runs" / resolved.video_id / key
    run_dir.mkdir(parents=True, exist_ok=True)

    ledger = Ledger(run_dir)
    ctx = RunContext(
        config=config,
        raw_input=raw_input,
        source=picked,
        resolved=resolved,
        source_dir=source_dir,
        run_dir=run_dir,
        run_key=key,
        range_spec=rs,
        ledger=ledger,
        progress=progress_fn,
        redo=redo_set,
        frames_only=frames_only,
        stt=stt,
        vision=vision,
        out_override=out_override,
        range_start=None if locked else start,
        range_end=None if locked else end,
        range_chapter=None if locked else chapter,
        range_url_t=None if locked else url_t,
        range_locked=locked,
        range_deferred=deferred,
        requested_vision_lane=requested_lane,
        lane_choice=early_choice,
    )
    if early_choice and early_choice.get("warning"):
        ctx.warnings.append(str(early_choice["warning"]))

    _install_handlers(ctx)
    try:
        by_name = {stage.name: stage for stage in STAGES}
        for stage in STAGES:
            started = time.monotonic()
            result = _execute_stage(ctx, stage, by_name)
            elapsed = time.monotonic() - started
            progress_fn(f"stage {stage.name}: {result.status} ({elapsed:.1f}s)")
            if result.status == "failed":
                raise RuntimeError(f"stage {stage.name} failed")
            ctx.warnings.extend(result.warnings)
        assert ctx.output_path is not None
        summary = ctx.ledger.summary()
        ctx.cost_usd = float(summary["total_usd"])
        return RunOutcome(
            output_path=ctx.output_path,
            completion=ctx.completion,
            cost_usd=ctx.cost_usd,
            run_key=ctx.run_key,
            warnings=list(ctx.warnings),
        )
    finally:
        _clear_handlers()


def _execute_stage(
    ctx: RunContext, stage: Stage, by_name: dict[str, Stage]
) -> StageResult:
    if ctx.frames_only and stage.name in {"fetch_captions", "transcript", "describe"}:
        result = _write_skip_stub(ctx, stage)
        _write_inputs(ctx, stage, by_name)
        return result

    if stage.name not in ctx.redo:
        reused = _try_reuse(ctx, stage, by_name)
        if reused is not None:
            return reused

    if stage.keyed:
        ctx.ledger.drop_stage(stage.name)

    result = stage.run(ctx)
    if result.status == "done":
        _write_inputs(ctx, stage, by_name)
    return result


def _try_reuse(
    ctx: RunContext, stage: Stage, by_name: dict[str, Stage]
) -> StageResult | None:
    expected = _dependency_digests(ctx, stage, by_name)
    artifact = _artifact_path(ctx, stage)
    inputs_path = _inputs_path(ctx, stage)
    if artifact.is_file() and _inputs_match(inputs_path, expected):
        if stage.name == "assemble" and not _assemble_output_exists(artifact):
            return None
        _hydrate_after_reuse(ctx, stage)
        return StageResult(stage=stage.name, status="reused", artifact=str(artifact))

    # Speech artifacts are reusable across run keys that only change frame/vision
    # settings (frame_interval invalidates 5-7, not transcript). Never sibling-reuse
    # frames/describe/assemble — those are keyed by the run key itself.
    if stage.keyed and stage.name == "transcript":
        sibling = _find_sibling(ctx, stage, expected)
        if sibling is not None:
            _copy_reuse(sibling, artifact)
            _atomic_json(inputs_path, expected)
            usd = dollars_for_stage(sibling.parent, stage.name)
            if usd:
                ctx.ledger.add_reused(usd)
            _hydrate_after_reuse(ctx, stage)
            return StageResult(stage=stage.name, status="reused", artifact=str(artifact))
    return None


def _assemble_output_exists(artifact: Path) -> bool:
    try:
        data = json.loads(artifact.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    path = data.get("output_path")
    return isinstance(path, str) and Path(path).is_dir()


def _find_sibling(
    ctx: RunContext, stage: Stage, expected: dict[str, str]
) -> Path | None:
    root = Path(ctx.config.cache_dir) / "runs" / (ctx.resolved.video_id if ctx.resolved else "")
    if not root.is_dir():
        return None
    for run_dir in sorted(root.iterdir()):
        if not run_dir.is_dir() or run_dir == ctx.run_dir:
            continue
        candidate = run_dir / stage.artifact
        inputs = run_dir / f"{stage.artifact}.inputs.json"
        if candidate.is_file() and _inputs_match(inputs, expected):
            return candidate
    return None


def _copy_reuse(src: Path, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    if src.is_dir():
        if dest.exists():
            shutil.rmtree(dest)
        shutil.copytree(src, dest)
    else:
        shutil.copy2(src, dest)
        # frames.json reuse also needs the frames/ directory when present
        frames_dir = src.parent / "frames"
        if src.name == "frames.json" and frames_dir.is_dir():
            target = dest.parent / "frames"
            if target.exists():
                shutil.rmtree(target)
            shutil.copytree(frames_dir, target)


def _hydrate_after_reuse(ctx: RunContext, stage: Stage) -> None:
    if stage.name == "resolve":
        data = json.loads(_artifact_path(ctx, stage).read_text(encoding="utf-8"))
        ctx.resolved = Resolved.from_dict(data)
        _refresh_range(ctx)
    elif stage.name == "assemble":
        data = json.loads(_artifact_path(ctx, stage).read_text(encoding="utf-8"))
        ctx.output_path = Path(data["output_path"])
        ctx.completion = data.get("completion", ctx.completion)


def _refresh_range(ctx: RunContext) -> None:
    """Recompute the range after resolved duration/chapters become final."""
    if ctx.range_locked or ctx.resolved is None:
        return
    still_unknown = ctx.range_deferred and float(ctx.resolved.duration) <= 0.0
    ctx.range_spec = parse_range(
        ctx.range_start,
        ctx.range_end,
        ctx.range_chapter,
        ctx.range_url_t,
        ctx.resolved,
        defer_bounds=still_unknown,
    )
    if ctx.range_deferred and float(ctx.resolved.duration) > 0.0:
        _bind_run_placement(ctx)
        ctx.range_deferred = False


def _bind_run_placement(ctx: RunContext) -> None:
    """Set run key and directory from the finalized range label."""
    assert ctx.resolved is not None
    key = make_run_key(ctx.config, ctx.range_spec.label)
    run_dir = Path(ctx.config.cache_dir) / "runs" / ctx.resolved.video_id / key
    run_dir.mkdir(parents=True, exist_ok=True)
    ctx.run_key = key
    ctx.run_dir = run_dir
    ctx.ledger = Ledger(run_dir)


def _write_skip_stub(ctx: RunContext, stage: Stage) -> StageResult:
    path = _artifact_path(ctx, stage)
    path.parent.mkdir(parents=True, exist_ok=True)
    if stage.name == "fetch_captions":
        _atomic_json(
            path,
            {
                "segments": [],
                "source": "none",
                "track": None,
                "reason": "frames-only; speech not requested",
            },
        )
    elif stage.name == "transcript":
        _atomic_json(path, {"segments": [], "source": "none", "silent": True})
    elif stage.name == "describe":
        _atomic_json(path, {"descriptions": []})
    return StageResult(stage=stage.name, status="skipped", artifact=str(path))


def _write_inputs(ctx: RunContext, stage: Stage, by_name: dict[str, Stage]) -> None:
    _atomic_json(_inputs_path(ctx, stage), _dependency_digests(ctx, stage, by_name))


def _dependency_digests(
    ctx: RunContext, stage: Stage, by_name: dict[str, Stage]
) -> dict[str, str]:
    digests: dict[str, str] = {}
    for dep_name in stage.depends_on:
        dep = by_name[dep_name]
        path = _artifact_path(ctx, dep)
        digests[dep_name] = _sha256_file(path) if path.is_file() else ""
    if stage.name == "transcript":
        digests["settings"] = _transcript_settings_digest(ctx.config, ctx.range_spec)
    if stage.name == "assemble":
        if ctx.out_override is not None:
            digests["out"] = str(ctx.out_override.resolve())
        else:
            digests["out"] = str(require_out(ctx.config).resolve())
    return digests


def _transcript_settings_digest(config: Config, range_spec: RangeSpec) -> str:
    payload = {
        "stt_backend": config.stt_backend,
        "stt_model": config.stt_model,
        "stt_device": config.stt_device,
        "speakers": config.speakers,
        "captions_mode": config.captions_mode,
        "range": range_spec.label,
    }
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode()).hexdigest()


def _inputs_match(path: Path, expected: dict[str, str]) -> bool:
    if not path.is_file():
        return False
    try:
        stored = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    return stored == expected


def _artifact_path(ctx: RunContext, stage: Stage) -> Path:
    base = ctx.run_dir if stage.keyed else ctx.source_dir
    return base / stage.artifact


def _inputs_path(ctx: RunContext, stage: Stage) -> Path:
    base = ctx.run_dir if stage.keyed else ctx.source_dir
    return base / f"{stage.artifact}.inputs.json"


def _cascade_redo(redo: set[str]) -> set[str]:
    if not redo:
        return set()
    names = [stage.name for stage in STAGES]
    earliest = min((names.index(name) for name in redo if name in names), default=len(names))
    return set(names[earliest:])


def _with_resolved_lane(
    config: Config,
    *,
    resolved: Resolved | None = None,
    frames: int | None = None,
    transcript_minutes: float | None = None,
) -> tuple[Config, dict[str, Any] | None]:
    """Resolve ``auto`` via the usage-aware chooser. Returns (config, lane_choice dict)."""
    if config.vision_lane != "auto":
        return config, None
    if resolved is None:
        return replace(config, vision_lane="claude"), None

    if frames is None:
        synthetic = [Segment(0.0, resolved.duration, "", "none")]
        planned = plan_frames(synthetic, resolved.duration, config)
        frame_count = len(planned.frames)
        minutes = resolved.duration / 60.0
    else:
        frame_count = int(frames)
        minutes = (
            float(transcript_minutes)
            if transcript_minutes is not None
            else resolved.duration / 60.0
        )

    snapshot = load_snapshot()
    plan = Plan(
        frames=frame_count,
        transcript_minutes=minutes,
        frames_per_call=max(1, int(config.frames_per_call)),
    )
    choice = choose(plan, snapshot, COEFFICIENTS, config, explicit_lane=None)
    return replace(config, vision_lane=choice.lane), choice_to_dict(choice)


def _prepare_stt(config: Config, stt: SttBackend | None) -> SttBackend | None:
    """Build the STT backend; raise SpeakersUnavailable before stage 1 when needed."""
    if stt is not None:
        return stt
    if config.speakers:
        from frameweave.stt.speakers import make_diarizer

        return WhisperXBackend(diarize=make_diarizer(config))
    return None


def _pick_source(raw_input: str) -> Source:
    for candidate in (YouTubeSource(), HttpSource(), LocalFileSource()):
        if candidate.matches(raw_input):
            return candidate
    raise ValueError(f"no source matches input: {raw_input!r}")


# --- stages ---


def _stage_resolve(ctx: RunContext) -> StageResult:
    assert ctx.resolved is not None
    path = ctx.source_dir / "resolved.json"
    _atomic_json(path, ctx.resolved.to_dict())
    return StageResult(stage="resolve", status="done", artifact=str(path))


def _stage_fetch_media(ctx: RunContext) -> StageResult:
    assert ctx.resolved is not None
    with claim(ctx.source_dir):
        media = ctx.source.fetch_media(ctx.resolved, ctx.source_dir)
    meta = ctx.source_dir / "media.json"
    if not meta.is_file():
        # Sources always write media.json; guard for fakes that only drop media.*.
        digest = _sha256_file(media)
        _atomic_json(
            meta,
            {"sha256": digest, "bytes": media.stat().st_size, "path": media.name},
        )
    after = getattr(ctx.source, "resolved_after_fetch", None)
    if callable(after):
        ctx.resolved = after(ctx.source_dir)
        _atomic_json(ctx.source_dir / "resolved.json", ctx.resolved.to_dict())
        if not ctx.range_locked:
            _refresh_range(ctx)
    return StageResult(stage="fetch_media", status="done", artifact=str(meta))


def _stage_fetch_captions(ctx: RunContext) -> StageResult:
    assert ctx.resolved is not None
    path = ctx.source_dir / "captions.json"
    if getattr(ctx.source, "name", "") == "youtube":
        result = fetch_captions_track(ctx.resolved.source, ctx.source_dir, ctx.config)
        if result.source == "none" or not path.is_file():
            _atomic_json(
                path,
                {
                    "segments": [s.to_dict() for s in result.segments],
                    "source": result.source,
                    "track": result.track,
                    "reason": result.reason,
                },
            )
        return StageResult(
            stage="fetch_captions",
            status="done",
            artifact=str(path),
            usage=result.usage,
            warnings=[result.reason] if result.reason else [],
        )

    # Non-YouTube: never fail the run; record a none reason.
    reason = "source has no caption tracks; speech falls through to STT"
    _atomic_json(
        path,
        {"segments": [], "source": "none", "track": None, "reason": reason},
    )
    return StageResult(
        stage="fetch_captions",
        status="done",
        artifact=str(path),
        warnings=[reason],
    )


def _stage_transcript(ctx: RunContext) -> StageResult:
    assert ctx.resolved is not None
    path = ctx.run_dir / "transcript.json"
    captions = _load_captions(ctx.source_dir / "captions.json")
    use_captions = (
        captions.source != "none"
        and ctx.config.captions_mode != "none"
        and bool(captions.segments)
    )
    if use_captions:
        segments = list(captions.segments)
        source_tag = captions.source
        usage = Usage()
        silent = False
    else:
        backend = ctx.stt or _default_stt(ctx.config)
        usage = Usage()
        media = _media_file(ctx.source_dir)
        audio = stt_audio.extract(media, ctx.run_dir, timeout_s=ctx.config.timeout_s)
        chunks = stt_audio.chunk(audio, ctx.run_dir, timeout_s=ctx.config.timeout_s)
        results: list[tuple[Any, SttResult]] = []
        for piece in chunks:
            result = backend.transcribe(piece.path, ctx.config)
            results.append((piece, result))
            usage = usage + result.usage
        if not results:
            result = backend.transcribe(audio, ctx.config)
            segments = clamp_to_words(result.segments)
            source_tag = result.source
            usage = usage + result.usage
            silent = is_silent(result)
        else:
            segments = clamp_to_words(merge_chunks(results))
            source_tag = results[0][1].source if results else "stt"
            silent = not segments
        started = datetime.now(UTC).isoformat().replace("+00:00", "Z")
        ended = datetime.now(UTC).isoformat().replace("+00:00", "Z")
        provider = getattr(backend, "name", ctx.config.stt_backend)
        ctx.ledger.append(
            LedgerEntry(
                stage="transcript",
                provider=str(provider),
                model=ctx.config.stt_model,
                started=started,
                ended=ended,
                usage=usage,
                status="ok",
            )
        )

    # Assign ids if missing, then keep only segments that overlap the range.
    numbered: list[Segment] = []
    for index, seg in enumerate(segments, start=1):
        if seg.id is None:
            numbered.append(replace(seg, id=f"s{index:04d}"))
        else:
            numbered.append(seg)
    rs = ctx.range_spec
    numbered = [
        seg
        for seg in numbered
        if overlaps(seg.start, seg.end, rs.start, rs.end)
    ]

    _atomic_json(
        path,
        {
            "segments": [s.to_dict() for s in numbered],
            "source": source_tag,
            "silent": silent if not use_captions else False,
        },
    )
    return StageResult(
        stage="transcript",
        status="done",
        artifact=str(path),
        usage=usage,
        warnings=["no speech"] if (not use_captions and silent) else [],
    )


def _stage_frames(ctx: RunContext) -> StageResult:
    assert ctx.resolved is not None
    path = ctx.run_dir / "frames.json"
    transcript = _load_transcript(ctx.run_dir / "transcript.json")
    media = _media_file(ctx.source_dir)
    rs = ctx.range_spec
    window = rs.end - rs.start
    # Plan in window-relative time, then shift candidates back to absolute.
    relative = [
        replace(seg, start=seg.start - rs.start, end=seg.end - rs.start)
        for seg in transcript["segments"]
    ]
    planned = plan_frames(relative, window, ctx.config)
    absolute_frames: list[Frame] = []
    for frame in planned.frames:
        time = round(frame.time + rs.start, 1)
        stamp = timecode.format(time).replace(":", "-")
        absolute_frames.append(
            replace(frame, time=time, path=f"frames/{frame.id}-{stamp}.jpg")
        )
    absolute_plan = replace(
        planned,
        frames=absolute_frames,
        windows=[(a + rs.start, b + rs.start) for a, b in planned.windows],
    )
    frames = extract_frames(media, absolute_plan, ctx.run_dir, ctx.config)
    _atomic_json(
        path,
        {
            "frames": [f.to_dict() for f in frames],
            "windows": [[a, b] for a, b in absolute_plan.windows],
            "budget": absolute_plan.budget,
            "interval_s": absolute_plan.interval_s,
            "merged": absolute_plan.merged,
        },
    )
    return StageResult(stage="frames", status="done", artifact=str(path))


def _stage_describe(ctx: RunContext) -> StageResult:
    assert ctx.resolved is not None
    path = ctx.run_dir / "descriptions.json"
    frames = _load_frames(ctx.run_dir / "frames.json")
    transcript = _load_transcript(ctx.run_dir / "transcript.json")
    segments: list[Segment] = transcript["segments"]

    # Issue #26: when the user asked for auto, choose with the actual frame plan.
    before_snap = load_snapshot(refresh=False)
    if ctx.requested_vision_lane == "auto":
        minutes = ctx.resolved.duration / 60.0
        config, choice_dict = _with_resolved_lane(
            replace(ctx.config, vision_lane="auto"),
            resolved=ctx.resolved,
            frames=len(frames),
            transcript_minutes=minutes,
        )
        ctx.config = config
        if choice_dict is not None:
            ctx.lane_choice = choice_dict
            warning = choice_dict.get("warning")
            if warning and warning not in ctx.warnings:
                ctx.warnings.append(str(warning))

    lane = ctx.config.vision_lane
    backend = ctx.vision or make_backend(lane, ctx.config)
    model = getattr(backend, "model", ctx.config.vision_model.get(lane, ""))
    provider = getattr(backend, "name", lane)

    if lane == "none" or not frames:
        _atomic_json(path, {"descriptions": []})
        return StageResult(stage="describe", status="done", artifact=str(path))

    frames_dir = ctx.run_dir / "frames"
    batch_size = max(1, int(ctx.config.frames_per_call))
    descriptions: list[Description] = []
    total_usage = Usage()

    for start in range(0, len(frames), batch_size):
        batch = frames[start : start + batch_size]
        context = _transcript_window(segments, batch)
        started = datetime.now(UTC).isoformat().replace("+00:00", "Z")
        try:
            batch_descs, usage = backend.describe(batch, context, frames_dir, ctx.config)
        except Exception:
            # Ledger must persist before the stage artifact exists.
            ended = datetime.now(UTC).isoformat().replace("+00:00", "Z")
            ctx.ledger.append(
                LedgerEntry(
                    stage="describe",
                    provider=provider,
                    model=model,
                    started=started,
                    ended=ended,
                    usage=Usage(calls=1, unknown_usd=True),
                    status="failed",
                )
            )
            raise
        ended = datetime.now(UTC).isoformat().replace("+00:00", "Z")
        ctx.ledger.append(
            LedgerEntry(
                stage="describe",
                provider=provider,
                model=model,
                started=started,
                ended=ended,
                usage=usage,
                status="ok",
            )
        )
        descriptions.extend(batch_descs)
        total_usage = total_usage + usage

    if lane != "none" and frames:
        after_snap = load_snapshot(refresh=True)
        ctx.lane_actual = lane_actual_from_run(
            lane=lane,
            frames=len(frames),
            calls=int(total_usage.calls),
            tokens=int(
                total_usage.tokens_in
                + total_usage.tokens_out
                + total_usage.tokens_reasoning
            ),
            before=before_snap,
            after=after_snap,
        )
        _atomic_json(ctx.run_dir / "lane_actual.json", ctx.lane_actual)

    _atomic_json(
        path,
        {"descriptions": [d.to_dict() for d in descriptions]},
    )
    return StageResult(
        stage="describe",
        status="done",
        artifact=str(path),
        usage=total_usage,
    )


def _stage_assemble(ctx: RunContext) -> StageResult:
    assert ctx.resolved is not None
    marker = ctx.run_dir / "assembled.json"
    rs = ctx.range_spec
    if ctx.out_override is not None:
        out = output_dir(
            Path("."),
            "",
            "",
            out_override=ctx.out_override,
        )
    else:
        root = require_out(ctx.config)
        out = output_dir(
            root,
            channel_slug(ctx.resolved.channel, ctx.config, root),
            slugify(ctx.resolved.title),
            range_slug=rs.slug or None,
        )
    out.mkdir(parents=True, exist_ok=True)

    transcript = _load_transcript(ctx.run_dir / "transcript.json")
    segments: list[Segment] = list(transcript["segments"])
    frames = _load_frames(ctx.run_dir / "frames.json")
    descriptions = _load_descriptions(ctx.run_dir / "descriptions.json")
    captions = _load_captions(ctx.source_dir / "captions.json")

    # Copy frames into the output folder.
    src_frames = ctx.run_dir / "frames"
    dest_frames = out / "frames"
    if src_frames.is_dir():
        if dest_frames.exists():
            shutil.rmtree(dest_frames)
        shutil.copytree(src_frames, dest_frames)

    speech_requested = not ctx.frames_only
    if ctx.frames_only:
        completion, reason = "complete (speech not requested)", None
    else:
        completion, reason = derive_completion(
            segments, list(ctx.warnings), speech_requested=speech_requested
        )

    lane = ctx.config.vision_lane
    model = ctx.config.vision_model.get(lane, "")
    if lane == "none" or ctx.frames_only:
        vision = "none"
    else:
        vision = (
            f"{lane}:{model} {ctx.config.vision_quality}, "
            f"{ctx.config.frames_per_call} per call"
        )

    primary = sum(1 for f in frames if f.kind == "primary")
    extra = sum(1 for f in frames if f.kind == "extra")
    meta = RunMeta(
        range_spec=rs.header,
        transcript_source=transcript.get("source") or "none",
        speakers=None,
        vision=vision,
        frame_width=ctx.config.frame_width,
        completion=completion,
        completion_reason=reason,
        warnings=list(ctx.warnings),
        stats={
            "segments": len(segments),
            "windows": 0,
            "frames_primary": primary,
            "frames_extra": extra,
            "dropped_duplicates": 0,
        },
        generated_at=datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        tool_version=__version__,
        caption_track=captions.track,
        command_line=f"frameweave run {ctx.resolved.source}",
    )

    # Body headings only for chapters inside the window; header keeps the full list.
    window_chapters = [
        ch for ch in ctx.resolved.chapters if rs.start <= ch.start < rs.end
    ]
    write_transcript(
        out,
        replace(ctx.resolved, chapters=window_chapters),
        segments,
        frames,
        descriptions,
        meta,
    )
    _restore_chapters_header(out / "transcript.fwv", ctx.resolved)
    write_meta(out, ctx.resolved, frames, meta)
    cost = ctx.ledger.summary()
    if ctx.lane_actual is None:
        actual_path = ctx.run_dir / "lane_actual.json"
        if actual_path.is_file():
            try:
                ctx.lane_actual = json.loads(actual_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                ctx.lane_actual = None
    if ctx.lane_actual is not None:
        cost = {**cost, "lane_actual": ctx.lane_actual}
    write_cost(out, cost)
    meta_dict = json.loads((out / "meta.json").read_text(encoding="utf-8"))
    if ctx.lane_choice is not None:
        meta_dict["lane_choice"] = ctx.lane_choice
        temporary = out / ".meta.json.tmp"
        temporary.write_text(
            json.dumps(meta_dict, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        temporary.replace(out / "meta.json")
    write_readme(out, out / "transcript.fwv", meta_dict, cost)

    ctx.output_path = out.resolve()
    # Writer may rewrite completion around missing descriptions; frames-only keeps
    # the speech-not-requested wording the CLI exit path will read.
    ctx.completion = completion if ctx.frames_only else meta.completion
    _atomic_json(
        marker,
        {"output_path": str(ctx.output_path), "completion": ctx.completion},
    )
    return StageResult(stage="assemble", status="done", artifact=str(marker))


STAGES: list[Stage] = [
    Stage("resolve", _stage_resolve, (), "resolved.json", False),
    Stage("fetch_media", _stage_fetch_media, ("resolve",), "media.json", False),
    Stage("fetch_captions", _stage_fetch_captions, ("resolve",), "captions.json", False),
    Stage(
        "transcript",
        _stage_transcript,
        ("fetch_captions", "fetch_media"),
        "transcript.json",
        True,
    ),
    Stage("frames", _stage_frames, ("transcript", "fetch_media"), "frames.json", True),
    Stage(
        "describe",
        _stage_describe,
        ("frames", "transcript"),
        "descriptions.json",
        True,
    ),
    Stage(
        "assemble",
        _stage_assemble,
        ("describe", "frames", "transcript", "resolve"),
        "assembled.json",
        True,
    ),
]


# --- helpers ---


def _default_stt(config: Config) -> SttBackend:
    if config.stt_backend != "local":
        raise ConfigError(f"unsupported stt_backend: {config.stt_backend}")
    return WhisperXBackend()


def _load_captions(path: Path) -> CaptionsResult:
    if not path.is_file():
        return CaptionsResult([], "none", None, "captions missing", Usage())
    data = json.loads(path.read_text(encoding="utf-8"))
    segments = [Segment.from_dict(item) for item in data.get("segments") or []]
    return CaptionsResult(
        segments=segments,
        source=data.get("source") or "none",
        track=data.get("track"),
        reason=data.get("reason"),
        usage=Usage(),
    )


def _load_transcript(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    segments = [Segment.from_dict(item) for item in data.get("segments") or []]
    return {
        "segments": segments,
        "source": data.get("source") or "none",
        "silent": bool(data.get("silent")),
    }


def _load_frames(path: Path) -> list[Frame]:
    data = json.loads(path.read_text(encoding="utf-8"))
    return [Frame.from_dict(item) for item in data.get("frames") or []]


def _load_descriptions(path: Path) -> list[Description]:
    if not path.is_file():
        return []
    data = json.loads(path.read_text(encoding="utf-8"))
    return [Description.from_dict(item) for item in data.get("descriptions") or []]


def _media_file(source_dir: Path) -> Path:
    for path in sorted(source_dir.iterdir()):
        if not path.is_file():
            continue
        name = path.name
        if name.startswith("media.") and not name.endswith(".json") and ".partial" not in name:
            return path
    raise FileNotFoundError(f"no media.* in {source_dir}")


def _transcript_window(segments: list[Segment], batch: list[Frame]) -> str:
    if not batch:
        return ""
    start = min(frame.time for frame in batch)
    end = max(frame.time for frame in batch)
    parts = [
        seg.text
        for seg in segments
        if seg.end >= start and seg.start <= end
    ]
    text = " ".join(parts)
    if len(text) > _CONTEXT_CAP:
        return text[:_CONTEXT_CAP]
    return text


def _restore_chapters_header(path: Path, resolved: Resolved) -> None:
    """Rewrite ``chapters:`` to the whole-video list after a windowed body write."""
    if not resolved.chapters or not path.is_file():
        return
    rendered = "; ".join(
        f"{timecode.format(ch.start, tenths=False)} {ch.title.replace(';', ',')}"
        for ch in resolved.chapters
    )
    wanted = f"chapters: {rendered}"
    lines = path.read_text(encoding="utf-8").splitlines()
    out: list[str] = []
    found = False
    blank_at: int | None = None
    for index, line in enumerate(lines):
        if line.startswith("chapters:"):
            out.append(wanted)
            found = True
            continue
        if blank_at is None and line == "" and index > 0:
            blank_at = len(out)
        out.append(line)
    if not found:
        insert_at = blank_at if blank_at is not None else len(out)
        out.insert(insert_at, wanted)
    _atomic_write_text(path, "\n".join(out) + "\n")


def _atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temporary.write_text(text, encoding="utf-8")
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _atomic_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temporary.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _creatable_writable(path: Path) -> tuple[bool, str]:
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / f".write-probe-{os.getpid()}"
        probe.write_text("ok\n", encoding="utf-8")
        probe.unlink()
        return True, f"{path} is writable"
    except OSError as exc:
        return False, f"{path} not writable: {exc}"


def _install_handlers(ctx: RunContext) -> None:
    _ACTIVE["ctx"] = ctx
    previous = {
        signal.SIGINT: signal.getsignal(signal.SIGINT),
        signal.SIGTERM: signal.getsignal(signal.SIGTERM),
    }
    _ACTIVE["previous"] = previous

    def _handler(signum: int, _frame: object) -> None:
        _cleanup_on_signal(ctx)
        signal.signal(signum, previous.get(signum, signal.SIG_DFL))
        os.kill(os.getpid(), signum)

    signal.signal(signal.SIGINT, _handler)
    signal.signal(signal.SIGTERM, _handler)


def _clear_handlers() -> None:
    previous = _ACTIVE.pop("previous", None)
    _ACTIVE.pop("ctx", None)
    if previous:
        for sig, handler in previous.items():
            signal.signal(sig, handler)


def _cleanup_on_signal(ctx: RunContext) -> None:
    try:
        ctx.ledger.reconcile_run()
    except Exception:
        pass
    for root in (ctx.run_dir, ctx.source_dir):
        if not root.is_dir():
            continue
        for path in root.rglob("*"):
            if not path.is_file():
                continue
            name = path.name
            if (
                name.endswith(".partial")
                or name.endswith(".tmp.jpg")
                or name.endswith(".tmp")
                or ".tmp." in name
            ):
                path.unlink(missing_ok=True)


# Silence unused import warning for load (available for CLI collectors).
_ = load
