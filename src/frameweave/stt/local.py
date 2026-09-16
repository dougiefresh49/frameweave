"""Local WhisperX speech-to-text backend."""

from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
import time
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar

from frameweave.types import Segment, Usage, Word

from .audio import duration
from .base import SttResult, SttTimeout, clamp_to_words

if TYPE_CHECKING:
    from frameweave.config import Check, Config, FlagSpec

_BATCH_SIZE = 8
_CPU_THREADS = 3
_SOURCE = "stt-whisperx"


class WhisperXBackend:
    """WhisperX large-v3-turbo with forced word alignment."""

    name: ClassVar[str] = _SOURCE

    def __init__(self, diarize: Callable[[dict[str, Any], Any], dict[str, Any]] | None = None):
        self.diarize = diarize
        self._model: Any = None
        self._model_key: tuple[str, str, str] | None = None
        self._align_model: Any = None
        self._align_metadata: dict[str, Any] | None = None
        self._align_key: tuple[str, str] | None = None

    def transcribe(self, audio: Path, config: Config) -> SttResult:
        """Transcribe one audio chunk locally and return aligned segments."""
        started = time.monotonic()
        audio_seconds = duration(audio)
        import whisperx

        device = config.stt_device
        compute_type = "int8" if device == "cpu" else "default"
        model_key = (config.stt_model, device, compute_type)
        if self._model is None or self._model_key != model_key:
            self._model = whisperx.load_model(
                config.stt_model,
                device,
                compute_type=compute_type,
                language="en",
                threads=_CPU_THREADS,
                vad_method="silero",
            )
            self._model_key = model_key

        samples = whisperx.load_audio(str(audio))
        transcription = self._model.transcribe(samples, batch_size=_BATCH_SIZE)
        raw_segments = transcription.get("segments", [])
        if raw_segments:
            language = transcription.get("language", "en")
            align_key = (language, device)
            if self._align_model is None or self._align_key != align_key:
                self._align_model, self._align_metadata = whisperx.load_align_model(
                    language_code=language,
                    device=device,
                )
                self._align_key = align_key
            aligned = whisperx.align(
                raw_segments,
                self._align_model,
                self._align_metadata,
                samples,
                device,
                return_char_alignments=False,
            )
            if config.speakers and self.diarize is not None:
                aligned = self.diarize(aligned, samples)
            segments = clamp_to_words(_segments(aligned.get("segments", [])))
        else:
            segments = []

        elapsed = time.monotonic() - started
        limit = config.timeout_s * 20
        if elapsed > limit:
            raise SttTimeout(
                f"local STT exceeded {limit:g}s after chunk {audio.name}; resume with "
                "--stt-device cpu or transcribe fewer minutes via a range"
            )
        return SttResult(
            segments=segments,
            source=self.name,
            usage=Usage(calls=1, seconds=elapsed, usd=0.0),
            audio_seconds=audio_seconds,
        )


def is_silent(result: SttResult) -> bool:
    """Return whether a transcription contains no spoken segments."""
    return not result.segments


def cli_flags() -> list[FlagSpec]:
    """Return STT flags for the CLI collector."""
    from frameweave.config import FlagSpec

    return [
        FlagSpec("--stt", "stt_backend", _stt_choice, "Speech backend: local or none."),
        FlagSpec("--stt-device", "stt_device", str, "Local STT device (cpu)."),
        FlagSpec("--stt-model", "stt_model", str, "Local WhisperX model."),
    ]


def preflight_checks(config: Config) -> list[Check]:
    """Report ffmpeg, WhisperX, and local model availability."""
    from frameweave.config import Check

    local_required = config.stt_backend == "local"
    ffmpeg_path = shutil.which("ffmpeg")
    ffmpeg_detail = "not found on PATH"
    ffmpeg_ok = ffmpeg_path is not None
    if ffmpeg_path is not None:
        try:
            completed = subprocess.run(
                [ffmpeg_path, "-version"],
                check=True,
                capture_output=True,
                text=True,
                timeout=5,
            )
            ffmpeg_detail = completed.stdout.splitlines()[0]
        except (OSError, subprocess.SubprocessError, IndexError):
            ffmpeg_ok = False
            ffmpeg_detail = f"could not read version from {ffmpeg_path}"

    whisperx_ok = importlib.util.find_spec("whisperx") is not None
    whisperx_detail = "importable" if whisperx_ok else "not installed"
    model_ok, model_detail = _model_present(config.stt_model)
    download = (
        "uv run python -c \"from faster_whisper.utils import download_model; "
        f"download_model('{config.stt_model}')\""
    )
    return [
        Check("ffmpeg", ffmpeg_ok, ffmpeg_detail, "brew install ffmpeg"),
        Check(
            "whisperx",
            whisperx_ok,
            whisperx_detail,
            "uv sync --extra local-stt",
            required=local_required,
        ),
        Check(
            "WhisperX model weights",
            model_ok,
            model_detail,
            download,
            required=local_required,
        ),
    ]


def _segments(raw_segments: list[dict[str, Any]]) -> list[Segment]:
    segments: list[Segment] = []
    for index, raw in enumerate(raw_segments, 1):
        words = [
            Word(
                start=float(word["start"]),
                end=float(word["end"]),
                text=str(word.get("word", word.get("text", ""))),
                score=_optional_float(word.get("score")),
            )
            for word in raw.get("words", [])
            if word.get("start") is not None and word.get("end") is not None
        ]
        segments.append(
            Segment(
                id=f"s{index:04d}",
                start=float(raw["start"]),
                end=float(raw["end"]),
                text=str(raw.get("text", "")).strip(),
                source=_SOURCE,
                speaker=raw.get("speaker"),
                words=words,
                quality=_optional_float(raw.get("avg_logprob")),
            )
        )
    return segments


def _optional_float(value: object) -> float | None:
    return None if value is None else float(value)


def _stt_choice(value: str) -> str:
    if value not in {"local", "none"}:
        raise ValueError("STT backend must be local or none")
    return value


def _model_present(model: str) -> tuple[bool, str]:
    hub = _hub_cache()
    if not hub.is_dir():
        return False, f"Hugging Face cache not found at {hub}"
    needle = model.lower().replace("/", "--")
    matches = [path for path in hub.iterdir() if path.is_dir() and needle in path.name.lower()]
    if matches:
        return True, str(matches[0])
    return False, f"{model} not found under {hub}"


def _hub_cache() -> Path:
    if value := os.environ.get("HF_HUB_CACHE"):
        return Path(value).expanduser()
    if value := os.environ.get("HF_HOME"):
        return Path(value).expanduser() / "hub"
    if value := os.environ.get("XDG_CACHE_HOME"):
        return Path(value).expanduser() / "huggingface" / "hub"
    return Path.home() / ".cache" / "huggingface" / "hub"
