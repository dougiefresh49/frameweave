"""Tests for the usage-aware lane chooser (issue #26).

Fixtures under tests/fixtures/usage-snapshot/ are hand-written from the
AgentUsageBar snapshot shape (generatedAt + providers.*.metrics with
percentUsed). No live refresh or provider API is called.
"""

from __future__ import annotations

import json
import stat
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from frameweave.config import load
from frameweave.preflight import MODULES
from frameweave.vision import choose as choose_mod
from frameweave.vision.choose import (
    CODEX_WARNING,
    USAGE_UNAVAILABLE_WARNING,
    NoVisionLane,
    Plan,
    Snapshot,
    choose,
    format_projection_table,
    format_recalibrate_toml,
    get_coefficients,
    lane_actual_from_run,
    load_coefficients,
    load_snapshot,
    preflight_checks,
    project,
    recalibrate,
)

FIXTURES = Path(__file__).parent / "fixtures" / "usage-snapshot"
MISSING_TOML = Path("/nonexistent/frameweave-test/config.toml")
# Fresh relative to fixture generatedAt values (2026-09-16T18:00:00Z).
NOW = datetime(2026, 9, 16, 18, 0, 30, tzinfo=UTC)
STALE_NOW = datetime(2026, 9, 16, 18, 5, 0, tzinfo=UTC)

# All lanes present (key set, CLIs on PATH): the baseline for tests that exercise
# quota logic and are unrelated to issue #66's presence checks.
ALL_PRESENT_ENV = {"GEMINI_API_KEY": "test-key-not-real"}


def _all_present_which(name: str) -> str | None:
    return f"/usr/bin/{name}"


def _presence(**overrides: object) -> dict[str, object]:
    """kwargs for choose()/project() with every lane present unless overridden."""
    base: dict[str, object] = {"env": ALL_PRESENT_ENV, "which": _all_present_which}
    base.update(overrides)
    return base


def _cfg(**flags: object):
    base: dict[str, object] = {"vision_lane": "auto", "frames_per_call": 8}
    base.update(flags)
    return load(flags=base, env={}, toml_path=MISSING_TOML, dotenv_paths=[])


def _snap(name: str):
    path = FIXTURES / name
    snap = load_snapshot(path, refresh=False, now=NOW)
    assert snap is not None, name
    return snap


def _plan(frames: int = 16, minutes: float = 10.0, per_call: int = 8) -> Plan:
    return Plan(frames=frames, transcript_minutes=minutes, frames_per_call=per_call)


def _coeffs() -> dict[str, dict[str, float]]:
    return get_coefficients()


def test_project_arithmetic_against_hand_table() -> None:
    """Hand-computed table (decision 48 coefficients).

    frames=16, frames_per_call=8, transcript_minutes=10
      calls = ceil(16/8) = 2
      tokens = 2*3997 + 16*2034 + 200*10
             = 7994 + 32544 + 2000
             = 42538
      five_hour delta = 42.538 * 0.0166 = 0.7061308
      seven_day  delta = 42.538 * 0.004  = 0.170152
      before five_hour=10 → after 10.7061308
      before seven_day=20  → after 20.170152

    gemini: tokens = 2*300 + 16*1265 + 2000 = 600 + 20240 + 2000 = 22840
      usd = 22.840*0.0003 + (16*120)/1000*0.0025
          = 0.006852 + 0.0048
          = 0.011652
    """
    snap = _snap("both-open.json")
    coeffs = _coeffs()
    claude = project("claude", 16, 10.0, 8, coeffs, snap, skip_percent=90, **_presence())
    assert claude.calls == 2
    assert claude.tokens == 42538
    assert claude.windows["five_hour"][0] == pytest.approx(10.0)
    assert claude.windows["five_hour"][1] == pytest.approx(10.7061308)
    assert claude.windows["seven_day"][1] == pytest.approx(20.170152)
    assert claude.available is True

    gemini = project("gemini", 16, 10.0, 8, coeffs, snap, skip_percent=90, **_presence())
    assert gemini.tokens == 22840
    assert gemini.usd == pytest.approx(0.011652)
    assert gemini.windows == {}
    assert gemini.available is True


def test_both_open_picks_claude_by_headroom() -> None:
    """Only Claude is a subscription auto-candidate (decision 47); it wins when open."""
    snap = _snap("both-open.json")
    choice = choose(_plan(), snap, _coeffs(), _cfg(), explicit_lane=None, **_presence())
    assert choice.lane == "claude"
    assert choice.warning is None
    assert any(p.lane == "gemini" for p in choice.projections)


def test_claude_5h_crossing_falls_to_gemini() -> None:
    """Claude at 89% with a run that crosses 90 is skipped; metered Gemini wins."""
    snap = _snap("claude-5h-at-89.json")
    # 40 frames / 8 = 5 calls → ~107k tokens → five_hour delta ~1.8 → after ~90.8
    choice = choose(_plan(40, 30.0), snap, _coeffs(), _cfg(), explicit_lane=None, **_presence())
    assert choice.lane == "gemini"
    claude = next(p for p in choice.projections if p.lane == "claude")
    assert claude.available is False
    assert "five_hour" in (claude.reason or "")
    assert "metered fallback" in choice.reason


def test_skip_percent_from_config_at_75() -> None:
    """FRAMEWEAVE_LANE_SKIP_PERCENT / config.lane_skip_percent is honored (fixture at 75)."""
    snap = _snap("claude-5h-at-70.json")
    # 120 frames → five_hour delta ~5.15 → 70+5.15 crosses 75, stays under 90.
    plan = _plan(120, 30.0)
    cfg75 = _cfg(lane_skip_percent=75)
    choice = choose(plan, snap, _coeffs(), cfg75, explicit_lane=None, **_presence())
    assert choice.lane == "gemini"

    cfg90 = _cfg(lane_skip_percent=90)
    open_choice = choose(plan, snap, _coeffs(), cfg90, explicit_lane=None, **_presence())
    assert open_choice.lane == "claude"

    # Env var path: FRAMEWEAVE_LANE_SKIP_PERCENT
    from_env = load(
        flags={"vision_lane": "auto", "frames_per_call": 8},
        env={"FRAMEWEAVE_LANE_SKIP_PERCENT": "75"},
        toml_path=MISSING_TOML,
        dotenv_paths=[],
    )
    assert from_env.lane_skip_percent == 75
    assert choose(plan, snap, _coeffs(), from_env, **_presence()).lane == "gemini"


def test_both_crossing_returns_gemini_with_reason() -> None:
    snap = _snap("both-crossing.json")
    choice = choose(_plan(40, 30.0), snap, _coeffs(), _cfg(), explicit_lane=None, **_presence())
    assert choice.lane == "gemini"
    assert "metered fallback" in choice.reason
    assert "five_hour" in choice.reason or "seven_day" in choice.reason


def test_explicit_lane_bypasses_skip() -> None:
    snap = _snap("claude-5h-at-89.json")
    choice = choose(
        _plan(40, 30.0),
        snap,
        _coeffs(),
        _cfg(),
        explicit_lane="claude",
        **_presence(),
    )
    assert choice.lane == "claude"
    assert choice.reason == "explicit lane"
    claude = next(p for p in choice.projections if p.lane == "claude")
    assert claude.available is False  # projection still shows the skip


def test_explicit_codex_warning() -> None:
    snap = _snap("both-open.json")
    choice = choose(
        _plan(),
        snap,
        _coeffs(),
        _cfg(),
        explicit_lane="codex",
        **_presence(),
    )
    assert choice.lane == "codex"
    assert choice.warning == CODEX_WARNING
    assert any(p.lane == "codex" for p in choice.projections)


def test_project_gemini_unavailable_without_key() -> None:
    """Issue #66: a blank or absent GEMINI_API_KEY makes gemini unavailable."""
    snap = _snap("both-open.json")
    absent = project("gemini", 16, 10.0, 8, _coeffs(), snap, env={}, which=_all_present_which)
    assert absent.available is False
    assert absent.reason == "GEMINI_API_KEY not set"

    blank = project(
        "gemini",
        16,
        10.0,
        8,
        _coeffs(),
        snap,
        env={"GEMINI_API_KEY": "  "},
        which=_all_present_which,
    )
    assert blank.available is False
    assert blank.reason == "GEMINI_API_KEY not set"


def test_project_claude_unavailable_without_cli() -> None:
    """Issue #66: claude is unavailable when the CLI is not on PATH."""
    snap = _snap("both-open.json")
    proj = project(
        "claude", 16, 10.0, 8, _coeffs(), snap, env=ALL_PRESENT_ENV, which=lambda _n: None
    )
    assert proj.available is False
    assert proj.reason == "claude CLI not on PATH"


def test_project_codex_unavailable_without_cli() -> None:
    """Issue #66: codex is unavailable when the CLI is not on PATH."""
    snap = _snap("both-open.json")
    proj = project(
        "codex", 16, 10.0, 8, _coeffs(), snap, env=ALL_PRESENT_ENV, which=lambda _n: None
    )
    assert proj.available is False
    assert proj.reason == "codex CLI not on PATH"


def test_project_presence_reason_wins_over_quota_reason() -> None:
    """Issue #66: presence and quota unavailability combine; presence names the reason."""
    snap = _snap("claude-5h-at-89.json")
    proj = project(
        "claude", 40, 30.0, 8, _coeffs(), snap, skip_percent=90, env={}, which=lambda _n: None
    )
    assert proj.available is False
    assert proj.reason == "claude CLI not on PATH"


def test_choose_falls_back_to_gemini_when_claude_cli_missing() -> None:
    """Issue #66: claude CLI missing makes it a non-candidate; gemini (key set) wins."""
    snap = _snap("both-open.json")
    choice = choose(
        _plan(),
        snap,
        _coeffs(),
        _cfg(),
        explicit_lane=None,
        env=ALL_PRESENT_ENV,
        which=lambda _n: None,
    )
    assert choice.lane == "gemini"
    claude = next(p for p in choice.projections if p.lane == "claude")
    assert claude.available is False
    assert claude.reason == "claude CLI not on PATH"


def test_choose_raises_when_no_lane_available() -> None:
    """Issue #66: claude CLI missing and no Gemini key leaves no candidate."""
    snap = _snap("both-open.json")
    with pytest.raises(NoVisionLane) as excinfo:
        choose(
            _plan(),
            snap,
            _coeffs(),
            _cfg(),
            explicit_lane=None,
            env={},
            which=lambda _n: None,
        )
    message = str(excinfo.value)
    assert "claude" in message
    assert "gemini" in message
    assert "frameweave doctor" in message


def test_choose_explicit_gemini_without_key_raises_naming_lane() -> None:
    """Issue #66 acceptance: explicit --vision gemini without a key fails before stage 1."""
    snap = _snap("both-open.json")
    with pytest.raises(NoVisionLane) as excinfo:
        choose(
            _plan(),
            snap,
            _coeffs(),
            _cfg(),
            explicit_lane="gemini",
            env={},
            which=_all_present_which,
        )
    assert "gemini" in str(excinfo.value)


def test_choose_explicit_codex_without_cli_raises() -> None:
    """Issue #66: an explicit lane with a missing CLI raises, unlike a quota skip."""
    snap = _snap("both-open.json")
    with pytest.raises(NoVisionLane):
        choose(
            _plan(),
            snap,
            _coeffs(),
            _cfg(),
            explicit_lane="codex",
            env=ALL_PRESENT_ENV,
            which=lambda _n: None,
        )


def test_project_defaults_use_os_environ_and_shutil_which() -> None:
    """No env/which override reads the real process environment (default seam)."""
    snap = _snap("both-open.json")
    proj = project("gemini", 16, 10.0, 8, _coeffs(), snap)
    # Whatever this machine's real GEMINI_API_KEY state is, the reason is exact.
    if proj.available:
        assert proj.reason is None
    else:
        assert proj.reason == "GEMINI_API_KEY not set"


def test_choose_ignores_config_vision_lane_for_auto() -> None:
    """explicit_lane=None runs auto rules even when config.vision_lane is named."""
    snap = _snap("both-open.json")
    choice = choose(
        _plan(),
        snap,
        _coeffs(),
        _cfg(vision_lane="gemini"),
        explicit_lane=None,
        **_presence(),
    )
    assert choice.lane == "claude"
    assert choice.reason != "explicit lane"


def test_stale_snapshot_without_refresh_defaults_to_claude() -> None:
    path = FIXTURES / "stale.json"
    missing_script = Path("/nonexistent/frameweave-get-usage.sh")
    snap = load_snapshot(
        path,
        refresh=True,
        refresh_script=missing_script,
        now=STALE_NOW,
    )
    assert snap is None
    choice = choose(_plan(), None, _coeffs(), _cfg(), explicit_lane=None, **_presence())
    assert choice.lane == "claude"
    assert choice.warning == USAGE_UNAVAILABLE_WARNING


def test_refresh_script_failure_defaults_to_claude(tmp_path: Path) -> None:
    """Stale snapshot + refresh script that exits non-zero → unavailable → claude."""
    stale = tmp_path / "stale-snap.json"
    stale.write_text((FIXTURES / "stale.json").read_text(encoding="utf-8"), encoding="utf-8")
    script = tmp_path / "fail-refresh.sh"
    script.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    snap = load_snapshot(
        stale,
        refresh=True,
        refresh_script=script,
        now=STALE_NOW,
    )
    assert snap is None
    choice = choose(_plan(), None, _coeffs(), _cfg(), explicit_lane=None, **_presence())
    assert choice.lane == "claude"
    assert choice.warning == USAGE_UNAVAILABLE_WARNING


def test_missing_snapshot_defaults_to_claude() -> None:
    snap = load_snapshot(
        Path("/nonexistent/usage-snapshot.json"),
        refresh=True,
        refresh_script=Path("/nonexistent/get-usage.sh"),
        now=NOW,
    )
    assert snap is None
    choice = choose(_plan(), None, _coeffs(), _cfg(), **_presence())
    assert choice.lane == "claude"
    assert choice.warning == USAGE_UNAVAILABLE_WARNING


def test_none_snapshot_prints_na_not_zero_before() -> None:
    choice = choose(_plan(), None, _coeffs(), _cfg(), explicit_lane=None, **_presence())
    table = format_projection_table(choice)
    assert "n/a" in table
    assert "five_hour 0.0->" not in table


def test_projection_table_prints_reason_not_no() -> None:
    """Issue #66 decision 3: the availability column shows the reason, never 'no'."""
    snap = _snap("both-open.json")
    choice = choose(
        _plan(),
        snap,
        _coeffs(),
        _cfg(),
        explicit_lane=None,
        env=ALL_PRESENT_ENV,
        which=lambda _n: None,
    )
    table = format_projection_table(choice)
    assert "claude CLI not on PATH" in table
    assert " no\n" not in table
    assert not table.rstrip().endswith(" no")


def test_fable_limit_row_never_used_as_headroom() -> None:
    """limit.* at 95 must not be read as a Claude window; seven_day stays low."""
    snap = _snap("fable-limit-at-95.json")
    assert "limit.weekly_scoped:Fable:2026-09-23T08:00:00.095338+00:00" not in snap.metrics.get(
        "claude", {}
    )
    # seven_day_sonnet is present in the file but is not a chooser window either.
    claude = project("claude", 8, 1.0, 8, _coeffs(), snap, skip_percent=90, **_presence())
    assert set(claude.windows) == {"five_hour", "seven_day"}
    assert claude.windows["seven_day"][0] == pytest.approx(12.0)
    choice = choose(_plan(8, 1.0), snap, _coeffs(), _cfg(), **_presence())
    assert choice.lane == "claude"


def test_recalibrate_single_batch_keeps_a_derives_b(tmp_path: Path) -> None:
    """One batch size: keep packaged a, derive b from tokens_per_call = a + b*fpc."""
    # a=3997, fpc=8 → tpc = 3997 + 2034*8 = 20269 → b = 2034
    files: list[Path] = []
    for i in range(3):
        path = tmp_path / f"cost-{i}.json"
        path.write_text(
            json.dumps(
                {
                    "lane_actual": {
                        "lane": "claude",
                        "frames": 16,
                        "calls": 2,
                        "frames_per_call": 8,
                        "tokens_per_call": 20269,
                        "tokens_per_frame": 2533.625,
                    }
                }
            ),
            encoding="utf-8",
        )
        files.append(path)
    result = recalibrate(files)
    assert result["a"] == pytest.approx(3997)
    assert result["b"] == pytest.approx(2034)
    assert result["c"] == 200
    toml_text = format_recalibrate_toml(result)
    assert "[claude]" in toml_text
    assert "a = 3997" in toml_text


def test_recalibrate_multi_batch_fits_slope(tmp_path: Path) -> None:
    """Two batch sizes: OLS so a is intercept and b is slope (no double-count)."""
    # tpc = 4000 + 2000 * fpc
    samples = ((4, 12000), (8, 20000), (8, 20000))
    files: list[Path] = []
    for i, (fpc, tpc) in enumerate(samples):
        path = tmp_path / f"cost-{i}.json"
        path.write_text(
            json.dumps(
                {
                    "lane_actual": {
                        "lane": "claude",
                        "frames": int(fpc),
                        "calls": 1,
                        "frames_per_call": fpc,
                        "tokens_per_call": tpc,
                    }
                }
            ),
            encoding="utf-8",
        )
        files.append(path)
    result = recalibrate(files)
    assert result["a"] == pytest.approx(4000)
    assert result["b"] == pytest.approx(2000)


def test_codex_coefficients_are_measured_astra() -> None:
    codex = _coeffs()["codex"]
    assert codex["a"] == 4000
    assert codex["b"] == 9582
    assert codex["primary"] == pytest.approx(0.147)


def test_packaged_lanes_toml_loads() -> None:
    coeffs = load_coefficients()
    assert "claude" in coeffs and "gemini" in coeffs and "codex" in coeffs
    assert coeffs["claude"]["a"] == 3997


def test_cli_flags_empty() -> None:
    assert choose_mod.cli_flags() == []


def test_preflight_module_registered() -> None:
    assert "frameweave.vision.choose" in MODULES


def test_preflight_checks_only_for_auto(tmp_path: Path) -> None:
    assert preflight_checks(_cfg(vision_lane="claude")) == []
    missing = tmp_path / "missing-snapshot.json"
    rows = preflight_checks(
        _cfg(vision_lane="auto", usage_snapshot=missing, usage_refresh_script=tmp_path / "x.sh")
    )
    assert len(rows) == 1
    assert rows[0].required is False
    assert rows[0].ok is False


def test_preflight_stale_snapshot_warns(tmp_path: Path) -> None:
    path = tmp_path / "old-snap.json"
    old = (datetime.now(UTC) - timedelta(seconds=700)).strftime("%Y-%m-%dT%H:%M:%SZ")
    path.write_text(
        json.dumps(
            {
                "generatedAt": old,
                "providers": {"claude": {"metrics": [{"id": "five_hour", "percentUsed": 1}]}},
            }
        ),
        encoding="utf-8",
    )
    rows = preflight_checks(
        _cfg(vision_lane="auto", usage_snapshot=path, usage_refresh_script=tmp_path / "x.sh")
    )
    assert len(rows) == 1
    assert rows[0].ok is False
    assert ">600s" in rows[0].detail


def test_lane_actual_quota_delta_uses_chooser_before() -> None:
    before = Snapshot(
        generated_at=NOW,
        metrics={"claude": {"five_hour": 10.0, "seven_day": 20.0}},
    )
    after = Snapshot(
        generated_at=NOW,
        metrics={"claude": {"five_hour": 12.0, "seven_day": 20.5}},
    )
    actual = lane_actual_from_run(
        lane="claude",
        frames=16,
        calls=2,
        tokens=42538,
        before=before,
        after=after,
    )
    assert actual["quota_delta"]["five_hour"] == pytest.approx(2.0)
    assert actual["quota_delta"]["seven_day"] == pytest.approx(0.5)
    assert actual["frames_per_call"] == pytest.approx(8.0)


def test_config_usage_paths_threaded(tmp_path: Path) -> None:
    snap = tmp_path / "custom-snap.json"
    script = tmp_path / "custom-refresh.sh"
    cfg = _cfg(usage_snapshot=snap, usage_refresh_script=script)
    assert cfg.usage_snapshot == snap
    assert cfg.usage_refresh_script == script
