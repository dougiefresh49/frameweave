"""Ledger summary arithmetic and dated rate table."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from frameweave.ledger import RATES, RATES_DATED, RATES_USD_PER_MILLION, Ledger, reconcile
from frameweave.types import LedgerEntry, Usage


def _entry(
    stage: str = "describe",
    *,
    usd: float = 0.01,
    status: str = "ok",
    unknown: bool = False,
) -> LedgerEntry:
    now = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    return LedgerEntry(
        stage=stage,
        provider="fake",
        model="fake-model",
        started=now,
        ended=now,
        usage=Usage(calls=1, tokens_in=10, tokens_out=5, usd=usd, unknown_usd=unknown),
        status=status,  # type: ignore[arg-type]
    )


def test_rates_table_has_date() -> None:
    assert RATES_DATED
    assert RATES is RATES_USD_PER_MILLION
    assert "gemini-3.5-flash-lite" in RATES_USD_PER_MILLION
    assert RATES_USD_PER_MILLION["gemini-3.5-flash-lite"] == (0.30, 2.50)


def test_summary_current_reused_unknown(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    ledger = Ledger(run_dir)
    ledger.append(_entry(usd=0.02))
    ledger.append(_entry(usd=0.03, status="timeout", unknown=True))
    first = ledger.summary()
    assert first["current_run_usd"] == 0.05
    assert first["reused_usd"] == 0.0
    assert first["unknown_usd"] == 0.03
    assert first["total_usd"] == 0.05
    assert first["rates_dated"] == RATES_DATED
    assert len(first["stages"]) == 1

    # Simulate a later process that reuses the prior ledger lines.
    ledger2 = Ledger(run_dir)
    ledger2.add_reused(0.04)
    ledger2.append(_entry(stage="assemble", usd=0.01))
    second = ledger2.summary()
    assert second["current_run_usd"] == 0.01
    assert second["reused_usd"] == 0.05 + 0.04  # prior lines + explicit
    assert second["total_usd"] == second["current_run_usd"] + second["reused_usd"]


def test_reconcile_calls_deleter(tmp_path: Path) -> None:
    from frameweave import ledger as ledger_mod

    deleted: list[str] = []
    ledger_mod.DELETERS["fake"] = deleted.append
    try:
        run_dir = tmp_path / "runs" / "vid" / "key1"
        led = Ledger(run_dir)
        led.register("fake", "up-1")
        led.register("fake", "up-2")
        assert reconcile(tmp_path) == 2
        assert sorted(deleted) == ["up-1", "up-2"]
        assert reconcile(tmp_path) == 0
    finally:
        ledger_mod.DELETERS.pop("fake", None)
