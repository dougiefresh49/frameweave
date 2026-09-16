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
_DIARIZATION_URL = "https://huggingface.co/pyannote/speaker-diarization-3.1"


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
        if segment.speaker not in mapping:
            mapping[segment.speaker] = f"S{len(mapping) + 1}"
        out.append(replace(segment, speaker=mapping[segment.speaker]))
    return out


def make_diarizer(config: Config) -> Callable[[dict[str, Any], Any], dict[str, Any]] | None:
    """Return a WhisperXBackend diarize callable, or None when speakers are off.

    Raises SpeakersUnavailable when speakers are on but HF_TOKEN is missing.
    """
    if not config.speakers:
        return None
    token = os.environ.get("HF_TOKEN")
    if not token or not token.strip():
        raise SpeakersUnavailable(_unavailable_message())

    def _callable(result: dict[str, Any], audio: Any) -> dict[str, Any]:
        labeled = _diarize(audio, result, config, token)
        return _relabel_result(labeled)

    return _callable


def _diarize(
    audio: Path | Any,
    result: dict[str, Any],
    config: Config,
    token: str,
) -> dict[str, Any]:
    from whisperx.diarize import DiarizationPipeline, assign_word_speakers

    pipeline = DiarizationPipeline(token=token, device=config.stt_device)
    source: Any = str(audio) if isinstance(audio, Path) else audio
    diarize_segments = pipeline(source)
    return assign_word_speakers(diarize_segments, result)


def _relabel_result(result: dict[str, Any]) -> dict[str, Any]:
    """Rewrite SPEAKER_* labels in a WhisperX result dict to S1… by first appearance."""
    mapping: dict[str, str] = {}

    def mapped(label: str | None) -> str | None:
        if not label:
            return label
        if label not in mapping:
            mapping[label] = f"S{len(mapping) + 1}"
        return mapping[label]

    for segment in result.get("segments", []):
        if "speaker" in segment:
            segment["speaker"] = mapped(segment.get("speaker"))
        for word in segment.get("words") or []:
            if "speaker" in word:
                word["speaker"] = mapped(word.get("speaker"))
    return result


def _unavailable_message() -> str:
    return (
        "speaker labels need HF_TOKEN with the gated pyannote models accepted at "
        f"{_SEGMENTATION_URL} and {_DIARIZATION_URL}; "
        "export HF_TOKEN or drop --speakers"
    )
