"""CLI: doctor, inspect, run, and cache size.

Collects ``cli_flags()`` from modules, wires subcommands, and never spends on
``inspect`` or ``--dry-run``.
"""

from __future__ import annotations

import argparse
import importlib
import json
import math
import os
import shutil
import sys
import traceback
from collections.abc import Mapping, Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any, TextIO

from frameweave.config import Config, ConfigError, FlagSpec, load, require_out
from frameweave.config import run_key as make_run_key
from frameweave.frames import plan as plan_frames
from frameweave.ledger import RATES
from frameweave.preflight import (
    MODULES,
    _dir_size,
    _existing_ancestor,
    as_json,
    exit_code,
    run_doctor,
)
from frameweave.sources.http import HttpSource
from frameweave.sources.local import LocalFileSource
from frameweave.sources.youtube import SourceBusy, YouTubeSource
from frameweave.stt import SttError
from frameweave.types import Resolved, Segment, Source
from frameweave.util import timecode
from frameweave.util.media import NotMediaError
from frameweave.util.retry import RequestTimeout
from frameweave.vision import VisionFailed

# Spike token model (decision 48): tokens = calls * 3997 + frames * 2034.
# #26 moves these into config/lanes.toml.
_SPIKE_TOKENS_PER_CALL = (3997, 2034)
_OUTPUT_TOKENS_PER_FRAME = 120
_GB = 1024**3
_SUBSCRIPTION_LANES = frozenset({"claude", "codex"})
# Dest names that are CLI/pipeline control, not Config fields.
_CLI_ONLY_DESTS = frozenset({"debug", "dry_run", "doctor_json", "redo", "frames_only", "out"})


def cli_flags() -> list[FlagSpec]:
    return [
        FlagSpec("--debug", "debug", bool, "Print a traceback on errors."),
        FlagSpec("--dry-run", "dry_run", bool, "Print the inspect block and exit (run only)."),
        FlagSpec(
            "--out",
            "out",
            Path,
            "Output folder for this run. Used exactly, no extra level.",
        ),
    ]


def preflight_checks(config: Config) -> list:
    del config
    return []


def main(
    argv: list[str] | None = None,
    *,
    backends: Mapping[str, Any] | None = None,
    env: Mapping[str, str] | None = None,
    toml_path: Path | None = None,
    dotenv_paths: Sequence[Path] | None = None,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
) -> int:
    """Parse argv and dispatch. Returns a process exit code."""
    out = stdout or sys.stdout
    err = stderr or sys.stderr
    args_list = list(sys.argv[1:] if argv is None else argv)
    parser = _build_parser()
    try:
        args = parser.parse_args(args_list)
    except SystemExit as exc:
        # Usage errors must not collide with exit 2 (incomplete speech).
        code = exc.code
        if code is None or code == 0:
            return 0
        return 1

    load_kwargs: dict[str, Any] = {}
    if env is not None:
        load_kwargs["env"] = env
    if toml_path is not None:
        load_kwargs["toml_path"] = toml_path
    if dotenv_paths is not None:
        load_kwargs["dotenv_paths"] = dotenv_paths

    try:
        if args.command == "doctor":
            return _cmd_doctor(args, load_kwargs, out)
        if args.command == "inspect":
            return _cmd_inspect(args, load_kwargs, backends, out, err)
        if args.command == "run":
            return _cmd_run(args, load_kwargs, backends, out, err)
        if args.command == "cache":
            return _cmd_cache(args, load_kwargs, out)
    except Exception as exc:
        return _handle_error(exc, getattr(args, "debug", False), err)

    parser.print_help(err)
    return 1


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="frameweave")
    parser.add_argument("--quiet", action="store_true", default=False, help="Hide progress lines.")
    parser.add_argument(
        "--debug",
        action="store_true",
        default=False,
        help="Print a traceback on errors.",
    )

    sub = parser.add_subparsers(dest="command", required=True)

    doctor = sub.add_parser("doctor", help="Check binaries, keys, and the output root.")
    doctor.add_argument(
        "--json",
        dest="doctor_json",
        action="store_true",
        default=False,
        help="Print doctor rows as JSON.",
    )
    # SUPPRESS so a global --debug before the subcommand is not overwritten.
    doctor.add_argument("--debug", action="store_true", default=argparse.SUPPRESS)

    inspect = sub.add_parser("inspect", help="Resolve input and print the pre-spend estimate.")
    inspect.add_argument("input", help="YouTube URL, media URL, or local file.")
    inspect.add_argument("--quiet", action="store_true", default=argparse.SUPPRESS)
    inspect.add_argument("--debug", action="store_true", default=argparse.SUPPRESS)
    _attach_flags(inspect, include_dry_run=False)

    run = sub.add_parser("run", help="Run the pipeline and write the output folder.")
    run.add_argument("input", help="YouTube URL, media URL, or local file.")
    run.add_argument("--quiet", action="store_true", default=argparse.SUPPRESS)
    run.add_argument("--debug", action="store_true", default=argparse.SUPPRESS)
    _attach_flags(run, include_dry_run=True)

    cache = sub.add_parser("cache", help="Cache utilities.")
    cache_sub = cache.add_subparsers(dest="cache_command")
    cache_sub.add_parser("size", help="Print the total cache size.")

    return parser


def _attach_flags(parser: argparse.ArgumentParser, *, include_dry_run: bool) -> None:
    for spec in _collect_flags():
        if spec.name in {"--json", "--debug", "--quiet"}:
            continue
        if spec.dest in {"doctor_json", "debug", "quiet"}:
            continue
        if spec.dest == "dry_run" and not include_dry_run:
            continue
        _add_flag(parser, spec)


def _collect_flags() -> list[FlagSpec]:
    """Merge module flags; first occurrence of each ``name`` wins; config first."""
    by_name: dict[str, FlagSpec] = {}
    for modname in _flag_modules():
        try:
            module = importlib.import_module(modname)
        except ModuleNotFoundError:
            continue
        fn = getattr(module, "cli_flags", None)
        if fn is None:
            continue
        for spec in fn():
            if spec.name not in by_name:
                by_name[spec.name] = spec
    return list(by_name.values())


def _flag_modules() -> list[str]:
    # config first for dedup; pipeline + this module are not in MODULES but own flags.
    ordered = ["frameweave.config"]
    for name in MODULES:
        if name not in ordered:
            ordered.append(name)
    for name in ("frameweave.pipeline", "frameweave.cli"):
        if name not in ordered:
            ordered.append(name)
    return ordered


def _add_flag(parser: argparse.ArgumentParser, spec: FlagSpec) -> None:
    kwargs: dict[str, Any] = {"dest": spec.dest, "help": spec.help, "default": None}
    if spec.dest == "redo":
        kwargs["action"] = "append"
        kwargs["type"] = str
    elif spec.type is bool:
        kwargs["action"] = "store_true"
    else:
        kwargs["type"] = spec.type  # type: ignore[arg-type]
    parser.add_argument(spec.name, **kwargs)


def _flags_dict(args: argparse.Namespace) -> dict[str, Any]:
    """Pass every collected dest except CLI-only; ``config.load`` rejects unknowns."""
    flags: dict[str, Any] = {}
    for spec in _collect_flags():
        if spec.dest in _CLI_ONLY_DESTS:
            continue
        flags[spec.dest] = getattr(args, spec.dest, None)
    return flags


def _process_env(load_kwargs: dict[str, Any]) -> Mapping[str, str]:
    # Honour an injected empty mapping; only fall back when env was not passed.
    if "env" in load_kwargs:
        return load_kwargs["env"]
    return os.environ


def _cmd_doctor(args: argparse.Namespace, load_kwargs: dict[str, Any], out: TextIO) -> int:
    config = load(flags={}, **load_kwargs)
    process_env = _process_env(load_kwargs)
    if getattr(args, "doctor_json", False):
        from frameweave.preflight import collect

        rows = collect(config, env=process_env)
        print(as_json(rows), file=out)
        return exit_code(rows)
    return run_doctor(config, env=process_env, out=out)


def _cmd_inspect(
    args: argparse.Namespace,
    load_kwargs: dict[str, Any],
    backends: Mapping[str, Any] | None,
    out: TextIO,
    err: TextIO,
) -> int:
    del err
    config = load(flags=_flags_dict(args), **load_kwargs)
    _warn_low_disk(config, out)
    source = _pick_source(args.input, backends)
    resolved = source.resolve(args.input)
    _write_resolved(config, resolved)
    _print_estimate(resolved, config, out)
    return 0


def _cmd_run(
    args: argparse.Namespace,
    load_kwargs: dict[str, Any],
    backends: Mapping[str, Any] | None,
    out: TextIO,
    err: TextIO,
) -> int:
    from frameweave import pipeline

    config = load(flags=_flags_dict(args), **load_kwargs)
    _warn_low_disk(config, out)

    out_override = getattr(args, "out", None)
    if out_override is not None:
        out_override = Path(out_override)
    else:
        require_out(config)

    source = _pick_source(args.input, backends)
    resolved = source.resolve(args.input)
    _write_resolved(config, resolved)

    dry_run = bool(getattr(args, "dry_run", False))
    if dry_run:
        _print_estimate(resolved, config, out)
        return 0

    quiet = bool(getattr(args, "quiet", False))
    frames_only = bool(getattr(args, "frames_only", False))
    redo = getattr(args, "redo", None) or ()

    printed_estimate = False

    def progress(msg: str) -> None:
        nonlocal printed_estimate
        if msg.startswith("stage resolve:"):
            if not printed_estimate:
                _print_estimate(resolved, config, out)
                printed_estimate = True
            if not quiet:
                print(msg, file=out)
            return
        if not quiet:
            print(msg, file=out)
        if msg.startswith("stage frames:"):
            _print_frames_line(config, resolved.video_id, out)

    try:
        outcome = pipeline.run(
            args.input,
            config,
            progress=progress,
            redo=redo,
            frames_only=frames_only,
            source=source,
            stt=backends.get("stt") if backends else None,
            vision=backends.get("vision") if backends else None,
            out_override=out_override,
            resolved=resolved,
        )
    except Exception as exc:
        return _handle_error(exc, bool(getattr(args, "debug", False)), err)

    if not printed_estimate:
        _print_estimate(resolved, config, out)

    print(str(outcome.output_path.resolve()), file=out)
    print(_format_cost(outcome.cost_usd, config.vision_lane), file=out)

    if outcome.completion.startswith("incomplete") and not frames_only:
        return 2
    return 0


def _cmd_cache(args: argparse.Namespace, load_kwargs: dict[str, Any], out: TextIO) -> int:
    if getattr(args, "cache_command", None) != "size":
        print("frameweave cache: only 'size' is available; prune is issue #23", file=out)
        return 1
    config = load(flags={}, **load_kwargs)
    size = _dir_size(config.cache_dir)
    print(f"cache: {_format_bytes(size)} ({config.cache_dir})", file=out)
    return 0


def _pick_source(raw_input: str, backends: Mapping[str, Any] | None) -> Source:
    if backends and backends.get("source") is not None:
        return backends["source"]  # type: ignore[return-value]
    for candidate in (YouTubeSource(), HttpSource(), LocalFileSource()):
        if candidate.matches(raw_input):
            return candidate
    raise ValueError(f"no source matches input: {raw_input!r}")


def _write_resolved(config: Config, resolved: Resolved) -> None:
    source_dir = Path(config.cache_dir) / "sources" / resolved.video_id
    source_dir.mkdir(parents=True, exist_ok=True)
    path = source_dir / "resolved.json"
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temporary.write_text(
            json.dumps(resolved.to_dict(), indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _print_estimate(resolved: Resolved, config: Config, out: TextIO) -> None:
    lane = config.vision_lane if config.vision_lane != "auto" else "claude"
    duration = timecode.format(resolved.duration, tenths=False)
    captions = (
        "yes"
        if resolved.has_captions is True
        else ("no" if resolved.has_captions is False else "unknown")
    )
    published = resolved.published or "unknown"
    chapters = len(resolved.chapters)

    # Upper bound: one synthetic segment spanning the duration (interval-only plan).
    synthetic = [Segment(0.0, resolved.duration, "", "none")]
    planned = plan_frames(synthetic, resolved.duration, config)
    frames = len(planned.frames)
    per_call = max(1, int(config.frames_per_call))
    calls = math.ceil(frames / per_call) if frames else 0
    fixed, per_frame = _SPIKE_TOKENS_PER_CALL
    tokens = calls * fixed + frames * per_frame

    print(f"title: {resolved.title}", file=out)
    print(f"channel: {resolved.channel}", file=out)
    print(f"duration: {duration}", file=out)
    print(f"published: {published}", file=out)
    print(f"captions: {captions}", file=out)
    print(f"chapters: {chapters}", file=out)
    print(f"frames upper bound: {frames}", file=out)
    print(f"vision lane: {lane}", file=out)
    print(f"tokens (est): {tokens} ({calls} calls, {frames} frames)", file=out)
    # TODO(#26): per-lane projection table (tokens, quota windows, dollars).
    if lane == "none":
        return
    if lane == "gemini":
        model = config.vision_model.get("gemini", "gemini-3.5-flash-lite")
        rate_in, rate_out = RATES.get(model, RATES.get("gemini-3.5-flash-lite", (0.30, 2.50)))
        est_out = frames * _OUTPUT_TOKENS_PER_FRAME
        dollars = tokens * rate_in / 1e6 + est_out * rate_out / 1e6
        print(f"dollars (est): ${dollars:.4f} ({model})", file=out)
    else:
        print("dollars (est): $0 (subscription)", file=out)


def _print_frames_line(config: Config, video_id: str, out: TextIO) -> None:
    # Match the pipeline's run key (auto → claude until #26).
    keyed = config if config.vision_lane != "auto" else replace(config, vision_lane="claude")
    try:
        key = make_run_key(keyed, "full")
    except ConfigError:
        return
    path = Path(config.cache_dir) / "runs" / video_id / key / "frames.json"
    if not path.is_file():
        return
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    frames = data.get("frames") or []
    primary = sum(1 for frame in frames if frame.get("kind") == "primary")
    extra = sum(1 for frame in frames if frame.get("kind") == "extra")
    print(f"frames: {len(frames)} primary {primary} extra {extra}", file=out)


def _format_cost(cost_usd: float, vision_lane: str) -> str:
    lane = "claude" if vision_lane == "auto" else vision_lane
    if lane in _SUBSCRIPTION_LANES:
        return "cost: $0.000 (subscription)"
    return f"cost: ${cost_usd:.3f}"


def _warn_low_disk(config: Config, out: TextIO) -> None:
    probe_root = config.out if config.out is not None else config.cache_dir
    probe = probe_root if probe_root.exists() else _existing_ancestor(probe_root)
    try:
        free_gb = shutil.disk_usage(probe).free / _GB
    except OSError:
        return
    if free_gb >= config.disk_warn_gb:
        return
    cache_gb = _dir_size(config.cache_dir) / _GB
    print(
        f"warning: free disk {free_gb:.1f} GB under {config.disk_warn_gb} GB "
        f"(cache {cache_gb:.2f} GB); frameweave cache prune",
        file=out,
    )


def _format_bytes(size: int) -> str:
    if size < 1024:
        return f"{size} B"
    value = float(size)
    for unit in ("KiB", "MiB", "GiB", "TiB"):
        value /= 1024.0
        if value < 1024.0:
            return f"{value:.2f} {unit}"
    return f"{value:.2f} PiB"


def _handle_error(exc: BaseException, debug: bool, err: TextIO) -> int:
    stage = _stage_for_error(exc)
    remedy = _remedy_for_error(exc)
    parts = [f"frameweave: {stage}: {exc}"]
    if remedy and remedy not in str(exc):
        parts.append(remedy)
    print(" ".join(parts), file=err)
    if debug:
        traceback.print_exc(file=err)
    return 1


def _stage_for_error(exc: BaseException) -> str:
    if isinstance(exc, ConfigError):
        return "config"
    if isinstance(exc, (SourceBusy, NotMediaError)):
        return "fetch_media"
    if isinstance(exc, SttError):
        return "transcript"
    if isinstance(exc, VisionFailed):
        return "describe"
    if isinstance(exc, RequestTimeout):
        return "provider"
    text = str(exc)
    if text.startswith("stage ") and " failed" in text:
        return text.removeprefix("stage ").removesuffix(" failed").strip() or "run"
    return "run"


def _remedy_for_error(exc: BaseException) -> str | None:
    if isinstance(exc, RequestTimeout):
        return None  # message already names the three remedies
    if isinstance(exc, SourceBusy):
        return "Wait for the other process to finish, or remove the stale claim."
    if isinstance(exc, NotMediaError):
        return "Download the file in your browser and pass the local path."
    if isinstance(exc, ConfigError):
        return None
    return None
