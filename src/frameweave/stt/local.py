"""Local WhisperX speech-to-text backend."""

from __future__ import annotations

import importlib.util
import logging
import os
import re
import shutil
import subprocess
import time
import warnings
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar

from frameweave.types import Segment, Usage, Word

from .audio import Chunk, duration
from .base import SttResult, SttTimeout, clamp_to_words, merge_chunks, merge_into_presentation

if TYPE_CHECKING:
    from frameweave.config import Check, Config, FlagSpec

_BATCH_SIZE = 8
_CPU_THREADS = 3
_SOURCE = "stt-whisperx"
_WARN_MODULES = (
    "whisperx",
    "torch",
    "torchaudio",
    "torchcodec",
    "lightning",
    "pytorch_lightning",
)
_QUIET_LOGGERS = _WARN_MODULES + ("pyannote", "speechbrain")


class WhisperXBackend:
    """WhisperX large-v3-turbo with forced word alignment."""

    name: ClassVar[str] = _SOURCE

    def __init__(
        self,
        diarize: Callable[[Path, list[Segment]], list[Segment]] | None = None,
    ):
        self.diarize = diarize
        self._model: Any = None
        self._model_key: tuple[str, str, str] | None = None
        self._align_model: Any = None
        self._align_metadata: dict[str, Any] | None = None
        self._align_key: tuple[str, str] | None = None
        self._elapsed_s = 0.0

    def transcribe(self, audio: Path, config: Config) -> SttResult:
        """Transcribe one audio chunk locally and return aligned segments."""
        limit = config.timeout_s * 20
        if self._elapsed_s > limit:
            raise SttTimeout(
                f"local STT exceeded {limit:g}s before chunk {audio.name}; resume with "
                "--stt-device cpu or transcribe fewer minutes via a range"
            )

        started = time.monotonic()
        try:
            return self._transcribe(audio, config, started)
        finally:
            self._elapsed_s += time.monotonic() - started

    def merge_and_diarize(
        self,
        audio: Path,
        chunk_results: list[tuple[Chunk, SttResult]],
    ) -> list[Segment]:
        """Chunk-merge, clamp, diarize (optional), then form presentation spans."""
        segments = clamp_to_words(merge_chunks(chunk_results))
        if self.diarize is not None:
            with _quiet_third_party():
                segments = self.diarize(audio, segments)
        return merge_into_presentation(segments)

    def _transcribe(self, audio: Path, config: Config, started: float) -> SttResult:
        audio_seconds = duration(audio, timeout_s=config.timeout_s)
        with _quiet_third_party():
            import whisperx

            device = config.stt_device
            compute_type = "int8" if device == "cpu" else "default"
            model_key = (config.stt_model, device, compute_type)
            if self._model is None or self._model_key != model_key:
                # Use whisperx's default VAD (assumed: spike 3.4x realtime used the default).
                self._model = whisperx.load_model(
                    config.stt_model,
                    device,
                    compute_type=compute_type,
                    language="en",
                    threads=_CPU_THREADS,
                )
                self._model_key = model_key

            samples = whisperx.load_audio(str(audio))
            transcription = self._model.transcribe(samples, batch_size=_BATCH_SIZE)
            raw_segments = transcription.get("segments", [])
            dropped_words = 0
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
                segments, dropped_words = _segments(aligned.get("segments", []))
                # Keep raw (clamped) segments here. Presentation merge runs only after
                # merge_chunks in merge_and_diarize so overlap dedup never drops a
                # whole 20–40 s span at a chunk boundary.
                segments = clamp_to_words(segments)
            else:
                segments = []

        elapsed = time.monotonic() - started
        return SttResult(
            segments=segments,
            source=self.name,
            usage=Usage(calls=1, seconds=elapsed, usd=0.0),
            audio_seconds=audio_seconds,
            dropped_words=dropped_words,
        )


def is_silent(result: SttResult) -> bool:
    """Return whether a transcription contains no spoken segments."""
    return not result.segments


@contextmanager
def _quiet_third_party() -> Iterator[None]:
    """Silence third-party warnings/logs during model load and transcription."""
    previous_filters = warnings.filters[:]
    previous_disable = logging.root.manager.disable
    previous: dict[str, tuple[int, list[tuple[logging.Handler, int]], bool]] = {}
    for name in _QUIET_LOGGERS:
        logger = logging.getLogger(name)
        previous[name] = (
            logger.level,
            [(handler, handler.level) for handler in logger.handlers],
            logger.propagate,
        )

    try:
        for name in _WARN_MODULES:
            warnings.filterwarnings(
                "ignore",
                module=rf"{re.escape(name)}(\..*)?",
            )
        # pyannote emits the torchcodec UserWarning from its own module path.
        warnings.filterwarnings("ignore", module=r"pyannote(\..*)?")

        # Libraries reconfigure their loggers during import/load (WhisperX
        # setup_logging, Lightning migration). Pin levels and also disable
        # INFO/WARNING globally for the quiet window so mid-load resets lose.
        logging.disable(logging.WARNING)
        for name in _QUIET_LOGGERS:
            logger = logging.getLogger(name)
            logger.setLevel(logging.ERROR)
            for handler in logger.handlers:
                handler.setLevel(logging.ERROR)

        # WhisperX's get_logger() calls setup_logging() when there are no handlers,
        # which resets the level to INFO and attaches a StreamHandler. Seed a
        # NullHandler first so that path never runs during our quiet window.
        whisperx_logger = logging.getLogger("whisperx")
        if not whisperx_logger.handlers:
            whisperx_logger.addHandler(logging.NullHandler())
        whisperx_logger.setLevel(logging.ERROR)
        whisperx_logger.propagate = False
        for handler in whisperx_logger.handlers:
            handler.setLevel(logging.ERROR)

        yield
    finally:
        logging.disable(previous_disable)
        warnings.filters[:] = previous_filters
        for name, (level, handlers, propagate) in previous.items():
            logger = logging.getLogger(name)
            logger.setLevel(level)
            logger.handlers.clear()
            for handler, handler_level in handlers:
                handler.setLevel(handler_level)
                logger.addHandler(handler)
            logger.propagate = propagate


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


def _segments(raw_segments: list[dict[str, Any]]) -> tuple[list[Segment], int]:
    segments: list[Segment] = []
    dropped_words = 0
    for index, raw in enumerate(raw_segments, 1):
        words: list[Word] = []
        for word in raw.get("words", []):
            if word.get("start") is None or word.get("end") is None:
                dropped_words += 1
                continue
            words.append(
                Word(
                    start=float(word["start"]),
                    end=float(word["end"]),
                    text=str(word.get("word", word.get("text", ""))),
                    score=_optional_float(word.get("score")),
                )
            )
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
    return segments, dropped_words


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
    expected = hub / _faster_whisper_hub_dirname(model)
    if expected.is_dir():
        return True, str(expected)
    return False, f"{model} not found under {hub} (expected {expected.name})"


def _faster_whisper_hub_dirname(model: str) -> str:
    """Exact Hugging Face hub directory for a faster-whisper model id or alias."""
    repo_id = model
    try:
        from faster_whisper.utils import _MODELS

        repo_id = _MODELS.get(model, model)
    except ImportError:
        pass
    return f"models--{repo_id.replace('/', '--')}"


def _hub_cache() -> Path:
    if value := os.environ.get("HF_HUB_CACHE"):
        return Path(value).expanduser()
    if value := os.environ.get("HF_HOME"):
        return Path(value).expanduser() / "hub"
    if value := os.environ.get("XDG_CACHE_HOME"):
        return Path(value).expanduser() / "huggingface" / "hub"
    return Path.home() / ".cache" / "huggingface" / "hub"
