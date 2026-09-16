"""Usage-aware vision lane chooser (issue #26).

Reads the AgentUsageBar snapshot, projects each lane's tokens and quota windows
for a frame plan, and picks Claude or metered Gemini by headroom. Codex is never
an auto candidate (decision 47); an explicit `--vision` bypasses the choice.
"""

from __future__ import annotations

import json
import math
import os
import shutil
import statistics
import subprocess
import tomllib
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from importlib import resources
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from frameweave.config import Config

DEFAULT_SNAPSHOT_PATH = (
    Path.home() / "Library" / "Application Support" / "AgentUsageBar" / "usage-snapshot.json"
)
DEFAULT_REFRESH_SCRIPT = (
    Path.home()
    / "projects"
    / "fleet"
    / "skills"
    / "universal"
    / "ai-usage"
    / "scripts"
    / "get-usage.sh"
)
STALE_SECONDS = 120
PREFLIGHT_MAX_AGE_SECONDS = 600  # 10 minutes for doctor
REFRESH_TIMEOUT_S = 40
OUTPUT_TOKENS_PER_FRAME = 120
CODEX_WARNING = "codex under-transcribes on-screen text (decision 47)"
USAGE_UNAVAILABLE_WARNING = "usage could not be read"

# Lane -> (provider key in snapshot, ordered window metric ids, long-window id)
_LANE_WINDOWS: dict[str, tuple[str, tuple[str, ...], str]] = {
    "claude": ("claude", ("five_hour", "seven_day"), "seven_day"),
    "codex": ("openai", ("primary", "secondary"), "secondary"),
}
_AUTO_CANDIDATES = ("claude", "gemini")
_SUBSCRIPTION_LANES = frozenset({"claude", "codex"})
SUBSCRIPTION_LANES = _SUBSCRIPTION_LANES
_EXPLICIT_LANES = frozenset({"claude", "codex", "gemini", "none"})
_METERED_LANES = frozenset({"gemini"})
METERED_LANES = _METERED_LANES

_COEFFICIENTS: dict[str, dict[str, float]] | None = None

_PRESENCE_REASONS: dict[str, str] = {
    "gemini": "GEMINI_API_KEY not set",
    "claude": "claude CLI not on PATH",
    "codex": "codex CLI not on PATH",
}


class NoVisionLane(RuntimeError):
    """No vision lane is available (or the explicit lane named is unavailable)."""


def _presence_reason(
    lane: str,
    env: Mapping[str, str],
    which: Callable[[str], str | None],
) -> str | None:
    """``None`` when the lane's key or CLI is present; the reason string otherwise."""
    if lane == "gemini":
        key = env.get("GEMINI_API_KEY", "")
        return None if key and key.strip() else _PRESENCE_REASONS["gemini"]
    if lane in ("claude", "codex"):
        return None if which(lane) is not None else _PRESENCE_REASONS[lane]
    return None


def _lane_unavailable_message(
    projections: list[Projection], *, lane: str | None = None
) -> str:
    """Message for ``NoVisionLane``: every candidate's reason plus the doctor remedy."""
    header = (
        f"vision lane {lane!r} is unavailable" if lane else "no vision lane is available"
    )
    lines = [f"{header}:"]
    for proj in projections:
        if proj.lane == "none":
            continue
        status = "available" if proj.available else (proj.reason or "unavailable")
        lines.append(f"  {proj.lane}: {status}")
    lines.append("Run `frameweave doctor` for remedies.")
    return "\n".join(lines)


@dataclass(frozen=True)
class Snapshot:
    """Parsed usage snapshot. ``metrics[provider][id]`` is percent used."""

    generated_at: datetime
    metrics: dict[str, dict[str, float]]
    path: Path | None = None


@dataclass(frozen=True)
class Plan:
    frames: int
    transcript_minutes: float
    frames_per_call: int


@dataclass(frozen=True)
class Projection:
    lane: str
    tokens: int
    calls: int
    usd: float
    # before may be None when the snapshot was missing (print as n/a).
    windows: dict[str, tuple[float | None, float | None]]
    available: bool
    reason: str | None


@dataclass(frozen=True)
class Choice:
    lane: str
    projections: list[Projection]
    reason: str
    warning: str | None


def load_coefficients(path: Path | None = None) -> dict[str, dict[str, float]]:
    """Load packaged ``lanes.toml`` (or an explicit path). Lazy; clear error if missing."""
    if path is not None:
        try:
            with path.open("rb") as handle:
                raw = tomllib.load(handle)
        except FileNotFoundError as exc:
            raise RuntimeError(f"lanes coefficients not found: {path}") from exc
        except (OSError, tomllib.TOMLDecodeError) as exc:
            raise RuntimeError(f"lanes coefficients unreadable: {path}: {exc}") from exc
    else:
        try:
            ref = resources.files("frameweave").joinpath("lanes.toml")
            raw = tomllib.loads(ref.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, tomllib.TOMLDecodeError, TypeError) as exc:
            raise RuntimeError(
                "packaged lanes.toml could not be loaded; reinstall frameweave "
                "(uv tool install --from . frameweave --force)"
            ) from exc
    out: dict[str, dict[str, float]] = {}
    for lane, table in raw.items():
        if not isinstance(table, dict):
            continue
        out[str(lane)] = {str(k): float(v) for k, v in table.items()}
    return out


def get_coefficients() -> dict[str, dict[str, float]]:
    """Cached packaged coefficients. Prefer this over importing a module constant."""
    global _COEFFICIENTS
    if _COEFFICIENTS is None:
        _COEFFICIENTS = load_coefficients()
    return _COEFFICIENTS


def __getattr__(name: str) -> Any:
    if name == "COEFFICIENTS":
        return get_coefficients()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def resolve_usage_paths(config: Config) -> tuple[Path, Path]:
    """Snapshot and refresh-script paths from Config, else module defaults."""
    snap = (
        config.usage_snapshot if config.usage_snapshot is not None else DEFAULT_SNAPSHOT_PATH
    )
    script = (
        config.usage_refresh_script
        if config.usage_refresh_script is not None
        else DEFAULT_REFRESH_SCRIPT
    )
    return snap, script


def load_snapshot(
    path: Path | None = None,
    *,
    refresh: bool = True,
    refresh_script: Path | None = None,
    now: datetime | None = None,
) -> Snapshot | None:
    """Read the usage snapshot JSON. Refresh when stale if a script is available."""
    target = path if path is not None else DEFAULT_SNAPSHOT_PATH
    # None means the module default; pass a missing Path to disable refresh.
    script: Path | None = (
        DEFAULT_REFRESH_SCRIPT if refresh_script is None else refresh_script
    )
    clock = now or datetime.now(UTC)

    if refresh and target.is_file():
        snap = _parse_snapshot(target)
        if snap is not None and _age_seconds(snap.generated_at, clock) > STALE_SECONDS:
            _run_refresh(script)
            snap = _parse_snapshot(target) if target.is_file() else None
            if snap is None or _age_seconds(snap.generated_at, clock) > STALE_SECONDS:
                return None
            return snap
    elif refresh and not target.is_file():
        _run_refresh(script)

    if not target.is_file():
        return None
    snap = _parse_snapshot(target)
    if snap is None:
        return None
    if _age_seconds(snap.generated_at, clock) > STALE_SECONDS:
        return None
    return snap


def project(
    lane: str,
    frames: int,
    transcript_minutes: float,
    frames_per_call: int,
    coefficients: dict[str, dict[str, float]],
    snapshot: Snapshot | None,
    *,
    skip_percent: float = 90.0,
    env: Mapping[str, str] | None = None,
    which: Callable[[str], str | None] | None = None,
) -> Projection:
    """Project tokens, dollars, and window percents for one lane.

    ``env`` and ``which`` default to ``os.environ`` and ``shutil.which`` and are
    injected in tests. Availability from quota (usage windows) and availability
    from presence (key or CLI) combine; ``reason`` names whichever failed first,
    presence taking priority since a missing key or binary can't be worked around.
    """
    if lane == "none":
        return Projection(
            lane=lane,
            tokens=0,
            calls=0,
            usd=0.0,
            windows={},
            available=True,
            reason=None,
        )

    env = os.environ if env is None else env
    which = shutil.which if which is None else which

    calls = math.ceil(frames / frames_per_call) if frames else 0
    coeffs = coefficients.get(lane, {})
    a = float(coeffs.get("a", 0.0))
    b = float(coeffs.get("b", 0.0))
    c = float(coeffs.get("c", 0.0))
    tokens = int(round(calls * a + frames * b + c * transcript_minutes))

    usd = 0.0
    if lane == "gemini":
        out_tokens = frames * OUTPUT_TOKENS_PER_FRAME
        usd = (
            tokens / 1000.0 * float(coeffs.get("usd_in_per_1k", 0.0))
            + out_tokens / 1000.0 * float(coeffs.get("usd_out_per_1k", 0.0))
        )

    windows: dict[str, tuple[float | None, float | None]] = {}
    quota_available = True
    quota_reason: str | None = None

    if lane in _LANE_WINDOWS:
        provider, window_ids, _long = _LANE_WINDOWS[lane]
        provider_metrics = {} if snapshot is None else snapshot.metrics.get(provider, {})
        for window_id in window_ids:
            pct_per_1k = float(coeffs.get(window_id, 0.0))
            delta = (tokens / 1000.0) * pct_per_1k
            if snapshot is None:
                windows[window_id] = (None, None)
                continue
            before = float(provider_metrics.get(window_id, 0.0))
            after = before + delta
            windows[window_id] = (before, after)
            if after > skip_percent:
                quota_available = False
                if quota_reason is None:
                    quota_reason = (
                        f"{window_id} would reach {after:.1f}% (skip at {skip_percent:g}%)"
                    )
    # gemini has no subscription windows; only its key presence gates it.

    presence_reason = _presence_reason(lane, env, which)
    available = quota_available and presence_reason is None
    reason = presence_reason if presence_reason is not None else quota_reason

    return Projection(
        lane=lane,
        tokens=tokens,
        calls=calls,
        usd=usd,
        windows=windows,
        available=available,
        reason=reason,
    )


def choose(
    plan: Plan,
    snapshot: Snapshot | None,
    coefficients: dict[str, dict[str, float]],
    config: Config,
    explicit_lane: str | None = None,
    *,
    env: Mapping[str, str] | None = None,
    which: Callable[[str], str | None] | None = None,
    skip_cli_presence: bool = False,
) -> Choice:
    """Pick a vision lane. Pure given ``snapshot`` (no I/O).

    Auto vs explicit is solely ``explicit_lane``: pass a named lane to bypass the
    chooser, or ``None`` to run the auto rules (config.vision_lane is not read
    for that decision — only ``lane_skip_percent`` and related fields).

    ``choose`` never returns a lane whose key or CLI is missing. With no
    available candidate, or an explicit lane that is unavailable, it raises
    ``NoVisionLane`` (issue #66); ``env`` and ``which`` default to
    ``os.environ`` and ``shutil.which`` and are injected in tests. Quota-based
    skip (``lane_skip_percent``) is bypassed for an explicit lane as before
    (decision 40); only a missing key or CLI blocks it.

    ``skip_cli_presence`` bypasses the real-CLI check (``claude``/``codex``
    only, never Gemini's key) for an explicit named lane: the caller (the
    pipeline) sets it when it already has a working ``VisionBackend`` object,
    so the real CLI's absence is moot — the injected backend, not
    ``make_backend``'s subprocess, is what actually runs. Auto mode is
    unaffected; the projections table still reports the real CLI's presence.
    """
    env = os.environ if env is None else env
    which = shutil.which if which is None else which
    skip = float(config.lane_skip_percent)
    frames = plan.frames
    minutes = plan.transcript_minutes
    per_call = max(1, int(plan.frames_per_call))

    named = explicit_lane if explicit_lane in _EXPLICIT_LANES else None

    project_lanes = list(_AUTO_CANDIDATES)
    if named is not None and named not in project_lanes:
        if named != "none":
            project_lanes.append(named)

    projections = [
        project(
            lane,
            frames,
            minutes,
            per_call,
            coefficients,
            snapshot,
            skip_percent=skip,
            env=env,
            which=which,
        )
        for lane in project_lanes
    ]
    by_lane = {p.lane: p for p in projections}

    if named is not None:
        warning = CODEX_WARNING if named == "codex" else None
        if named == "none":
            none_proj = project(
                "none",
                frames,
                minutes,
                per_call,
                coefficients,
                snapshot,
                skip_percent=skip,
                env=env,
                which=which,
            )
            return Choice(
                lane="none",
                projections=[none_proj, *projections],
                reason="explicit lane",
                warning=warning,
            )
        # Quota skip is bypassable for an explicit lane; a missing key or CLI is
        # not, unless an injected backend already made the real CLI moot.
        presence_reason = _presence_reason(named, env, which)
        bypassed = skip_cli_presence and named in ("claude", "codex")
        if presence_reason is not None and not bypassed:
            raise NoVisionLane(_lane_unavailable_message(projections, lane=named))
        return Choice(
            lane=named,
            projections=projections,
            reason="explicit lane",
            warning=warning,
        )

    if snapshot is None:
        claude_proj = by_lane.get("claude")
        if claude_proj is not None and claude_proj.available:
            return Choice(
                lane="claude",
                projections=projections,
                reason="usage snapshot unavailable; defaulting to claude",
                warning=USAGE_UNAVAILABLE_WARNING,
            )
        gemini_proj = by_lane.get("gemini")
        if gemini_proj is not None and gemini_proj.available:
            return Choice(
                lane="gemini",
                projections=projections,
                reason=(
                    "usage snapshot unavailable; claude unavailable "
                    f"({claude_proj.reason if claude_proj else 'unknown'}); "
                    "falling back to metered gemini"
                ),
                warning=USAGE_UNAVAILABLE_WARNING,
            )
        raise NoVisionLane(_lane_unavailable_message(projections))

    subscription = [
        by_lane[name]
        for name in _AUTO_CANDIDATES
        if name in _SUBSCRIPTION_LANES and name in by_lane and by_lane[name].available
    ]
    if subscription:
        def headroom(proj: Projection) -> float:
            _provider, _ids, long_id = _LANE_WINDOWS[proj.lane]
            _before, after = proj.windows[long_id]
            return 100.0 - float(after or 0.0)

        best = max(subscription, key=headroom)
        return Choice(
            lane=best.lane,
            projections=projections,
            reason=(
                f"{best.lane} has the most 7-day headroom after the run "
                f"({headroom(best):.1f}% left)"
            ),
            warning=None,
        )

    gemini = by_lane.get("gemini")
    crossed = [
        by_lane[name]
        for name in _AUTO_CANDIDATES
        if name in _SUBSCRIPTION_LANES and name in by_lane and not by_lane[name].available
    ]
    crossed_reason = "; ".join(
        f"{p.lane}: {p.reason}" for p in crossed if p.reason
    ) or "subscription lanes unavailable"
    if gemini is None or not gemini.available:
        raise NoVisionLane(_lane_unavailable_message(projections))
    return Choice(
        lane="gemini",
        projections=projections,
        reason=f"metered fallback ({crossed_reason})",
        warning=None,
    )


def choice_to_dict(choice: Choice) -> dict[str, Any]:
    """JSON-friendly shape for ``meta.json`` ``lane_choice``."""
    return {
        "lane": choice.lane,
        "reason": choice.reason,
        "warning": choice.warning,
        "projections": [
            {
                "lane": p.lane,
                "tokens": p.tokens,
                "calls": p.calls,
                "usd": p.usd,
                "windows": {
                    k: [v[0], v[1]] for k, v in p.windows.items()
                },
                "available": p.available,
                "reason": p.reason,
            }
            for p in choice.projections
        ],
    }


def format_projection_table(choice: Choice) -> str:
    """One row per candidate for the inspect / pre-spend block."""
    lines = [
        "lane projection:",
        (
            f"  {'lane':<8} {'calls':>5} {'tokens':>8} {'usd':>10}  "
            f"{'windows (before -> after)':<48} available"
        ),
    ]
    for proj in choice.projections:
        if proj.lane == "none":
            continue
        if proj.windows:
            parts: list[str] = []
            for name, (before, after) in proj.windows.items():
                if before is None or after is None:
                    parts.append(f"{name} n/a")
                else:
                    parts.append(f"{name} {before:.1f}->{after:.1f}")
            windows = ", ".join(parts)
        else:
            windows = "-"
        avail = "yes" if proj.available else (proj.reason or "no")
        lines.append(
            f"  {proj.lane:<8} {proj.calls:5d} {proj.tokens:8d} ${proj.usd:9.4f}  "
            f"{windows:<48} {avail}"
        )
    lines.append(f"chosen: {choice.lane} ({choice.reason})")
    if choice.warning:
        lines.append(f"warning: {choice.warning}")
    return "\n".join(lines)


def plan_from_duration(
    duration_s: float,
    frames: int,
    frames_per_call: int,
) -> Plan:
    """Build a plan; transcript minutes are the range duration in minutes."""
    return Plan(
        frames=frames,
        transcript_minutes=max(0.0, float(duration_s) / 60.0),
        frames_per_call=max(1, int(frames_per_call)),
    )


def lane_actual_from_run(
    *,
    lane: str,
    frames: int,
    calls: int,
    tokens: int,
    before: Snapshot | None,
    after: Snapshot | None,
) -> dict[str, Any]:
    """Build the ``cost.json`` ``lane_actual`` object after describe."""
    tokens_per_frame = (tokens / frames) if frames else 0.0
    tokens_per_call = (tokens / calls) if calls else 0.0
    frames_per_call = (frames / calls) if calls else 0.0
    quota_delta: dict[str, float] = {}
    if lane in _LANE_WINDOWS and before is not None and after is not None:
        provider, window_ids, _long = _LANE_WINDOWS[lane]
        before_m = before.metrics.get(provider, {})
        after_m = after.metrics.get(provider, {})
        for window_id in window_ids:
            if window_id in before_m and window_id in after_m:
                quota_delta[window_id] = float(after_m[window_id]) - float(before_m[window_id])
    return {
        "lane": lane,
        "tokens": tokens,
        "calls": calls,
        "frames": frames,
        "frames_per_call": frames_per_call,
        "tokens_per_frame": tokens_per_frame,
        "tokens_per_call": tokens_per_call,
        "quota_delta": quota_delta,
    }


def recalibrate(cost_files: list[Path]) -> dict[str, Any]:
    """Fit intercept ``a`` and per-frame slope ``b`` from lane_actual samples.

    Model: ``tokens_per_call = a + b * frames_per_call``. When every sample shares
    one batch size, keep packaged ``a`` fixed and derive ``b`` so pasting does not
    double-count the per-frame part already inside tokens_per_call.
    """
    files = list(cost_files)[-10:]
    samples: list[tuple[float, float]] = []  # (frames_per_call, tokens_per_call)
    lanes_seen: list[str] = []
    for path in files:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        actual = data.get("lane_actual")
        if not isinstance(actual, dict):
            continue
        tpc = actual.get("tokens_per_call")
        if not isinstance(tpc, (int, float)):
            continue
        fpc = actual.get("frames_per_call")
        if isinstance(fpc, (int, float)) and float(fpc) > 0:
            frames_per_call = float(fpc)
        else:
            frames = actual.get("frames")
            calls = actual.get("calls")
            if (
                isinstance(frames, (int, float))
                and isinstance(calls, (int, float))
                and float(calls) > 0
            ):
                frames_per_call = float(frames) / float(calls)
            else:
                continue
        samples.append((frames_per_call, float(tpc)))
        lane = actual.get("lane")
        if isinstance(lane, str):
            lanes_seen.append(lane)

    if not samples:
        return {}

    lane = lanes_seen[-1] if lanes_seen else "claude"
    coeffs = get_coefficients().get(lane, {})
    a_fixed = float(coeffs.get("a", 0.0))
    distinct_fpc = {round(fpc, 6) for fpc, _tpc in samples}

    if len(distinct_fpc) == 1:
        fpc = samples[0][0]
        median_tpc = statistics.median([tpc for _fpc, tpc in samples])
        b = (median_tpc - a_fixed) / fpc if fpc else 0.0
        a = a_fixed
    else:
        # Ordinary least squares: tpc = a + b * fpc
        n = len(samples)
        mean_x = sum(fpc for fpc, _ in samples) / n
        mean_y = sum(tpc for _, tpc in samples) / n
        var_x = sum((fpc - mean_x) ** 2 for fpc, _ in samples)
        if var_x == 0:
            a = a_fixed
            b = (mean_y - a) / mean_x if mean_x else 0.0
        else:
            cov = sum((fpc - mean_x) * (tpc - mean_y) for fpc, tpc in samples)
            b = cov / var_x
            a = mean_y - b * mean_x

    result: dict[str, Any] = {"lane": lane, "a": a, "b": b}
    result["c"] = float(coeffs.get("c", 200.0))
    rate_keys = (
        "five_hour",
        "seven_day",
        "primary",
        "secondary",
        "usd_in_per_1k",
        "usd_out_per_1k",
    )
    for key in rate_keys:
        if key in coeffs:
            result[key] = coeffs[key]
    return result


def format_recalibrate_toml(result: dict[str, Any]) -> str:
    """Printable TOML fragment for the operator to paste into ``lanes.toml``."""
    if not result:
        return "# no lane_actual data in the given cost.json files\n"
    lane = str(result.get("lane", "claude"))
    lines = [f"[{lane}]"]
    for key in (
        "a",
        "b",
        "c",
        "five_hour",
        "seven_day",
        "primary",
        "secondary",
        "usd_in_per_1k",
        "usd_out_per_1k",
    ):
        if key not in result or key == "lane":
            continue
        value = result[key]
        if isinstance(value, float):
            lines.append(f"{key} = {value:g}")
        else:
            lines.append(f"{key} = {value}")
    return "\n".join(lines) + "\n"


def cli_flags() -> list:
    """No new flags; ``--vision`` already exists."""
    return []


def preflight_checks(config: Config) -> list:
    """When lane is auto, warn if the usage snapshot is missing or older than 10 minutes."""
    from frameweave.config import Check

    if config.vision_lane != "auto":
        return []
    path, _script = resolve_usage_paths(config)
    remedy = "Open AgentUsageBar or set --vision to a named lane."
    if not path.is_file():
        return [
            Check(
                name="usage snapshot",
                ok=False,
                detail=f"missing: {path}",
                remedy=remedy,
                required=False,
            )
        ]
    snap = _parse_snapshot(path)
    if snap is None:
        return [
            Check(
                name="usage snapshot",
                ok=False,
                detail=f"unreadable: {path}",
                remedy=remedy,
                required=False,
            )
        ]
    age = _age_seconds(snap.generated_at, datetime.now(UTC))
    ok = age <= PREFLIGHT_MAX_AGE_SECONDS
    if ok:
        detail = f"{path.name} age {age:.0f}s"
    else:
        detail = f"{path.name} age {age:.0f}s (>{PREFLIGHT_MAX_AGE_SECONDS}s)"
    return [
        Check(
            name="usage snapshot",
            ok=ok,
            detail=detail,
            remedy=remedy,
            required=False,
        )
    ]


def _run_refresh(script: Path | None) -> bool:
    if script is None or not script.is_file():
        return False
    try:
        completed = subprocess.run(
            [str(script)],
            capture_output=True,
            text=True,
            timeout=REFRESH_TIMEOUT_S,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return completed.returncode == 0


def _parse_snapshot(path: Path) -> Snapshot | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    raw_ts = data.get("generatedAt")
    if not isinstance(raw_ts, str):
        return None
    try:
        generated = datetime.fromisoformat(raw_ts.replace("Z", "+00:00"))
    except ValueError:
        return None
    if generated.tzinfo is None:
        generated = generated.replace(tzinfo=UTC)
    metrics: dict[str, dict[str, float]] = {}
    providers = data.get("providers")
    if not isinstance(providers, dict):
        return Snapshot(generated_at=generated, metrics={}, path=path)
    for provider, body in providers.items():
        if not isinstance(body, dict):
            continue
        rows = body.get("metrics")
        if not isinstance(rows, list):
            continue
        bucket: dict[str, float] = {}
        for row in rows:
            if not isinstance(row, dict):
                continue
            metric_id = row.get("id")
            percent = row.get("percentUsed")
            if not isinstance(metric_id, str) or not isinstance(percent, (int, float)):
                continue
            # limit.* (Fable weekly) and other scoped rows are recorded in the
            # file but never used as Claude / OpenAI chooser windows.
            if metric_id.startswith("limit."):
                continue
            bucket[metric_id] = float(percent)
        metrics[str(provider)] = bucket
    return Snapshot(generated_at=generated, metrics=metrics, path=path)


def _age_seconds(generated_at: datetime, now: datetime) -> float:
    if generated_at.tzinfo is None:
        generated_at = generated_at.replace(tzinfo=UTC)
    if now.tzinfo is None:
        now = now.replace(tzinfo=UTC)
    return max(0.0, (now - generated_at).total_seconds())
