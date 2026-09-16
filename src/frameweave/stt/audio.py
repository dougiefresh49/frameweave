"""Audio extraction, probing, and overlapping chunks for speech-to-text."""

from __future__ import annotations

import subprocess
import uuid
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Chunk:
    """One audio chunk and its position in the original audio."""

    path: Path
    offset_s: float
    duration_s: float


def extract(media: Path, dest_dir: Path) -> Path:
    """Extract 16 kHz mono PCM audio to ``dest_dir/audio.wav`` atomically."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    target = dest_dir / "audio.wav"
    partial = _partial_path(target)
    try:
        _run(
            [
                "ffmpeg",
                "-nostdin",
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-i",
                str(media),
                "-vn",
                "-ac",
                "1",
                "-ar",
                "16000",
                "-c:a",
                "pcm_s16le",
                str(partial),
            ]
        )
        partial.replace(target)
    finally:
        partial.unlink(missing_ok=True)
    return target


def duration(path: Path) -> float:
    """Return media duration in seconds using ffprobe."""
    completed = _run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(path),
        ],
        capture_output=True,
    )
    try:
        return float(completed.stdout.strip())
    except ValueError as exc:
        raise ValueError(f"ffprobe returned no duration for {path}") from exc


def chunk(
    audio: Path,
    dest_dir: Path,
    chunk_s: float = 900,
    overlap_s: float = 2.0,
) -> list[Chunk]:
    """Cut audio into overlapping WAV chunks using stream copy."""
    if chunk_s <= 0:
        raise ValueError("chunk_s must be positive")
    if overlap_s < 0 or overlap_s >= chunk_s:
        raise ValueError("overlap_s must be non-negative and smaller than chunk_s")

    audio_duration = duration(audio)
    chunks_dir = dest_dir / "chunks"
    chunks_dir.mkdir(parents=True, exist_ok=True)
    offsets = [0.0]
    step = chunk_s - overlap_s
    while offsets[-1] + chunk_s < audio_duration:
        offsets.append(offsets[-1] + step)

    chunks: list[Chunk] = []
    for index, offset in enumerate(offsets):
        length = min(chunk_s, max(0.0, audio_duration - offset))
        target = chunks_dir / f"{index:03d}.wav"
        partial = _partial_path(target)
        try:
            _run(
                [
                    "ffmpeg",
                    "-nostdin",
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-y",
                    "-ss",
                    _number(offset),
                    "-i",
                    str(audio),
                    "-t",
                    _number(length),
                    "-c",
                    "copy",
                    str(partial),
                ]
            )
            partial.replace(target)
        finally:
            partial.unlink(missing_ok=True)
        chunks.append(Chunk(path=target, offset_s=offset, duration_s=length))
    return chunks


def _partial_path(target: Path) -> Path:
    return target.with_name(f".{target.stem}.{uuid.uuid4().hex}.partial{target.suffix}")


def _run(args: list[str], *, capture_output: bool = False) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args,
        check=True,
        text=True,
        capture_output=capture_output,
    )


def _number(value: float) -> str:
    return f"{value:.6f}".rstrip("0").rstrip(".")
