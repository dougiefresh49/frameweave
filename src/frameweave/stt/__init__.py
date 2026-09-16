"""Speech-to-text interfaces and the local WhisperX backend."""

from .audio import Chunk, chunk, duration, extract
from .base import SttBackend, SttResult, SttTimeout, clamp_to_words, merge_chunks
from .local import WhisperXBackend, cli_flags, is_silent, preflight_checks

__all__ = [
    "Chunk",
    "SttBackend",
    "SttResult",
    "SttTimeout",
    "WhisperXBackend",
    "chunk",
    "clamp_to_words",
    "cli_flags",
    "duration",
    "extract",
    "is_silent",
    "merge_chunks",
    "preflight_checks",
]
