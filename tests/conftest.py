"""Shared test fixtures. Sleeps are recorded; the wall clock does not move."""

from __future__ import annotations

import shutil
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


@pytest.fixture(autouse=True)
def _isolate_vision_lane_presence(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make lane presence deterministic (issue #66 CI fix).

    A GitHub runner has no ``claude``/``codex`` CLI and no ``GEMINI_API_KEY``,
    so any test that lets ``frameweave.vision.choose`` fall back to its real
    ``os.environ``/``shutil.which`` defaults would see every lane unavailable
    there, while this Mac (both CLIs installed) sees every lane available.
    Patch ``shutil.which`` so ``claude``/``codex`` report present (a fake path)
    while every other name (``ffmpeg``, ``yt-dlp``, ...) still resolves for
    real, and set ``GEMINI_API_KEY`` in the environment the chooser reads.

    Tests that specifically exercise an unavailable lane pass their own
    ``env=``/``which=`` kwargs to ``choose``/``project`` directly (bypassing
    this fixture entirely), or call ``monkeypatch.delenv``/
    ``monkeypatch.setattr("shutil.which", ...)`` in the test body, which runs
    after fixture setup and so overrides it.
    """
    real_which = shutil.which

    def fake_which(name: str, *args: object, **kwargs: object) -> str | None:
        if name in ("claude", "codex"):
            return f"/usr/bin/{name}"
        return real_which(name, *args, **kwargs)

    monkeypatch.setattr(shutil, "which", fake_which)
    monkeypatch.setenv("GEMINI_API_KEY", "test-key-not-real")
