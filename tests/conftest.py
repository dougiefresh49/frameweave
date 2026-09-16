"""Shared test fixtures. Sleeps are recorded; the wall clock does not move."""

from __future__ import annotations

from collections.abc import Callable

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
