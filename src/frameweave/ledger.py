"""Request ledger, dated rate table, and cleanup obligations.

``ledger.py`` is the one home for dollar rates. Vision backends import
``RATES_USD_PER_MILLION`` from here; they do not keep their own copy.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from frameweave.types import LedgerEntry

# Pricing snapshot date (docs/audit/pricing-2026-09-15.md).
RATES_DATED = "2026-09-15"

# USD per million tokens: (input, output). Output rate also covers reasoning tokens.
RATES_USD_PER_MILLION: dict[str, tuple[float, float]] = {
    "gemini-3.5-flash-lite": (0.30, 2.50),
    "gemini-3.6-flash": (0.75, 3.75),
}

RATES = RATES_USD_PER_MILLION

# kind → delete(id) hook. Empty in M1; tests register a fake.
DELETERS: dict[str, Callable[[str], None]] = {}

_OBLIGATIONS = "obligations.json"
_LEDGER = "ledger.jsonl"


@dataclass
class Ledger:
    """Append-only request log under ``run_dir/ledger.jsonl`` plus obligations."""

    run_dir: Path
    _reused_usd: float = 0.0
    _entries_this_run: int = 0

    def __post_init__(self) -> None:
        self.run_dir.mkdir(parents=True, exist_ok=True)

    @property
    def path(self) -> Path:
        return self.run_dir / _LEDGER

    def append(self, entry: LedgerEntry) -> None:
        """Persist one request record before the stage is marked done."""
        line = json.dumps(entry.to_dict(), ensure_ascii=False) + "\n"
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(line)
            handle.flush()
            os.fsync(handle.fileno())
        self._entries_this_run += 1

    def add_reused(self, usd: float) -> None:
        """Accumulate dollars from a stage reused from a prior run's ledger."""
        self._reused_usd += float(usd)

    def drop_stage(self, stage: str) -> None:
        """Remove prior ledger lines for ``stage`` so a redo does not double-count."""
        if not self.path.is_file():
            return
        kept: list[str] = []
        removed = 0
        for line in self.path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            entry = LedgerEntry.from_dict(json.loads(line))
            if entry.stage == stage:
                removed += 1
                continue
            kept.append(line)
        if removed == 0:
            return
        # Prior lines that remain are still "prior"; this-run counter stays as-is
        # because dropped lines were never part of ``_entries_this_run``.
        temporary = self.path.with_name(f".{self.path.name}.{os.getpid()}.tmp")
        try:
            temporary.write_text(
                ("\n".join(kept) + ("\n" if kept else "")),
                encoding="utf-8",
            )
            temporary.replace(self.path)
        finally:
            temporary.unlink(missing_ok=True)

    def entries(self) -> list[LedgerEntry]:
        if not self.path.is_file():
            return []
        out: list[LedgerEntry] = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            out.append(LedgerEntry.from_dict(json.loads(line)))
        return out

    def summary(self, rates: dict[str, tuple[float, float]] | None = None) -> dict[str, Any]:
        """Build the ``cost.json`` shape the writer expects.

        ``current_run_usd`` is dollars from entries appended this process.
        ``reused_usd`` is prior-run cost for stages reused now (via ``add_reused``)
        plus older ledger lines not written in this process.
        ``unknown_usd`` counts timeout / ``unknown_usd`` usage rows (as 0 list-price
        but tallied separately when usage carried a dollar estimate, else 0).
        """
        del rates  # dated table is module-level; kept for call-site symmetry
        all_entries = self.entries()
        prior_count = max(0, len(all_entries) - self._entries_this_run)
        prior = all_entries[:prior_count]
        current = all_entries[prior_count:]

        stages_map: dict[str, dict[str, Any]] = {}
        current_usd = 0.0
        unknown_usd = 0.0

        for entry in current:
            current_usd += entry.usage.usd
            if entry.status == "timeout" or entry.usage.unknown_usd:
                unknown_usd += entry.usage.usd
            _accumulate_stage(stages_map, entry)

        reused_usd = self._reused_usd
        for entry in prior:
            reused_usd += entry.usage.usd
            if entry.status == "timeout" or entry.usage.unknown_usd:
                unknown_usd += entry.usage.usd
            _accumulate_stage(stages_map, entry)

        stages = list(stages_map.values())
        total = current_usd + reused_usd
        return {
            "stages": stages,
            "current_run_usd": current_usd,
            "reused_usd": reused_usd,
            "unknown_usd": unknown_usd,
            "total_usd": total,
            "rates_dated": RATES_DATED,
        }

    def register(self, kind: str, upload_id: str) -> None:
        """Record a remote upload id before the request that creates it."""
        items = _load_obligations(self.run_dir)
        items.append({"kind": kind, "id": upload_id})
        _save_obligations(self.run_dir, items)

    def release(self, upload_id: str) -> None:
        """Remove an obligation after the upload is deleted."""
        items = [item for item in _load_obligations(self.run_dir) if item.get("id") != upload_id]
        _save_obligations(self.run_dir, items)

    def reconcile_run(self) -> int:
        """Delete outstanding uploads for this run dir. Returns how many were cleared."""
        return _reconcile_dir(self.run_dir)


def reconcile(cache_dir: Path) -> int:
    """Walk every run dir under ``cache_dir`` and clear outstanding obligations."""
    runs_root = Path(cache_dir) / "runs"
    if not runs_root.is_dir():
        return 0
    cleared = 0
    for run_dir in sorted(p for p in runs_root.rglob("*") if p.is_dir()):
        if (run_dir / _OBLIGATIONS).is_file() or any(run_dir.glob("ledger.jsonl")):
            # Only treat dirs that look like run dirs (have obligations or ledger).
            if (run_dir / _OBLIGATIONS).is_file():
                cleared += _reconcile_dir(run_dir)
    return cleared


def dollars_for_stage(run_dir: Path, stage: str) -> float:
    """Sum ``usage.usd`` for ``stage`` in an existing ledger (sibling reuse)."""
    path = Path(run_dir) / _LEDGER
    if not path.is_file():
        return 0.0
    total = 0.0
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        entry = LedgerEntry.from_dict(json.loads(line))
        if entry.stage == stage:
            total += entry.usage.usd
    return total


def _accumulate_stage(stages_map: dict[str, dict[str, Any]], entry: LedgerEntry) -> None:
    row = stages_map.get(entry.stage)
    if row is None:
        stages_map[entry.stage] = {
            "stage": entry.stage,
            "provider": entry.provider,
            "model": entry.model,
            "tokens_in": entry.usage.tokens_in,
            "tokens_out": entry.usage.tokens_out,
            "tokens_reasoning": entry.usage.tokens_reasoning,
            "seconds": entry.usage.seconds,
            "usd": entry.usage.usd,
        }
        return
    row["tokens_in"] += entry.usage.tokens_in
    row["tokens_out"] += entry.usage.tokens_out
    row["tokens_reasoning"] += entry.usage.tokens_reasoning
    row["seconds"] += entry.usage.seconds
    row["usd"] += entry.usage.usd


def _load_obligations(run_dir: Path) -> list[dict[str, str]]:
    path = run_dir / _OBLIGATIONS
    if not path.is_file():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(data, list):
        return []
    return [item for item in data if isinstance(item, dict)]


def _save_obligations(run_dir: Path, items: list[dict[str, str]]) -> None:
    run_dir.mkdir(parents=True, exist_ok=True)
    path = run_dir / _OBLIGATIONS
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temporary.write_text(json.dumps(items, indent=2) + "\n", encoding="utf-8")
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _reconcile_dir(run_dir: Path) -> int:
    items = _load_obligations(run_dir)
    if not items:
        return 0
    remaining: list[dict[str, str]] = []
    cleared = 0
    for item in items:
        kind = item.get("kind", "")
        upload_id = item.get("id", "")
        deleter = DELETERS.get(kind)
        if deleter is None:
            remaining.append(item)
            continue
        try:
            deleter(upload_id)
            cleared += 1
        except Exception:
            remaining.append(item)
    _save_obligations(run_dir, remaining)
    return cleared
