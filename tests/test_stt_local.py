"""WhisperX has one opt-in offline model test; all other tests use scripted segments."""

from __future__ import annotations

import gc
import os
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from frameweave.stt.audio import Chunk, extract
from frameweave.stt.base import SttResult, clamp_to_words, merge_chunks
from frameweave.stt.local import WhisperXBackend, is_silent
from frameweave.types import Segment, Usage, Word
from tests.fakes.stt import FakeBackend


def _result(*segments: Segment, audio_seconds: float = 10) -> SttResult:
    return SttResult(
        segments=list(segments),
        source="stt-whisperx",
        usage=Usage(calls=1, seconds=1.0),
        audio_seconds=audio_seconds,
    )


def _segment(start: float, end: float, text: str, *, words: list[Word] | None = None) -> Segment:
    return Segment(start=start, end=end, text=text, source="stt-whisperx", words=words)


def test_fake_backend_returns_scripted_result(tmp_path: Path) -> None:
    scripted = _result(_segment(0, 1, "scripted"))
    backend = FakeBackend([scripted])

    assert backend.transcribe(tmp_path / "audio.wav", object()) is scripted


def test_merge_chunks_rebases_words_deduplicates_overlap_and_renumbers() -> None:
    first = Chunk(Path("000.wav"), offset_s=0, duration_s=10)
    second = Chunk(Path("001.wav"), offset_s=8, duration_s=10)
    first_result = _result(
        _segment(8.8, 9.4, "from first", words=[Word(8.9, 9.3, "first", 0.9)])
    )
    second_result = _result(
        _segment(0.5, 1.0, "duplicate"),
        _segment(1.5, 2.2, "from second", words=[Word(1.6, 2.1, "second", 0.8)]),
    )

    merged = merge_chunks([(second, second_result), (first, first_result)])

    assert [segment.text for segment in merged] == ["from first", "from second"]
    assert [segment.id for segment in merged] == ["s0001", "s0002"]
    assert merged[1].start == 9.5
    assert merged[1].end == 10.2
    assert merged[1].words == [Word(9.6, 10.1, "second", 0.8)]


def test_clamp_to_words_caps_stretched_edge_word_at_point_nine_seconds() -> None:
    stretched_first = _segment(
        0,
        12,
        "walk now",
        words=[Word(0, 7.78, "walk.", 0.2), Word(8.1, 9, "now", 0.9)],
    )
    stretched_last = _segment(
        0,
        8,
        "go there",
        words=[Word(1, 1.9, "go", 0.9), Word(2.1, 4.1, "there", 0.3)],
    )

    clamped = clamp_to_words([stretched_first, stretched_last])

    assert clamped[0].start == pytest.approx(6.88)
    assert clamped[0].end == 9
    assert clamped[1].start == 1
    assert clamped[1].end == 3.0


def test_clamp_without_words_leaves_segment_bounds_unchanged() -> None:
    segment = _segment(2, 5, "unaligned")
    assert clamp_to_words([segment]) == [segment]


def test_is_silent_recognizes_empty_result() -> None:
    assert is_silent(_result())
    assert not is_silent(_result(_segment(0, 1, "spoken")))


def _slow_tests_enabled() -> bool:
    return os.environ.get("FRAMEWEAVE_SLOW_TESTS") == "1"


def _speech_audio(tmp_path: Path) -> Path:
    if shutil.which("say") is None:
        pytest.skip("macOS say is unavailable")
    source = tmp_path / "speech.aiff"
    subprocess.run(
        [
            "say",
            "-o",
            str(source),
            "Frame weave keeps local speech private. The second sentence checks word alignment.",
        ],
        check=True,
    )
    return extract(source, tmp_path / "audio")


def _config() -> SimpleNamespace:
    return SimpleNamespace(
        stt_model="large-v3-turbo",
        stt_device="cpu",
        speakers=False,
        timeout_s=120.0,
    )


@pytest.mark.slow
@pytest.mark.skipif(not _slow_tests_enabled(), reason="set FRAMEWEAVE_SLOW_TESTS=1")
def test_real_model_transcribes_synthetic_speech_offline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HF_HUB_OFFLINE", "1")
    monkeypatch.setenv("TRANSFORMERS_OFFLINE", "1")
    audio = _speech_audio(tmp_path)

    backend = WhisperXBackend()
    result = backend.transcribe(audio, _config())

    words = [word for segment in result.segments for word in segment.words or []]
    spoken = {"frame", "weave", "local", "speech", "private", "second", "sentence"}
    heard = {word.text.lower().strip(".,!?\"") for word in words}
    assert heard & spoken
    assert all(word.start is not None and word.end is not None for word in words)
    assert all(segment.source == "stt-whisperx" for segment in result.segments)
    assert result.audio_seconds > 0
    assert result.usage.usd == 0
    del backend
    gc.collect()


@pytest.mark.slow
@pytest.mark.skipif(
    not (_slow_tests_enabled() and os.environ.get("FRAMEWEAVE_KICKOFF_DIR")),
    reason="set FRAMEWEAVE_SLOW_TESTS=1 and FRAMEWEAVE_KICKOFF_DIR",
)
def test_kickoff_chunk_zero_is_silent(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HF_HUB_OFFLINE", "1")
    monkeypatch.setenv("TRANSFORMERS_OFFLINE", "1")
    root = Path(os.environ["FRAMEWEAVE_KICKOFF_DIR"])
    candidates = [
        root / "chunks" / "000.wav",
        root / "chunks" / "chunk_000.wav",
        root / "000.wav",
        root / "chunk_000.wav",
    ]
    audio = next((candidate for candidate in candidates if candidate.is_file()), None)
    if audio is None:
        pytest.skip("kickoff chunk 0 was not found")

    assert is_silent(WhisperXBackend().transcribe(audio, _config()))
