"""WhisperX has one opt-in offline model test; merge/clamp use scripted Segment fixtures.

FakeBackend is kept for issue #14 pipeline tests (see tests/fakes/stt.py).
"""

from __future__ import annotations

import gc
import logging
import os
import shutil
import subprocess
import warnings
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

from frameweave.frames import plan
from frameweave.stt.audio import Chunk, extract
from frameweave.stt.base import (
    SttResult,
    SttTimeout,
    clamp_to_words,
    merge_chunks,
    merge_into_presentation,
)
from frameweave.stt.local import (
    WhisperXBackend,
    _faster_whisper_hub_dirname,
    _model_present,
    _quiet_third_party,
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


def _segment(
    start: float,
    end: float,
    text: str,
    *,
    words: list[Word] | None = None,
    speaker: str | None = None,
    quality: float | None = None,
) -> Segment:
    return Segment(
        start=start,
        end=end,
        text=text,
        source="stt-whisperx",
        words=words,
        speaker=speaker,
        quality=quality,
    )


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


def test_merge_into_presentation_four_minute_script_is_six_to_twelve_spans() -> None:
    """#54: 60 × 4 s segments → 6–12 presentation spans of 20–40 s (last may be short)."""
    raw: list[Segment] = []
    for index in range(60):
        start = index * 4.0
        # Sentence end every 7th segment so cuts land inside the 20–40 s window.
        text = "Done." if (index + 1) % 7 == 0 else "keep going"
        words = [
            Word(start, start + 1.5, "keep", 0.9),
            Word(start + 1.5, start + 4.0, "going" if "keep" in text else "Done.", 0.8),
        ]
        raw.append(
            _segment(start, start + 4.0, text, words=words, quality=-0.2 - (index % 3) * 0.1)
        )

    merged = merge_into_presentation(raw)

    assert 6 <= len(merged) <= 12
    assert [segment.id for segment in merged] == [f"s{i:04d}" for i in range(1, len(merged) + 1)]
    for segment in merged[:-1]:
        span = segment.end - segment.start
        assert 20.0 <= span <= 40.0
        assert segment.text.rstrip().endswith((".", "?", "!"))
    assert merged[-1].end - merged[-1].start <= 40.0
    assert all(segment.source == "stt-whisperx" for segment in merged)

    all_words = [word for segment in merged for word in segment.words or []]
    assert len(all_words) == sum(len(segment.words or []) for segment in raw)
    assert all(segment.quality is not None for segment in merged)

    planned = plan(
        merged,
        240.0,
        SimpleNamespace(frame_interval_s=45.0, max_frames=None, frame_width=1280, timeout_s=120.0),
    )
    primaries = [frame for frame in planned.frames if frame.kind == "primary"]
    # About one primary per 30 s of speech (240 / 30 ≈ 8).
    assert 6 <= len(primaries) <= 12


def test_merge_into_presentation_respects_speaker_boundaries() -> None:
    # 6 × 4 s of S1 then 6 × 4 s of S2; sentence end on each speaker's last unit.
    raw: list[Segment] = []
    for index in range(12):
        start = index * 4.0
        speaker = "S1" if index < 6 else "S2"
        text = "Sentence end." if (index + 1) % 6 == 0 else "keep going"
        raw.append(
            _segment(
                start,
                start + 4.0,
                text,
                speaker=speaker,
                words=[Word(start, start + 4.0, "w", 0.9)],
            )
        )

    merged = merge_into_presentation(raw)

    assert len(merged) == 2
    assert [segment.speaker for segment in merged] == ["S1", "S2"]
    assert merged[0].end == 24.0
    assert merged[1].start == 24.0
    assert len(merged[0].words or []) == 6
    assert len(merged[1].words or []) == 6


def test_merge_into_presentation_closes_at_exactly_forty_without_sentence_end() -> None:
    """Finding 5: pin the 40 s close — ten 4 s units flush at max before the 11th."""
    raw = [
        _segment(i * 4.0, i * 4.0 + 4.0, "no stop yet")
        for i in range(12)  # 48 s with no sentence ends
    ]
    merged = merge_into_presentation(raw)
    assert merged[0].start == 0.0
    assert merged[0].end == 40.0
    assert merged[1].start == 40.0
    assert all(segment.end - segment.start <= 40.0 for segment in merged)
    assert len(merged) == 2


def test_merge_into_presentation_enforces_max_before_min_reached() -> None:
    """Finding 3: a 15 s group plus a 30 s segment must not become 45 s."""
    raw = [
        _segment(0.0, 15.0, "short group so far"),
        _segment(15.0, 45.0, "long unit that would overshoot"),
    ]
    merged = merge_into_presentation(raw)
    assert [segment.end - segment.start for segment in merged] == [15.0, 30.0]
    assert [segment.text for segment in merged] == [
        "short group so far",
        "long unit that would overshoot",
    ]


def test_merge_into_presentation_passes_sixty_second_unit_unsplit() -> None:
    """Finding 5: never split a segment — a lone 60 s unit stays one span."""
    raw = [_segment(0.0, 60.0, "one very long whisper segment without breaks.")]
    merged = merge_into_presentation(raw)
    assert len(merged) == 1
    assert merged[0].start == 0.0
    assert merged[0].end == 60.0


def test_merge_into_presentation_quality_is_mean_of_constituents() -> None:
    """Finding 5: quality mean asserted numerically."""
    raw = [
        _segment(0.0, 4.0, "a", quality=-0.2),
        _segment(4.0, 8.0, "b", quality=-0.4),
        _segment(8.0, 12.0, "c", quality=-0.6),
        _segment(12.0, 16.0, "d", quality=-0.8),
        _segment(16.0, 20.0, "Done.", quality=-1.0),
    ]
    merged = merge_into_presentation(raw)
    assert len(merged) == 1
    assert merged[0].quality == pytest.approx((-0.2 - 0.4 - 0.6 - 0.8 - 1.0) / 5)


def test_merge_and_diarize_merges_only_after_chunk_join(tmp_path: Path) -> None:
    """Finding 1: presentation merge after merge_chunks keeps chunk-N+1 post-cutoff text.

    If each chunk were presentation-merged first, chunk 2's first ~38 s span would
    start before the overlap midpoint and be dropped entirely.
    """
    backend = WhisperXBackend()
    first = Chunk(tmp_path / "000.wav", offset_s=0.0, duration_s=900.0)
    second = Chunk(tmp_path / "001.wav", offset_s=898.0, duration_s=900.0)
    # Raw shorts at the end of chunk 1 (not pre-merged into a long span).
    first_segs = [
        *[_segment(float(860 + i * 4), float(864 + i * 4), "keep going") for i in range(9)],
        _segment(896.0, 900.0, "End one."),
    ]
    # Chunk 2 local: overlap dup then unique shorts that must survive + pad to 20–40 s.
    second_segs = [
        _segment(0.5, 1.0, "overlap dup"),  # rebased 898.5 < cutoff 899 → dropped
        _segment(1.5, 5.5, "kept after boundary"),  # rebased 899.5 ≥ 899 → kept
        *[_segment(5.5 + i * 4.0, 9.5 + i * 4.0, "keep going") for i in range(7)],
        _segment(33.5, 37.5, "Second chunk close."),
    ]
    first_result = _result(*first_segs, audio_seconds=900.0)
    second_result = _result(*second_segs, audio_seconds=900.0)

    merged = backend.merge_and_diarize(
        tmp_path / "full.wav", [(first, first_result), (second, second_result)]
    )

    texts = " ".join(segment.text for segment in merged)
    assert "kept after boundary" in texts
    assert "Second chunk close." in texts
    assert "overlap dup" not in texts
    assert any(20.0 <= segment.end - segment.start <= 40.0 for segment in merged)


def test_transcribe_returns_raw_clamped_segments_not_presentation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Finding 1: _transcribe must not call merge_into_presentation."""
    audio = tmp_path / "chunk.wav"
    audio.write_bytes(b"")
    raw = [
        {
            "start": float(i * 4),
            "end": float(i * 4 + 4),
            "text": "keep going",
            "avg_logprob": -0.2,
            "words": [
                {"word": "keep", "start": float(i * 4), "end": float(i * 4 + 2), "score": 0.9},
                {"word": "going", "start": float(i * 4 + 2), "end": float(i * 4 + 4), "score": 0.8},
            ],
        }
        for i in range(10)
    ]

    model = MagicMock()
    model.transcribe.return_value = {"segments": raw, "language": "en"}
    fake_whisperx = SimpleNamespace(
        load_model=lambda *a, **k: model,
        load_audio=lambda path: [],
        load_align_model=lambda **k: (MagicMock(), {}),
        align=lambda *a, **k: {"segments": raw},
    )
    monkeypatch.setitem(__import__("sys").modules, "whisperx", fake_whisperx)
    monkeypatch.setattr("frameweave.stt.local.duration", lambda path, timeout_s=120.0: 40.0)

    result = WhisperXBackend().transcribe(audio, _config())

    # Ten raw units, not collapsed into a few 20–40 s presentation spans.
    assert len(result.segments) == 10
    assert all(segment.end - segment.start <= 4.0 for segment in result.segments)


def test_merge_and_diarize_quiets_diarization(
    tmp_path: Path,
) -> None:
    """Finding 4: diarize runs inside the quiet window."""
    from dataclasses import replace

    inside: dict[str, int] = {}

    def fake_diarize(audio: Path, segments: list[Segment]) -> list[Segment]:
        del audio
        inside["pyannote"] = logging.getLogger("pyannote").level
        inside["torch"] = logging.getLogger("torch").level
        return [replace(segment, speaker="S1") for segment in segments]

    backend = WhisperXBackend(diarize=fake_diarize)
    chunk = Chunk(tmp_path / "000.wav", offset_s=0.0, duration_s=10.0)
    result = _result(_segment(0.0, 4.0, "hello there."))
    labeled = backend.merge_and_diarize(tmp_path / "full.wav", [(chunk, result)])

    assert inside["pyannote"] == logging.ERROR
    assert inside["torch"] == logging.ERROR
    assert labeled[0].speaker == "S1"


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


def test_transcribe_quiets_third_party_warnings_and_restores(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#55: filters and logger levels are installed around load/transcribe, then restored."""
    audio = tmp_path / "chunk.wav"
    audio.write_bytes(b"")
    inside: dict[str, Any] = {}

    whisperx_logger = logging.getLogger("whisperx")
    torch_logger = logging.getLogger("torch")
    pyannote_logger = logging.getLogger("pyannote")
    before_levels = {
        "whisperx": whisperx_logger.level,
        "torch": torch_logger.level,
        "pyannote": pyannote_logger.level,
    }
    before_filters = warnings.filters[:]

    def fake_load_model(*args: Any, **kwargs: Any) -> MagicMock:
        inside["whisperx_level"] = logging.getLogger("whisperx").level
        inside["torch_level"] = logging.getLogger("torch").level
        inside["pyannote_level"] = logging.getLogger("pyannote").level
        inside["filters"] = warnings.filters[:]
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

    assert inside["whisperx_level"] == logging.ERROR
    assert inside["torch_level"] == logging.ERROR
    assert inside["pyannote_level"] == logging.ERROR
    assert any(
        len(item) >= 4 and item[3] is not None and "whisperx" in str(item[3].pattern)
        for item in inside["filters"]
        if isinstance(item, tuple)
    )
    assert logging.getLogger("whisperx").level == before_levels["whisperx"]
    assert logging.getLogger("torch").level == before_levels["torch"]
    assert logging.getLogger("pyannote").level == before_levels["pyannote"]
    assert warnings.filters == before_filters


def test_quiet_third_party_context_restores_outside() -> None:
    logger = logging.getLogger("lightning")
    previous = logger.level
    logger.setLevel(logging.DEBUG)
    try:
        with _quiet_third_party():
            assert logger.level == logging.ERROR
        assert logger.level == logging.DEBUG
    finally:
        logger.setLevel(previous)


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
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
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
    captured = capsys.readouterr()
    noisy = ("UserWarning", "FutureWarning", "INFO:", "libavutil", "lightning", "torchcodec")
    combined = captured.out + captured.err
    assert not any(token in combined for token in noisy), combined
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
