"""Audio tests use speech synthesized at test time; no media fixture is committed."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from frameweave.stt.audio import chunk, duration, extract


def _spoken_clip(tmp_path: Path) -> Path:
    if shutil.which("say") is None:
        pytest.skip("macOS say is unavailable")
    source = tmp_path / "speech.aiff"
    subprocess.run(
        ["say", "-o", str(source), "Frame weave keeps spoken audio on this computer."],
        check=True,
    )
    return extract(source, tmp_path / "extracted")


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
    audio = _spoken_clip(tmp_path)
    clip_s = duration(audio)
    doubled = tmp_path / "double.wav"
    subprocess.run(
        [
            "ffmpeg",
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(audio),
            "-i",
            str(audio),
            "-filter_complex",
            "[0:a][1:a]concat=n=2:v=0:a=1",
            "-c:a",
            "pcm_s16le",
            str(doubled),
        ],
        check=True,
    )
    chunk_s = clip_s + 0.2
    overlap_s = 0.4

    chunks = chunk(doubled, tmp_path / "work", chunk_s=chunk_s, overlap_s=overlap_s)

    assert len(chunks) == 2
    assert chunks[0].path.name == "000.wav"
    assert chunks[0].offset_s == 0
    assert chunks[0].duration_s == pytest.approx(chunk_s)
    assert chunks[1].path.name == "001.wav"
    assert chunks[1].offset_s == pytest.approx(chunk_s - overlap_s)
    assert chunks[1].duration_s == pytest.approx(duration(doubled) - chunks[1].offset_s)
    assert all(item.path.is_file() for item in chunks)


def test_short_audio_yields_one_chunk_at_zero(tmp_path: Path) -> None:
    audio = _spoken_clip(tmp_path)

    chunks = chunk(audio, tmp_path / "work", chunk_s=duration(audio) + 1)

    assert len(chunks) == 1
    assert chunks[0].offset_s == 0
    assert chunks[0].duration_s == pytest.approx(duration(audio))
    assert chunks[0].path == tmp_path / "work" / "chunks" / "000.wav"
