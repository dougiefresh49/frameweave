"""Speaker diarization via WhisperX / pyannote.

Opt-in behind ``config.speakers``. The ``--speakers`` flag lives on config; this
module only supplies the diarization callable, relabeling, and doctor checks.
"""

from __future__ import annotations

import importlib.util
import os
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, Any

from frameweave.types import Segment

if TYPE_CHECKING:
    from frameweave.config import Check, Config, FlagSpec

_SEGMENTATION_URL = "https://huggingface.co/pyannote/segmentation-3.0"
_DIARIZATION_MODEL = "pyannote/speaker-diarization-community-1"
_DIARIZATION_URL = f"https://huggingface.co/{_DIARIZATION_MODEL}"


class SpeakersUnavailable(RuntimeError):
    """``--speakers`` was set but Hugging Face auth or model access is missing."""


def cli_flags() -> list[FlagSpec]:
    """``--speakers`` is owned by config; this module adds none."""
    return []


def preflight_checks(config: Config) -> list[Check]:
    """When speakers are on, report HF_TOKEN and pyannote.audio availability."""
    from frameweave.config import Check

    if not config.speakers:
        return []

    token = os.environ.get("HF_TOKEN")
    token_ok = bool(token and token.strip())
    token_remedy = (
        f"export HF_TOKEN=<token with access to {_SEGMENTATION_URL} and "
        f"{_DIARIZATION_URL}; or drop --speakers"
    )
    pyannote_ok = importlib.util.find_spec("pyannote.audio") is not None
    return [
        Check(
            "HF_TOKEN",
            token_ok,
            "set" if token_ok else "missing",
            token_remedy,
        ),
        Check(
            "pyannote.audio",
            pyannote_ok,
            "importable" if pyannote_ok else "not installed",
            "uv sync --extra speakers",
        ),
    ]


def diarize(audio: Path, result: dict[str, Any], config: Config, token: str) -> dict[str, Any]:
    """Run pyannote diarization and assign speakers onto aligned WhisperX segments."""
    return _diarize(audio, result, config, token)


def relabel(segments: list[Segment]) -> list[Segment]:
    """Map pyannote ``SPEAKER_00``… labels to ``S1``… by first appearance."""
    mapping: dict[str, str] = {}
    out: list[Segment] = []
    for segment in segments:
        if not segment.speaker:
            out.append(segment)
            continue
        out.append(replace(segment, speaker=_s_label(segment.speaker, mapping)))
    return out


def make_diarizer(config: Config) -> Callable[[Path, list[Segment]], list[Segment]] | None:
    """Return a post-merge diarize callable, or None when speakers are off.

    The callable takes the full audio path and merged segments, runs pyannote
    once, and returns segments with ``S1``… labels. Raises SpeakersUnavailable
    when speakers are on but HF_TOKEN is missing.
    """
    if not config.speakers:
        return None
    token = os.environ.get("HF_TOKEN")
    if not token or not token.strip():
        raise SpeakersUnavailable(_unavailable_message())

    def _callable(audio: Path, segments: list[Segment]) -> list[Segment]:
        result = _segments_as_result(segments)
        labeled = _diarize(audio, result, config, token)
        with_raw = _speakers_onto(segments, labeled)
        return relabel(with_raw)

    return _callable


def _s_label(raw: str, mapping: dict[str, str]) -> str:
    """Map one pyannote label to ``S<n>`` by first-appearance order."""
    if raw not in mapping:
        mapping[raw] = f"S{len(mapping) + 1}"
    return mapping[raw]


def _diarize(
    audio: Path | Any,
    result: dict[str, Any],
    config: Config,
    token: str,
) -> dict[str, Any]:
    from whisperx.diarize import DiarizationPipeline, assign_word_speakers

    pipeline = DiarizationPipeline(
        model_name=_DIARIZATION_MODEL,
        token=token,
        device=config.stt_device,
    )
    source: Any = str(audio) if isinstance(audio, Path) else audio
    diarize_segments = pipeline(source)
    return assign_word_speakers(diarize_segments, result)


def _relabel_result(result: dict[str, Any]) -> dict[str, Any]:
    """Rewrite SPEAKER_* labels in a WhisperX result dict to S1… by first appearance."""
    mapping: dict[str, str] = {}
    for segment in result.get("segments", []):
        if "speaker" in segment:
            speaker = segment.get("speaker")
            segment["speaker"] = _s_label(speaker, mapping) if speaker else speaker
        for word in segment.get("words") or []:
            if "speaker" in word:
                speaker = word.get("speaker")
                word["speaker"] = _s_label(speaker, mapping) if speaker else speaker
    return result


def _segments_as_result(segments: list[Segment]) -> dict[str, Any]:
    return {
        "segments": [
            {
                "start": segment.start,
                "end": segment.end,
                "text": segment.text,
                "words": [
                    {
                        "start": word.start,
                        "end": word.end,
                        "word": word.text,
                        **({"score": word.score} if word.score is not None else {}),
                    }
                    for word in (segment.words or [])
                ],
            }
            for segment in segments
        ]
    }


def _speakers_onto(segments: list[Segment], result: dict[str, Any]) -> list[Segment]:
    raw_segments = result.get("segments", [])
    return [
        replace(segment, speaker=raw.get("speaker"))
        for segment, raw in zip(segments, raw_segments, strict=True)
    ]


def _unavailable_message() -> str:
    return (
        "speaker labels need HF_TOKEN with the gated pyannote models accepted at "
        f"{_SEGMENTATION_URL} and {_DIARIZATION_URL}; "
        "export HF_TOKEN or drop --speakers"
    )
