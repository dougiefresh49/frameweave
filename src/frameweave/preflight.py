"""Preflight checks for `frameweave doctor`.

Issue #15 wires the CLI: load a Config (without require_out), call
`run_doctor(config, env=os.environ, out=sys.stdout)` for the human table, or
when `--json` / `doctor_json` is set print `as_json(rows)` and exit with
`exit_code(rows)` (same int `run_doctor` returns). `cli_flags()` contributes
the `--json` FlagSpec.
"""

from __future__ import annotations

import importlib
import json
import os
import re
import shutil
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import TextIO

from frameweave.config import Check, Config, ConfigError, FlagSpec, require_out

MODULES: list[str] = [
    "frameweave.preflight",
    "frameweave.config",
    "frameweave.sources.youtube",
    "frameweave.sources.local",
    "frameweave.sources.http",
    "frameweave.captions",
    "frameweave.stt.local",
    "frameweave.stt.speakers",
    "frameweave.frames",
    "frameweave.vision.base",
    "frameweave.vision.choose",
    "frameweave.format.readme",
]

_LOCK_PATH = Path(__file__).resolve().parents[2] / "uv.lock"
_YT_DLP_BLOCK = re.compile(
    r'\[\[package\]\]\s+name\s*=\s*"yt-dlp"\s+version\s*=\s*"([^"]+)"',
    re.MULTILINE,
)
_GB = 1024**3

WhichFn = Callable[[str], str | None]
RunFn = Callable[..., subprocess.CompletedProcess[str]]


def cli_flags() -> list[FlagSpec]:
    return [
        FlagSpec(
            "--json",
            "doctor_json",
            bool,
            "Print doctor rows as a JSON list instead of the human table.",
        ),
    ]


def preflight_checks(config: Config) -> list[Check]:
    """Built-in rows from decision 2; used by collect like any other module."""
    return _builtin_checks(config, env=os.environ, which=shutil.which, run=subprocess.run)


def collect(
    config: Config,
    *,
    env: Mapping[str, str] | None = None,
    which: WhichFn = shutil.which,
    run: RunFn = subprocess.run,
    modules: Sequence[str] | None = None,
) -> list[Check]:
    """Import every MODULES entry and merge its preflight_checks; missing modules skip.

    Pass `modules` to restrict which entries are collected (tests use this so real
    module rows cannot override builtins or call the machine's which/run).
    """
    process_env: Mapping[str, str] = os.environ if env is None else env
    by_name: dict[str, Check] = {}
    for modname in MODULES if modules is None else modules:
        try:
            if modname == "frameweave.preflight":
                checks = _builtin_checks(
                    config, env=process_env, which=which, run=run
                )
            else:
                module = importlib.import_module(modname)
                fn = getattr(module, "preflight_checks", None)
                if fn is None:
                    continue
                checks = fn(config)
        except ModuleNotFoundError as exc:
            if _is_missing_target(modname, exc):
                continue
            by_name[modname] = Check(
                modname, False, str(exc), f"repair import for {modname}"
            )
            continue
        except Exception as exc:  # noqa: BLE001 — doctor row, not a crash
            by_name[modname] = Check(
                modname, False, str(exc), f"repair {modname}"
            )
            continue
        for check in checks:
            by_name[check.name] = check
    # Doctor never calls require_out; still emit the ConfigError text as the row.
    if config.out is None:
        message = _missing_out_message(config)
        by_name["output root"] = Check(
            "output root",
            False,
            message,
            message,
        )
    return list(by_name.values())


def exit_code(rows: Sequence[Check]) -> int:
    """Return 1 if any required row failed, else 0.

    Used by `run_doctor` and by issue #15 for `--json` mode (print
    `as_json(rows)`, then exit with this value).
    """
    return 1 if any((not row.ok) and row.required for row in rows) else 0


def as_json(rows: Sequence[Check]) -> str:
    payload = [
        {
            "name": row.name,
            "ok": row.ok,
            "detail": row.detail,
            "remedy": row.remedy,
            "required": row.required,
        }
        for row in rows
    ]
    return json.dumps(payload)


def run_doctor(
    config: Config,
    *,
    env: Mapping[str, str],
    which: WhichFn = shutil.which,
    run: RunFn = subprocess.run,
    out: TextIO,
    modules: Sequence[str] | None = None,
) -> int:
    """Print one row per check and return `exit_code(rows)`."""
    rows = collect(config, env=env, which=which, run=run, modules=modules)
    failed = 0
    warnings = 0
    for row in rows:
        if row.ok:
            status = "ok"
        elif row.required:
            status = "FAIL"
            failed += 1
        else:
            status = "warn"
            warnings += 1
        print(f"{status} {row.name} {row.detail}", file=out)
        if not row.ok:
            print(f"  {row.remedy}", file=out)
    print(
        f"doctor: {len(rows)} checks, {failed} failed, {warnings} warnings",
        file=out,
    )
    return exit_code(rows)


def _missing_out_message(config: Config) -> str:
    try:
        require_out(config)
    except ConfigError as exc:
        return str(exc)
    raise AssertionError("require_out must raise when config.out is None")


def _is_missing_target(modname: str, exc: ModuleNotFoundError) -> bool:
    missing = exc.name
    if missing is None:
        return False
    return missing == modname or modname.startswith(f"{missing}.")


def _builtin_checks(
    config: Config,
    *,
    env: Mapping[str, str],
    which: WhichFn,
    run: RunFn,
) -> list[Check]:
    rows: list[Check] = [
        _binary_version_check("ffmpeg", which, run),
        _binary_version_check("ffprobe", which, run),
        _yt_dlp_check(),
        _python_check(),
        _disk_check(config),
        _env_key_check(
            "GEMINI_API_KEY",
            env,
            required=config.vision_lane == "gemini",
            remedy="export GEMINI_API_KEY=... (Gemini vision lane)",
        ),
    ]
    if config.vision_lane in {"auto", "claude"}:
        rows.append(_claude_check(which, run))
    if config.vision_lane == "codex":
        rows.append(
            _path_check(
                "codex",
                which,
                remedy="Install Codex CLI and ensure it is on PATH",
            )
        )
    rows.append(
        _env_key_check(
            "HF_TOKEN",
            env,
            required=config.speakers,
            remedy="export HF_TOKEN=... (required for speaker diarization)",
        )
    )
    return rows


def _binary_version_check(name: str, which: WhichFn, run: RunFn) -> Check:
    path = which(name)
    if not path:
        return Check(name, False, "not found on PATH", "brew install ffmpeg")
    try:
        completed = run(
            [path, "-version"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return Check(name, False, str(exc), "brew install ffmpeg")
    first = (completed.stdout or completed.stderr or "").splitlines()
    line = first[0] if first else ""
    version = _parse_ffmpeg_version(line)
    if completed.returncode != 0 and not version:
        return Check(
            name, False, line or f"{name} -version failed", "brew install ffmpeg"
        )
    return Check(name, True, version or line or path, "brew install ffmpeg")


def _parse_ffmpeg_version(line: str) -> str:
    marker = "version "
    idx = line.find(marker)
    if idx < 0:
        return ""
    rest = line[idx + len(marker) :].strip()
    if not rest:
        return ""
    return rest.split()[0]


def _yt_dlp_check(*, lock_path: Path = _LOCK_PATH) -> Check:
    pin = _yt_dlp_pin(lock_path)
    try:
        import yt_dlp.version as ytdlp_version  # noqa: PLC0415 — optional until sync

        installed = ytdlp_version.__version__
    except Exception as exc:  # noqa: BLE001
        return Check("yt-dlp", False, str(exc), "uv sync")
    if not pin:
        return Check(
            "yt-dlp",
            True,
            f"{installed} (lock pin unavailable)",
            "uv sync",
        )
    if _version_tuple(installed) < _version_tuple(pin):
        return Check(
            "yt-dlp",
            False,
            f"{installed} (lock pin {pin})",
            "uv sync",
        )
    return Check("yt-dlp", True, f"{installed} (lock pin {pin})", "uv sync")


def _yt_dlp_pin(lock_path: Path) -> str:
    try:
        text = lock_path.read_text(encoding="utf-8")
    except OSError:
        return ""
    match = _YT_DLP_BLOCK.search(text)
    return match.group(1) if match else ""


def _version_tuple(text: str) -> tuple[int, ...]:
    parts: list[int] = []
    for piece in re.split(r"[^\d]+", text):
        if piece.isdigit():
            parts.append(int(piece))
    return tuple(parts) if parts else (0,)


def _python_check() -> Check:
    version = (
        f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    )
    ok = sys.version_info >= (3, 12)
    return Check(
        "python",
        ok,
        version,
        "Install Python 3.12 or newer (uv python install 3.12)",
    )


def _disk_check(config: Config) -> Check:
    paths: list[Path] = [config.cache_dir]
    if config.out is not None:
        paths.insert(0, config.out)
    free_parts: list[str] = []
    low = False
    seen_devices: set[int] = set()
    for path in paths:
        probe = path if path.exists() else _existing_ancestor(path)
        try:
            usage = shutil.disk_usage(probe)
            device = probe.stat().st_dev if probe.exists() else id(probe)
        except OSError as exc:
            return Check(
                "disk",
                False,
                str(exc),
                "frameweave cache prune",
                required=False,
            )
        if device in seen_devices:
            continue
        seen_devices.add(device)
        free_gb = usage.free / _GB
        free_parts.append(f"{probe}: {free_gb:.1f} GB free")
        if free_gb < config.disk_warn_gb:
            low = True
    cache_bytes = _dir_size(config.cache_dir)
    cache_gb = cache_bytes / _GB
    detail = f"{'; '.join(free_parts)}; cache {cache_gb:.2f} GB"
    if low:
        detail = f"below {config.disk_warn_gb} GB threshold; {detail}"
    return Check(
        "disk",
        not low,
        detail,
        "frameweave cache prune",
        required=False,
    )


def _existing_ancestor(path: Path) -> Path:
    probe = path
    while not probe.exists():
        if probe.parent == probe:
            return probe
        probe = probe.parent
    return probe


def _dir_size(path: Path) -> int:
    if not path.is_dir():
        return 0
    total = 0
    for root, _dirs, files in os.walk(path):
        for name in files:
            try:
                total += (Path(root) / name).stat().st_size
            except OSError:
                continue
    return total


def _env_key_check(
    key: str,
    env: Mapping[str, str],
    *,
    required: bool,
    remedy: str,
) -> Check:
    present = bool(env.get(key))
    detail = "set" if present else "unset"
    return Check(key, present, detail, remedy, required=required)


def _claude_check(which: WhichFn, run: RunFn) -> Check:
    path = which("claude")
    if not path:
        return Check(
            "claude",
            False,
            "not found on PATH",
            "Install Claude Code CLI and ensure it is on PATH",
        )
    try:
        completed = run(
            [path, "--version"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return Check(
            "claude",
            False,
            str(exc),
            "Install Claude Code CLI and ensure it is on PATH",
        )
    text = (completed.stdout or completed.stderr or "").strip().splitlines()
    detail = text[0] if text else path
    ok = completed.returncode == 0
    return Check(
        "claude",
        ok,
        detail if ok else detail or "claude --version failed",
        "Install Claude Code CLI and ensure it is on PATH",
    )


def _path_check(name: str, which: WhichFn, *, remedy: str) -> Check:
    path = which(name)
    if not path:
        return Check(name, False, "not found on PATH", remedy)
    return Check(name, True, path, remedy)
