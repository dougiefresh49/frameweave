"""Frozen run config, output paths, and the seams later CLI and doctor collect."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import tomllib
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from dotenv import dotenv_values

from frameweave.util import timecode

# FlagSpec and Check live here until issue #3's types.py re-homes them.

_LANES = frozenset({"auto", "codex", "claude", "gemini", "none"})
_QUALITY = frozenset({"standard", "high"})
_BOOL_TRUE = frozenset({"1", "true", "yes"})
_BOOL_FALSE = frozenset({"0", "false", "no"})
_NON_ALNUM = re.compile(r"[^a-z0-9]+")
_SLUG_LIMIT = 80
_SLUG_TAIL = 20
_ENV_PREFIX = "FRAMEWEAVE_"
_VISION_MODEL_ENV = "FRAMEWEAVE_VISION_MODEL_"
_MISSING_OUT = (
    "FRAMEWEAVE_OUT is not set. Add this line to .env (or export it): "
    "FRAMEWEAVE_OUT=/path/to/output/folder"
)
_VISION_MODEL_DEFAULTS = {
    "codex": "gpt-5.6-sol",
    "claude": "sonnet",
    "gemini": "gemini-3.5-flash-lite",
}
_RUN_KEY_FIELDS = (
    "vision_lane",
    "vision_model",
    "vision_quality",
    "frames_per_call",
    "frame_interval_s",
    "frame_width",
    "max_frames",
    "stt_backend",
    "stt_model",
    "speakers",
    "prompt_revision",
    "glossary",
)


class ConfigError(Exception):
    """Invalid config key or value. The message names the key and the source."""


@dataclass(frozen=True)
class FlagSpec:
    name: str
    dest: str
    type: object
    help: str
    default: object = None


@dataclass(frozen=True)
class Check:
    name: str
    ok: bool
    detail: str
    remedy: str
    required: bool = True


@dataclass(frozen=True)
class Config:
    out: Path | None
    cache_dir: Path
    vision_lane: str
    vision_model: dict[str, str]
    vision_quality: str
    frames_per_call: int
    frame_interval_s: float
    frame_width: int
    max_frames: int | None
    stt_backend: str
    stt_model: str
    stt_device: str
    speakers: bool
    timeout_s: float
    concurrency: int
    glossary: Path | None
    lane_skip_percent: int
    disk_warn_gb: int
    prompt_revision: str
    channels: dict[str, str]
    captions_mode: str  # auto | manual | none (issue #8)
    vision_effort: str  # codex reasoning effort for frames: low | medium (issue #11)
    # None → chooser module defaults (AgentUsageBar path / fleet refresh script).
    usage_snapshot: Path | None
    usage_refresh_script: Path | None
    keep_duplicates: bool  # near-duplicate suppression off (issue #22)


_KINDS: dict[str, str] = {
    "out": "opt_path",
    "cache_dir": "path",
    "vision_lane": "lane",
    "vision_model": "vision_model",
    "vision_quality": "quality",
    "frames_per_call": "int",
    "frame_interval_s": "time",
    "frame_width": "int",
    "max_frames": "opt_int",
    "stt_backend": "str",
    "stt_model": "str",
    "stt_device": "str",
    "speakers": "bool",
    "timeout_s": "time",
    "concurrency": "int",
    "glossary": "opt_path",
    "lane_skip_percent": "int",
    "disk_warn_gb": "int",
    "prompt_revision": "str",
    "channels": "channels",
    "captions_mode": "str",
    "vision_effort": "str",
    "usage_snapshot": "opt_path",
    "usage_refresh_script": "opt_path",
    "keep_duplicates": "bool",
}


def _defaults() -> dict[str, object]:
    return {
        "out": None,
        "cache_dir": Path.home() / "Library" / "Caches" / "frameweave",
        "vision_lane": "auto",
        "vision_model": dict(_VISION_MODEL_DEFAULTS),
        "vision_quality": "standard",
        "frames_per_call": 8,
        "frame_interval_s": 45.0,
        "frame_width": 1280,
        "max_frames": None,
        "stt_backend": "local",
        "stt_model": "large-v3-turbo",
        "stt_device": "cpu",
        "speakers": False,
        "timeout_s": 120.0,
        "concurrency": 2,
        "glossary": None,
        "lane_skip_percent": 90,
        "disk_warn_gb": 10,
        "prompt_revision": "1",
        "channels": {},
        "captions_mode": "auto",
        "vision_effort": "low",
        "usage_snapshot": None,
        "usage_refresh_script": None,
        "keep_duplicates": False,
    }


def load(
    flags: dict | None = None,
    *,
    env: Mapping[str, str] | None = None,
    toml_path: Path | None = None,
    dotenv_paths: Sequence[Path] | None = None,
) -> Config:
    """Build Config. Keyword args let tests inject every source and skip real home."""
    if dotenv_paths is None:
        dotenv_paths = (
            Path(".env"),
            Path.home() / ".config" / "frameweave" / ".env",
        )
    if toml_path is None:
        toml_path = Path.home() / ".config" / "frameweave" / "config.toml"
    process_env: Mapping[str, str] = os.environ if env is None else env
    values = _defaults()
    _apply_toml(values, toml_path)
    layer = _env_layer(process_env, dotenv_paths)
    _apply_env(values, layer)
    if env is None:
        _export_provider_keys(layer, process_env)
    _apply_flags(values, flags or {})
    return Config(**values)  # type: ignore[arg-type]


def require_out(config: Config) -> Path:
    if config.out is None:
        raise ConfigError(_MISSING_OUT)
    return config.out


def slugify(text: str) -> str:
    normalized = unicodedata.normalize("NFKD", text)
    ascii_text = normalized.encode("ascii", "ignore").decode("ascii").lower()
    slug = _NON_ALNUM.sub("-", ascii_text).strip("-")
    slug = _cap_slug(slug)
    return slug or "untitled"


def channel_slug(name: str, config: Config, root: Path) -> str:
    if name in config.channels:
        return config.channels[name]
    wanted = slugify(name)
    if root.is_dir():
        if (root / wanted).is_dir():
            return wanted
        compact = wanted.replace("-", "")
        for path in root.iterdir():
            if path.is_dir() and path.name.replace("-", "") == compact:
                return path.name
    return wanted


def output_dir(
    root: Path,
    channel_slug: str,
    video_slug: str,
    range_slug: str | None = None,
    out_override: Path | None = None,
) -> Path:
    if out_override is not None:
        return out_override
    path = root / channel_slug / video_slug
    if range_slug:
        path = path / range_slug
    return path


def run_key(config: Config, range_spec: str) -> str:
    if config.vision_lane == "auto":
        raise ConfigError(
            "run_key needs a resolved vision lane; auto is chosen at run time by the lane chooser"
        )
    glossary: str | None = None
    if config.glossary is not None:
        try:
            glossary = hashlib.sha256(config.glossary.read_bytes()).hexdigest()
        except OSError as exc:
            raise ConfigError(
                f"unparseable value for glossary from flag: {config.glossary!r}"
            ) from exc
    payload = {
        "vision_lane": config.vision_lane,
        "vision_model": config.vision_model.get(config.vision_lane),
        "vision_quality": config.vision_quality,
        "frames_per_call": config.frames_per_call,
        "frame_interval_s": config.frame_interval_s,
        "frame_width": config.frame_width,
        "max_frames": config.max_frames,
        "stt_backend": config.stt_backend,
        "stt_model": config.stt_model,
        "speakers": config.speakers,
        "prompt_revision": config.prompt_revision,
        "glossary": glossary,
        "range": range_spec,
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:12]


def cli_flags() -> list[FlagSpec]:
    return [
        FlagSpec("--out", "out", Path, "Output folder for this run. Used exactly, no extra level."),
        FlagSpec("--vision", "vision_lane", str, "Vision lane: auto, codex, claude, gemini, none."),
        FlagSpec("--vision-quality", "vision_quality", str, "Vision detail: standard or high."),
        FlagSpec("--frames-per-call", "frames_per_call", int, "Frames sent in one vision call."),
        FlagSpec("--frame-interval", "frame_interval_s", float, "Extra-frame interval in seconds."),
        FlagSpec(
            "--max-frames",
            "max_frames",
            int,
            "Frame budget cap. Unset uses the duration rule.",
        ),
        FlagSpec("--speakers", "speakers", bool, "Turn on speaker labels."),
        FlagSpec("--timeout", "timeout_s", float, "Per-call timeout in seconds."),
        FlagSpec("--glossary", "glossary", Path, "Glossary file whose contents enter the run key."),
        FlagSpec(
            "--keep-duplicates",
            "keep_duplicates",
            bool,
            "Keep near-duplicate frames; skip difference-hash suppression.",
        ),
    ]


def preflight_checks(config: Config) -> list[Check]:
    if config.out is None:
        out_check = Check("output root", False, "FRAMEWEAVE_OUT is unset", _MISSING_OUT)
    else:
        ok, detail = _writable_existing_dir(config.out)
        out_check = Check(
            "output root",
            ok,
            detail,
            f"Create a writable directory and set FRAMEWEAVE_OUT={config.out}",
        )
    cache_ok, cache_detail = _creatable_dir(config.cache_dir)
    cache_check = Check(
        "cache dir",
        cache_ok,
        cache_detail,
        f"Choose a writable cache dir (FRAMEWEAVE_CACHE_DIR={config.cache_dir})",
    )
    return [out_check, cache_check]


def _cap_slug(slug: str) -> str:
    if len(slug) <= _SLUG_LIMIT:
        return slug
    window = slug[:_SLUG_LIMIT]
    if "-" in window[-_SLUG_TAIL:]:
        window = window[: window.rfind("-")]
    return window.strip("-")


# Provider credentials read by the vision and speaker backends straight from
# os.environ. A `.env` value for one of these is exported into the process when
# the process has no value of its own, so the keys can live in `.env` alongside
# FRAMEWEAVE_OUT (decision 54). Every other non-FRAMEWEAVE_* key stays put.
PROVIDER_KEYS: tuple[str, ...] = ("GEMINI_API_KEY", "HF_TOKEN")


def _export_provider_keys(layer: Mapping[str, str], process_env: Mapping[str, str]) -> None:
    for key in PROVIDER_KEYS:
        if key in process_env or key not in layer:
            continue
        os.environ[key] = layer[key]


def _env_layer(
    process_env: Mapping[str, str],
    dotenv_paths: Sequence[Path],
) -> dict[str, str]:
    merged: dict[str, str] = {}
    for path in dotenv_paths:
        parsed = dotenv_values(path)
        for key, value in parsed.items():
            if value is None or key in merged:
                continue
            merged[key] = value
    for key, value in process_env.items():
        merged[key] = value
    return merged


def _apply_toml(values: dict[str, object], toml_path: Path) -> None:
    if not toml_path.is_file():
        return
    try:
        with toml_path.open("rb") as handle:
            data = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ConfigError(f"unparseable value for {toml_path} from toml: {exc}") from exc
    if "vision_model" in data:
        table = data.pop("vision_model")
        values["vision_model"] = _merge_str_dict(
            values["vision_model"], table, "vision_model", "toml"
        )
    if "channels" in data:
        table = data.pop("channels")
        values["channels"] = _merge_str_dict(values["channels"], table, "channels", "toml")
    for key, raw in data.items():
        if key not in _KINDS:
            raise ConfigError(f"unknown key {key!r} in toml")
        if key in {"vision_model", "channels"}:
            continue
        values[key] = _coerce(_KINDS[key], raw, key, "toml")


def _apply_env(values: dict[str, object], env_layer: Mapping[str, str]) -> None:
    model = dict(values["vision_model"])  # type: ignore[arg-type]
    model_hit = False
    for key, raw in env_layer.items():
        if key.startswith(_VISION_MODEL_ENV) and len(key) > len(_VISION_MODEL_ENV):
            lane = key[len(_VISION_MODEL_ENV) :].lower()
            model[lane] = _as_str(raw, f"vision_model.{lane}", "env")
            model_hit = True
            continue
        if not key.startswith(_ENV_PREFIX):
            continue
        field = key[len(_ENV_PREFIX) :].lower()
        if field not in _KINDS or field in {"vision_model", "channels"}:
            if field == "channels":
                values["channels"] = _merge_str_dict(
                    values["channels"], _as_json_dict(raw, "channels", "env"), "channels", "env"
                )
            continue
        values[field] = _coerce(_KINDS[field], raw, field, "env")
    if model_hit:
        values["vision_model"] = model


def _apply_flags(values: dict[str, object], flags: Mapping[str, object]) -> None:
    for key, raw in flags.items():
        if raw is None:
            continue
        if key not in _KINDS:
            raise ConfigError(f"unknown key {key!r} in flag")
        if key == "vision_model":
            values["vision_model"] = _merge_str_dict(
                values["vision_model"], raw, "vision_model", "flag"
            )
            continue
        if key == "channels":
            values["channels"] = _merge_str_dict(values["channels"], raw, "channels", "flag")
            continue
        values[key] = _coerce(_KINDS[key], raw, key, "flag")


def _merge_str_dict(current: object, incoming: object, key: str, source: str) -> dict[str, str]:
    if not isinstance(incoming, dict):
        raise ConfigError(f"unparseable value for {key} from {source}: {incoming!r}")
    merged = dict(current) if isinstance(current, dict) else {}
    for item_key, item_value in incoming.items():
        name = _as_str(item_key, key, source)
        merged[name] = _as_str(item_value, f"{key}.{name}", source)
    return merged


def _as_json_dict(raw: str, key: str, source: str) -> dict[str, str]:
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ConfigError(f"unparseable value for {key} from {source}: {raw!r}") from exc
    if not isinstance(parsed, dict):
        raise ConfigError(f"unparseable value for {key} from {source}: {raw!r}")
    return parsed


def _coerce(kind: str, value: object, key: str, source: str) -> object:
    if kind in {"opt_int", "opt_path"} and _is_blank(value):
        return None
    if kind not in {"opt_int", "opt_path"} and _is_blank(value):
        raise ConfigError(f"unparseable value for {key} from {source}: {value!r}")
    if kind == "int":
        return _as_int(value, key, source)
    if kind == "opt_int":
        return _as_int(value, key, source)
    if kind == "time":
        return _as_time(value, key, source)
    if kind == "bool":
        return _as_bool(value, key, source)
    if kind == "path":
        return _as_path(value, key, source, optional=False)
    if kind == "opt_path":
        return _as_path(value, key, source, optional=True)
    if kind == "lane":
        text = _as_str(value, key, source)
        if text not in _LANES:
            raise ConfigError(f"unparseable value for {key} from {source}: {value!r}")
        return text
    if kind == "quality":
        text = _as_str(value, key, source)
        if text not in _QUALITY:
            raise ConfigError(f"unparseable value for {key} from {source}: {value!r}")
        return text
    if kind == "str":
        return _as_str(value, key, source)
    raise ConfigError(f"unknown key {key!r} in {source}")


def _is_blank(value: object) -> bool:
    return value is None or (isinstance(value, str) and value.strip() == "")


def _as_int(value: object, key: str, source: str) -> int:
    if isinstance(value, bool) or isinstance(value, float):
        raise ConfigError(f"unparseable value for {key} from {source}: {value!r}")
    if isinstance(value, int):
        return value
    text = str(value).strip()
    if not re.fullmatch(r"-?\d+", text):
        raise ConfigError(f"unparseable value for {key} from {source}: {value!r}")
    return int(text)


def _as_time(value: object, key: str, source: str) -> float:
    if isinstance(value, bool):
        raise ConfigError(f"unparseable value for {key} from {source}: {value!r}")
    if isinstance(value, (int, float)):
        number = float(value)
        if not math.isfinite(number) or number < 0:
            raise ConfigError(f"unparseable value for {key} from {source}: {value!r}")
        return number
    try:
        return timecode.parse(str(value).strip())
    except ValueError as exc:
        raise ConfigError(f"unparseable value for {key} from {source}: {value!r}") from exc


def _as_bool(value: object, key: str, source: str) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in (0, 1) and not isinstance(value, bool):
        return bool(value)
    text = str(value).strip().lower()
    if text in _BOOL_TRUE:
        return True
    if text in _BOOL_FALSE:
        return False
    raise ConfigError(f"unparseable value for {key} from {source}: {value!r}")


def _as_path(value: object, key: str, source: str, *, optional: bool) -> Path | None:
    if _is_blank(value):
        if optional:
            return None
        raise ConfigError(f"unparseable value for {key} from {source}: {value!r}")
    if isinstance(value, Path):
        return value.expanduser()
    if isinstance(value, str):
        return Path(value).expanduser()
    if isinstance(value, os.PathLike):
        return Path(value).expanduser()
    raise ConfigError(f"unparseable value for {key} from {source}: {value!r}")


def _as_str(value: object, key: str, source: str) -> str:
    if isinstance(value, (dict, list, bool)):
        raise ConfigError(f"unparseable value for {key} from {source}: {value!r}")
    if isinstance(value, bytes):
        raise ConfigError(f"unparseable value for {key} from {source}: {value!r}")
    return str(value)


def _writable_existing_dir(path: Path) -> tuple[bool, str]:
    if not path.exists():
        return False, f"{path} does not exist"
    if not path.is_dir():
        return False, f"{path} is not a directory"
    if not os.access(path, os.W_OK):
        return False, f"{path} is not writable"
    return True, f"{path} is writable"


def _creatable_dir(path: Path) -> tuple[bool, str]:
    if path.exists():
        return _writable_existing_dir(path)
    probe = path.parent
    while not probe.exists():
        if probe.parent == probe:
            return False, f"cannot create {path}"
        probe = probe.parent
    if not probe.is_dir() or not os.access(probe, os.W_OK):
        return False, f"cannot create {path} under {probe}"
    return True, f"{path} can be created"


# run-key field list is the cache contract; keep it next to the hasher.
assert set(_RUN_KEY_FIELDS) <= set(_KINDS)
