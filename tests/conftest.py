"""Shared test fixtures. Sleeps are recorded; the wall clock does not move."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest


@pytest.fixture
def fake_clock() -> Callable[[float], None]:
    sleeps: list[float] = []

    def sleep(seconds: float) -> None:
        sleeps.append(seconds)

    sleep.sleeps = sleeps  # type: ignore[attr-defined]
    return sleep


@pytest.fixture
def fixed_rand() -> Callable[[], float]:
    def rand() -> float:
        return 0.5

    return rand


@pytest.fixture(autouse=True)
def _isolate_usage_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Point chooser usage paths at the test tmp dir so nothing hits the real home.

    Covers Config fields (None → module defaults) and direct load_snapshot calls.
    """
    snap = tmp_path / "usage-snapshot.json"
    script = tmp_path / "get-usage.sh"
    monkeypatch.setattr(
        "frameweave.vision.choose.DEFAULT_SNAPSHOT_PATH",
        snap,
    )
    monkeypatch.setattr(
        "frameweave.vision.choose.DEFAULT_REFRESH_SCRIPT",
        script,
    )
