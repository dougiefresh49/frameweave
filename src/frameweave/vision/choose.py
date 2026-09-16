"""Usage-aware vision lane chooser (issue #26).

Reads the AgentUsageBar snapshot, projects each lane's tokens and quota windows
for a frame plan, and picks Claude or metered Gemini by headroom. Codex is never
an auto candidate (decision 47); an explicit `--vision` bypasses the choice.
"""

from __future__ import annotations

import json
import math
import statistics
import subprocess
import tomllib
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from frameweave.config import Config

_REPO_ROOT = Path(__file__).resolve().parents[3]
_LANES_PATH = _REPO_ROOT / "config" / "lanes.toml"

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
_EXPLICIT_LANES = frozenset({"claude", "codex", "gemini", "none"})


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
    windows: dict[str, tuple[float, float]]
    available: bool
    reason: str | None


@dataclass(frozen=True)
class Choice:
    lane: str
    projections: list[Projection]
    reason: str
    warning: str | None


def load_coefficients(path: Path | None = None) -> dict[str, dict[str, float]]:
    """Load ``config/lanes.toml``. Keys are lane names; values are coefficient maps."""
    target = path or _LANES_PATH
    with target.open("rb") as handle:
        raw = tomllib.load(handle)
    out: dict[str, dict[str, float]] = {}
    for lane, table in raw.items():
        if not isinstance(table, dict):
            continue
        out[str(lane)] = {str(k): float(v) for k, v in table.items()}
    return out


COEFFICIENTS: dict[str, dict[str, float]] = load_coefficients()


def load_snapshot(
    path: Path | None = None,
    *,
    refresh: bool = True,
    refresh_script: Path | None = None,
    now: datetime | None = None,
) -> Snapshot | None:
    """Read the usage snapshot JSON. Refresh when stale if a script is available."""
    target = path or DEFAULT_SNAPSHOT_PATH
    script = DEFAULT_REFRESH_SCRIPT if refresh_script is None else refresh_script
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
) -> Projection:
    """Project tokens, dollars, and window percents for one lane."""
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

    windows: dict[str, tuple[float, float]] = {}
    available = True
    reason: str | None = None

    if lane in _LANE_WINDOWS:
        provider, window_ids, _long = _LANE_WINDOWS[lane]
        provider_metrics = {} if snapshot is None else snapshot.metrics.get(provider, {})
        for window_id in window_ids:
            before = float(provider_metrics.get(window_id, 0.0))
            pct_per_1k = float(coeffs.get(window_id, 0.0))
            after = before + (tokens / 1000.0) * pct_per_1k
            windows[window_id] = (before, after)
            if after > skip_percent:
                available = False
                if reason is None:
                    reason = f"{window_id} would reach {after:.1f}% (skip at {skip_percent:g}%)"
    # gemini and none have no subscription windows; gemini stays available.

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
) -> Choice:
    """Pick a vision lane. Pure given ``snapshot`` (no I/O)."""
    skip = float(config.lane_skip_percent)
    frames = plan.frames
    minutes = plan.transcript_minutes
    per_call = max(1, int(plan.frames_per_call))

    named = explicit_lane if explicit_lane is not None else None
    if named is None and config.vision_lane != "auto":
        named = config.vision_lane

    project_lanes = list(_AUTO_CANDIDATES)
    if named is not None and named not in project_lanes and named in _EXPLICIT_LANES:
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
        )
        for lane in project_lanes
    ]
    by_lane = {p.lane: p for p in projections}

    if named is not None and named in _EXPLICIT_LANES:
        warning = CODEX_WARNING if named == "codex" else None
        if named == "none":
            none_proj = project(
                "none", frames, minutes, per_call, coefficients, snapshot, skip_percent=skip
            )
            return Choice(
                lane="none",
                projections=[none_proj, *projections],
                reason="explicit lane",
                warning=warning,
            )
        return Choice(
            lane=named,
            projections=projections,
            reason="explicit lane",
            warning=warning,
        )

    if snapshot is None:
        return Choice(
            lane="claude",
            projections=projections,
            reason="usage snapshot unavailable; defaulting to claude",
            warning=USAGE_UNAVAILABLE_WARNING,
        )

    subscription = [
        by_lane[name]
        for name in _AUTO_CANDIDATES
        if name in _SUBSCRIPTION_LANES and name in by_lane and by_lane[name].available
    ]
    if subscription:
        def headroom(proj: Projection) -> float:
            _provider, _ids, long_id = _LANE_WINDOWS[proj.lane]
            _before, after = proj.windows[long_id]
            return 100.0 - after

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
    return Choice(
        lane="gemini",
        projections=projections,
        reason=f"metered fallback ({crossed_reason})",
        warning=None if gemini is not None else USAGE_UNAVAILABLE_WARNING,
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
                "windows": {k: [v[0], v[1]] for k, v in p.windows.items()},
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
            parts = [
                f"{name} {before:.1f}->{after:.1f}"
                for name, (before, after) in proj.windows.items()
            ]
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
        "tokens_per_frame": tokens_per_frame,
        "tokens_per_call": tokens_per_call,
        "quota_delta": quota_delta,
    }


def recalibrate(cost_files: list[Path]) -> dict[str, Any]:
    """Median tokens per frame and per call over up to the last ten cost files.

    Returns a dict shaped for pasting into ``lanes.toml`` (does not write the file).
    """
    files = list(cost_files)[-10:]
    per_frame: list[float] = []
    per_call: list[float] = []
    lanes_seen: list[str] = []
    for path in files:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        actual = data.get("lane_actual")
        if not isinstance(actual, dict):
            continue
        tpf = actual.get("tokens_per_frame")
        tpc = actual.get("tokens_per_call")
        if isinstance(tpf, (int, float)):
            per_frame.append(float(tpf))
        if isinstance(tpc, (int, float)):
            per_call.append(float(tpc))
        lane = actual.get("lane")
        if isinstance(lane, str):
            lanes_seen.append(lane)

    if not per_frame and not per_call:
        return {}

    lane = lanes_seen[-1] if lanes_seen else "claude"
    result: dict[str, Any] = {"lane": lane}
    if per_call:
        # a ≈ tokens per call with the batch size baked into the median call size
        result["a"] = statistics.median(per_call)
    if per_frame:
        result["b"] = statistics.median(per_frame)
    result["c"] = float(COEFFICIENTS.get(lane, {}).get("c", 200.0))
    rate_keys = (
        "five_hour",
        "seven_day",
        "primary",
        "secondary",
        "usd_in_per_1k",
        "usd_out_per_1k",
    )
    for key in rate_keys:
        if key in COEFFICIENTS.get(lane, {}):
            result[key] = COEFFICIENTS[lane][key]
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
    path = DEFAULT_SNAPSHOT_PATH
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
