"""Shared speech-to-text records and chunk result normalization."""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from frameweave.types import Segment, Usage, Word

from .audio import Chunk

if TYPE_CHECKING:
    from frameweave.config import Config

_SENTENCE_END = re.compile(r"[.!?][\"']?$")


@dataclass(frozen=True)
class SttResult:
    """Speech-to-text output and its separately accounted time measures."""

    segments: list[Segment]
    source: str
    usage: Usage
    audio_seconds: float
    dropped_words: int = 0


class SttBackend(Protocol):
    """Backend contract consumed by the transcript stage."""

    name: str

    def transcribe(self, audio: Path, config: Config) -> SttResult: ...


class SttTimeout(TimeoutError):
    """A local transcription exceeded its expanded wall-clock allowance."""


def merge_chunks(results: list[tuple[Chunk, SttResult]]) -> list[Segment]:
    """Rebase chunk-local timestamps, remove overlap duplicates, and assign ids."""
    merged: list[Segment] = []
    previous: Chunk | None = None
    for current, result in sorted(results, key=lambda item: item[0].offset_s):
        cutoff: float | None = None
        if previous is not None:
            previous_end = previous.offset_s + previous.duration_s
            overlap = max(0.0, previous_end - current.offset_s)
            cutoff = previous_end - overlap / 2
        for segment in result.segments:
            rebased = _rebase(segment, current.offset_s)
            if cutoff is not None and rebased.start < cutoff:
                continue
            merged.append(rebased)
        previous = current
    return [replace(segment, id=f"s{index:04d}") for index, segment in enumerate(merged, 1)]


def clamp_to_words(segments: list[Segment]) -> list[Segment]:
    """Tighten segment bounds to aligned words, capping stretched edge words."""
    clamped: list[Segment] = []
    for segment in segments:
        words = segment.words
        if not words:
            clamped.append(segment)
            continue
        first = words[0]
        last = words[-1]
        start = first.start
        end = last.end
        if first.end - first.start > 0.9:
            start = first.end - 0.9
        if last.end - last.start > 0.9:
            end = last.start + 0.9
        if start > end:
            start, end = first.start, min(first.end, first.start + 0.9)
        clamped.append(replace(segment, start=start, end=end))
    return clamped


def merge_into_presentation(
    segments: list[Segment],
    min_s: float = 20.0,
    max_s: float = 40.0,
) -> list[Segment]:
    """Merge short STT segments into 20–40 s presentation spans at sentence ends.

    Never splits a segment. A speaker change closes the current group when either
    side has a speaker label. Words concatenate; quality is the mean of present
    values; source is unchanged; ids are renumbered ``s0001``…
    """
    if not segments:
        return []

    groups: list[list[Segment]] = []
    current: list[Segment] = []

    def span_of(group: list[Segment]) -> float:
        return group[-1].end - group[0].start

    def flush() -> None:
        nonlocal current
        if current:
            groups.append(current)
            current = []

    for segment in segments:
        if current and _speaker_change(current[0].speaker, segment.speaker):
            flush()

        if current and span_of(current) >= min_s:
            proposed = segment.end - current[0].start
            if proposed > max_s:
                flush()

        current.append(segment)
        if span_of(current) >= min_s and _SENTENCE_END.search(segment.text.rstrip()):
            flush()

    flush()
    return [_combine(group, index) for index, group in enumerate(groups, 1)]


def _speaker_change(current: str | None, incoming: str | None) -> bool:
    if current is None and incoming is None:
        return False
    return current != incoming


def _combine(group: list[Segment], index: int) -> Segment:
    words: list[Word] = []
    has_words = False
    qualities: list[float] = []
    for segment in group:
        if segment.words is not None:
            has_words = True
            words.extend(segment.words)
        if segment.quality is not None:
            qualities.append(segment.quality)
    return Segment(
        id=f"s{index:04d}",
        start=group[0].start,
        end=group[-1].end,
        text=" ".join(segment.text for segment in group if segment.text).strip(),
        source=group[0].source,
        speaker=group[0].speaker,
        words=words if has_words else None,
        quality=(sum(qualities) / len(qualities)) if qualities else None,
    )


def _rebase(segment: Segment, offset: float) -> Segment:
    words = None
    if segment.words is not None:
        words = [
            Word(
                start=word.start + offset,
                end=word.end + offset,
                text=word.text,
                score=word.score,
            )
            for word in segment.words
        ]
    return replace(
        segment,
        start=segment.start + offset,
        end=segment.end + offset,
        words=words,
        id=None,
    )
