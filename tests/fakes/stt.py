"""Scripted speech backend used by STT unit tests; it never loads a model."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from frameweave.stt.base import SttResult


@dataclass
class FakeBackend:
    """Return one scripted result per call."""

    results: list[SttResult]
    name: str = "stt-fake"

    def transcribe(self, audio: Path, config: Any) -> SttResult:
        del audio, config
        if not self.results:
            raise AssertionError("FakeBackend has no scripted result left")
        return self.results.pop(0)
