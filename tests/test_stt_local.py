"""WhisperX has one opt-in offline model test; merge/clamp use scripted Segment fixtures.

FakeBackend is kept for issue #14 pipeline tests (see tests/fakes/stt.py).
"""

from __future__ import annotations

import gc
import os
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

from frameweave.stt.audio import Chunk, extract
from frameweave.stt.base import SttResult, SttTimeout, clamp_to_words, merge_chunks
from frameweave.stt.local import (
    WhisperXBackend,
    _faster_whisper_hub_dirname,
    _model_present,
    _segments,
    is_silent,
)
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
    # Kept for #14; merge/clamp below use Segment fixtures, not FakeBackend.
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


def test_segments_counts_and_surfaces_dropped_words_without_times() -> None:
    segments, dropped = _segments(
        [
            {
                "start": 0.0,
                "end": 1.0,
                "text": "hi there",
                "words": [
                    {"word": "hi", "start": 0.0, "end": 0.4},
                    {"word": "there"},
                    {"word": "friend", "start": 0.5, "end": None},
                ],
            }
        ]
    )

    assert dropped == 2
    assert len(segments) == 1
    assert segments[0].words == [Word(0.0, 0.4, "hi", None)]


def test_wall_clock_guard_is_cumulative_before_next_chunk(tmp_path: Path) -> None:
    backend = WhisperXBackend()
    config = _config()
    limit = config.timeout_s * 20
    backend._elapsed_s = limit + 1
    audio = tmp_path / "001.wav"
    audio.write_bytes(b"")

    with pytest.raises(SttTimeout, match="before chunk 001.wav"):
        backend.transcribe(audio, config)


def test_load_model_uses_whisperx_default_vad(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    audio = tmp_path / "chunk.wav"
    audio.write_bytes(b"")
    captured: dict[str, Any] = {}

    def fake_load_model(*args: Any, **kwargs: Any) -> MagicMock:
        captured["args"] = args
        captured["kwargs"] = kwargs
        model = MagicMock()
        model.transcribe.return_value = {"segments": [], "language": "en"}
        return model

    fake_whisperx = SimpleNamespace(
        load_model=fake_load_model,
        load_audio=lambda path: [],
    )
    monkeypatch.setitem(__import__("sys").modules, "whisperx", fake_whisperx)
    monkeypatch.setattr("frameweave.stt.local.duration", lambda path, timeout_s=120.0: 1.0)

    WhisperXBackend().transcribe(audio, _config())

    assert "vad_method" not in captured["kwargs"]


def test_model_present_requires_exact_faster_whisper_hub_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    hub = tmp_path / "hub"
    hub.mkdir()
    # Substring match would wrongly accept this; exact hub dirname must not.
    (hub / "models--other--contains-large-v3-turbo-extra").mkdir()
    monkeypatch.setenv("HF_HUB_CACHE", str(hub))

    ok, detail = _model_present("large-v3-turbo")
    assert not ok
    assert _faster_whisper_hub_dirname("large-v3-turbo") in detail

    (hub / _faster_whisper_hub_dirname("large-v3-turbo")).mkdir()
    ok, matched = _model_present("large-v3-turbo")
    assert ok
    assert matched.endswith(_faster_whisper_hub_dirname("large-v3-turbo"))


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
    assert words, "aligned words must be non-empty"
    assert heard & spoken
    assert all(0 <= word.start <= word.end for word in words)
    assert result.dropped_words == 0
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
