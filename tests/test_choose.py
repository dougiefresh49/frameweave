"""Tests for the usage-aware lane chooser (issue #26).

Fixtures under tests/fixtures/usage-snapshot/ are hand-written from the
AgentUsageBar snapshot shape (generatedAt + providers.*.metrics with
percentUsed). No live refresh or provider API is called.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from frameweave.config import load
from frameweave.vision import choose as choose_mod
from frameweave.vision.choose import (
    CODEX_WARNING,
    COEFFICIENTS,
    USAGE_UNAVAILABLE_WARNING,
    Plan,
    choose,
    format_recalibrate_toml,
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
    claude = project("claude", 16, 10.0, 8, COEFFICIENTS, snap, skip_percent=90)
    assert claude.calls == 2
    assert claude.tokens == 42538
    assert claude.windows["five_hour"][0] == pytest.approx(10.0)
    assert claude.windows["five_hour"][1] == pytest.approx(10.7061308)
    assert claude.windows["seven_day"][1] == pytest.approx(20.170152)
    assert claude.available is True

    gemini = project("gemini", 16, 10.0, 8, COEFFICIENTS, snap, skip_percent=90)
    assert gemini.tokens == 22840
    assert gemini.usd == pytest.approx(0.011652)
    assert gemini.windows == {}
    assert gemini.available is True


def test_both_open_picks_claude_by_headroom() -> None:
    """Only Claude is a subscription auto-candidate (decision 47); it wins when open."""
    snap = _snap("both-open.json")
    choice = choose(_plan(), snap, COEFFICIENTS, _cfg(), explicit_lane=None)
    assert choice.lane == "claude"
    assert choice.warning is None
    assert any(p.lane == "gemini" for p in choice.projections)


def test_claude_5h_crossing_falls_to_gemini() -> None:
    """Claude at 89% with a run that crosses 90 is skipped; metered Gemini wins."""
    snap = _snap("claude-5h-at-89.json")
    # 40 frames / 8 = 5 calls → ~107k tokens → five_hour delta ~1.8 → after ~90.8
    choice = choose(_plan(40, 30.0), snap, COEFFICIENTS, _cfg(), explicit_lane=None)
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
    choice = choose(plan, snap, COEFFICIENTS, cfg75, explicit_lane=None)
    assert choice.lane == "gemini"

    cfg90 = _cfg(lane_skip_percent=90)
    open_choice = choose(plan, snap, COEFFICIENTS, cfg90, explicit_lane=None)
    assert open_choice.lane == "claude"

    # Env var path: FRAMEWEAVE_LANE_SKIP_PERCENT
    from_env = load(
        flags={"vision_lane": "auto", "frames_per_call": 8},
        env={"FRAMEWEAVE_LANE_SKIP_PERCENT": "75"},
        toml_path=MISSING_TOML,
        dotenv_paths=[],
    )
    assert from_env.lane_skip_percent == 75
    assert choose(plan, snap, COEFFICIENTS, from_env).lane == "gemini"


def test_both_crossing_returns_gemini_with_reason() -> None:
    snap = _snap("both-crossing.json")
    choice = choose(_plan(40, 30.0), snap, COEFFICIENTS, _cfg(), explicit_lane=None)
    assert choice.lane == "gemini"
    assert "five_hour" in choice.reason or "seven_day" in choice.reason or "claude" in choice.reason


def test_explicit_lane_bypasses_skip() -> None:
    snap = _snap("claude-5h-at-89.json")
    choice = choose(
        _plan(40, 30.0),
        snap,
        COEFFICIENTS,
        _cfg(vision_lane="claude"),
        explicit_lane="claude",
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
        COEFFICIENTS,
        _cfg(vision_lane="codex"),
        explicit_lane="codex",
    )
    assert choice.lane == "codex"
    assert choice.warning == CODEX_WARNING
    assert any(p.lane == "codex" for p in choice.projections)


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
    choice = choose(_plan(), None, COEFFICIENTS, _cfg(), explicit_lane=None)
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
    choice = choose(_plan(), None, COEFFICIENTS, _cfg())
    assert choice.lane == "claude"
    assert choice.warning == USAGE_UNAVAILABLE_WARNING


def test_fable_limit_row_never_used_as_headroom() -> None:
    """limit.* at 95 must not be read as a Claude window; seven_day stays low."""
    snap = _snap("fable-limit-at-95.json")
    assert "limit.weekly_scoped:Fable:2026-09-23T08:00:00.095338+00:00" not in snap.metrics.get(
        "claude", {}
    )
    # seven_day_sonnet is present in the file but is not a chooser window either.
    claude = project("claude", 8, 1.0, 8, COEFFICIENTS, snap, skip_percent=90)
    assert set(claude.windows) == {"five_hour", "seven_day"}
    assert claude.windows["seven_day"][0] == pytest.approx(12.0)
    choice = choose(_plan(8, 1.0), snap, COEFFICIENTS, _cfg())
    assert choice.lane == "claude"


def test_recalibrate_medians(tmp_path: Path) -> None:
    files: list[Path] = []
    # tokens_per_frame: 10, 20, 30 → median 20; tokens_per_call: 100, 200, 300 → 200
    for i, (tpf, tpc) in enumerate(((10, 100), (20, 200), (30, 300))):
        path = tmp_path / f"cost-{i}.json"
        path.write_text(
            json.dumps(
                {
                    "lane_actual": {
                        "lane": "claude",
                        "tokens_per_frame": tpf,
                        "tokens_per_call": tpc,
                    }
                }
            ),
            encoding="utf-8",
        )
        files.append(path)
    result = recalibrate(files)
    assert result["a"] == 200
    assert result["b"] == 20
    assert result["c"] == 200
    toml_text = format_recalibrate_toml(result)
    assert "[claude]" in toml_text
    assert "a = 200" in toml_text
    assert "b = 20" in toml_text


def test_cli_flags_empty() -> None:
    assert choose_mod.cli_flags() == []


def test_preflight_checks_only_for_auto(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    assert preflight_checks(_cfg(vision_lane="claude")) == []
    missing = tmp_path / "missing-snapshot.json"
    monkeypatch.setattr(choose_mod, "DEFAULT_SNAPSHOT_PATH", missing)
    rows = preflight_checks(_cfg(vision_lane="auto"))
    assert len(rows) == 1
    assert rows[0].required is False
    assert rows[0].ok is False
