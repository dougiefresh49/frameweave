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
from frameweave.vision.base import VisionBackend, make_backend

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
    range_spec: str
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
) -> RunOutcome:
    """Execute the stage registry and return the assembled output path."""
    progress_fn = progress or _progress_noop
    redo_set = _cascade_redo(set(redo))
    config = _with_resolved_lane(config)
    # Fail before any stage spends quota when the output root is missing.
    require_out(config)
    stt = _prepare_stt(config, stt)
    reconcile(config.cache_dir)

    picked = source or _pick_source(raw_input)
    resolved = picked.resolve(raw_input)
    source_dir = Path(config.cache_dir) / "sources" / resolved.video_id
    source_dir.mkdir(parents=True, exist_ok=True)
    key = make_run_key(config, "full")
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
        range_spec="full",
        ledger=ledger,
        progress=progress_fn,
        redo=redo_set,
        frames_only=frames_only,
        stt=stt,
        vision=vision,
    )

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
    elif stage.name == "assemble":
        data = json.loads(_artifact_path(ctx, stage).read_text(encoding="utf-8"))
        ctx.output_path = Path(data["output_path"])
        ctx.completion = data.get("completion", ctx.completion)


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
        digests["settings"] = _transcript_settings_digest(ctx.config)
    if stage.name == "assemble":
        digests["out"] = str(require_out(ctx.config).resolve())
    return digests


def _transcript_settings_digest(config: Config) -> str:
    payload = {
        "stt_backend": config.stt_backend,
        "stt_model": config.stt_model,
        "stt_device": config.stt_device,
        "speakers": config.speakers,
        "captions_mode": config.captions_mode,
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


def _with_resolved_lane(config: Config) -> Config:
    if config.vision_lane != "auto":
        return config
    # Until #26, auto means claude.
    return replace(config, vision_lane="claude")


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

    # Assign ids if missing.
    numbered: list[Segment] = []
    for index, seg in enumerate(segments, start=1):
        if seg.id is None:
            numbered.append(replace(seg, id=f"s{index:04d}"))
        else:
            numbered.append(seg)

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
    planned = plan_frames(transcript["segments"], ctx.resolved.duration, ctx.config)
    frames = extract_frames(media, planned, ctx.run_dir, ctx.config)
    _atomic_json(
        path,
        {
            "frames": [f.to_dict() for f in frames],
            "windows": [[a, b] for a, b in planned.windows],
            "budget": planned.budget,
            "interval_s": planned.interval_s,
            "merged": planned.merged,
        },
    )
    return StageResult(stage="frames", status="done", artifact=str(path))


def _stage_describe(ctx: RunContext) -> StageResult:
    assert ctx.resolved is not None
    path = ctx.run_dir / "descriptions.json"
    frames = _load_frames(ctx.run_dir / "frames.json")
    transcript = _load_transcript(ctx.run_dir / "transcript.json")
    segments: list[Segment] = transcript["segments"]
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
    root = require_out(ctx.config)
    out = output_dir(
        root,
        channel_slug(ctx.resolved.channel, ctx.config, root),
        slugify(ctx.resolved.title),
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
        range_spec=ctx.range_spec,
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

    write_transcript(out, ctx.resolved, segments, frames, descriptions, meta)
    write_meta(out, ctx.resolved, frames, meta)
    cost = ctx.ledger.summary()
    write_cost(out, cost)
    meta_dict = json.loads((out / "meta.json").read_text(encoding="utf-8"))
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
