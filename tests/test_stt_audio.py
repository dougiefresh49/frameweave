"""Audio tests generate fixed-length wavs at test time; no media fixture is committed."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from frameweave.stt.audio import SttError, chunk, duration, extract

# Fixed chunking constants so offsets never depend on runner clip length.
_CHUNK_S = 12.0
_OVERLAP_S = 2.0


def _spoken_clip(tmp_path: Path) -> Path:
    if shutil.which("say") is None:
        pytest.skip("macOS say is unavailable")
    source = tmp_path / "speech.aiff"
    subprocess.run(
        ["say", "-o", str(source), "Frame weave keeps spoken audio on this computer."],
        check=True,
    )
    return extract(source, tmp_path / "extracted")


def _tone_clip(tmp_path: Path, *, duration_s: float, name: str = "tone.wav") -> Path:
    """Write a fixed-length 16 kHz mono PCM wav via ffmpeg lavfi sine."""
    path = tmp_path / name
    subprocess.run(
        [
            "ffmpeg",
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            f"sine=frequency=440:duration={duration_s}",
            "-ac",
            "1",
            "-ar",
            "16000",
            "-c:a",
            "pcm_s16le",
            str(path),
        ],
        check=True,
    )
    return path


def _probe(path: Path) -> dict:
    completed = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "a:0",
            "-show_entries",
            "stream=sample_rate,channels",
            "-of",
            "json",
            str(path),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(completed.stdout)["streams"][0]


def test_extract_produces_atomic_16khz_mono_pcm(tmp_path: Path) -> None:
    audio = _spoken_clip(tmp_path)

    assert audio == tmp_path / "extracted" / "audio.wav"
    assert _probe(audio) == {"sample_rate": "16000", "channels": 1}
    assert not list(audio.parent.glob("*.partial*"))


def test_chunk_offsets_overlap_and_rejoin_two_copies(tmp_path: Path) -> None:
    audio = _tone_clip(tmp_path, duration_s=30.0)
    step = _CHUNK_S - _OVERLAP_S

    chunks = chunk(
        audio, tmp_path / "work", chunk_s=_CHUNK_S, overlap_s=_OVERLAP_S
    )

    assert len(chunks) == 3
    assert [item.path.name for item in chunks] == ["000.wav", "001.wav", "002.wav"]
    assert chunks[0].offset_s == 0
    assert chunks[0].duration_s == pytest.approx(_CHUNK_S)
    assert chunks[1].offset_s == pytest.approx(step)
    assert chunks[1].duration_s == pytest.approx(_CHUNK_S)
    assert chunks[2].offset_s == pytest.approx(2 * step)
    assert chunks[2].duration_s == pytest.approx(duration(audio) - chunks[2].offset_s)
    assert all(item.path.is_file() for item in chunks)


def test_short_audio_yields_one_chunk_at_zero(tmp_path: Path) -> None:
    audio = _tone_clip(tmp_path, duration_s=5.0, name="short.wav")

    chunks = chunk(audio, tmp_path / "work", chunk_s=_CHUNK_S, overlap_s=_OVERLAP_S)

    assert len(chunks) == 1
    assert chunks[0].offset_s == 0
    assert chunks[0].duration_s == pytest.approx(duration(audio))
    assert chunks[0].path == tmp_path / "work" / "chunks" / "000.wav"


def test_ffmpeg_failure_raises_stt_error_with_stderr(tmp_path: Path) -> None:
    with patch("frameweave.stt.audio.subprocess.run") as run:
        run.side_effect = subprocess.CalledProcessError(
            1,
            ["ffmpeg"],
            stderr="No such file or directory",
        )
        with pytest.raises(SttError, match="No such file or directory") as raised:
            extract(tmp_path / "missing.mp4", tmp_path / "out")
    assert isinstance(raised.value.__cause__, subprocess.CalledProcessError)


def test_ffmpeg_timeout_raises_stt_error(tmp_path: Path) -> None:
    with patch("frameweave.stt.audio.subprocess.run") as run:
        run.side_effect = subprocess.TimeoutExpired(cmd=["ffmpeg"], timeout=0.01)
        with pytest.raises(SttError, match="timed out after 0.01s") as raised:
            extract(tmp_path / "clip.mp4", tmp_path / "out", timeout_s=0.01)
    assert isinstance(raised.value.__cause__, subprocess.TimeoutExpired)
