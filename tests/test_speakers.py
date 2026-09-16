"""Speaker diarization: relabel, preflight, and an opt-in slow two-speaker fixture.

The slow test cuts a 60 s window from Doug's kickoff wrap-up chunk at test time
(path via FRAMEWEAVE_KICKOFF_DIR only; nothing from that folder is logged or
committed). Skip when the env var or file is missing, or when pyannote weights
are not in the Hugging Face cache.
"""

from __future__ import annotations

import json
import os
import subprocess
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from frameweave.format.writer import RunMeta, write_transcript
from frameweave.stt import local as stt_local
from frameweave.stt.audio import Chunk, extract
from frameweave.stt.base import SttResult
from frameweave.stt.local import WhisperXBackend
from frameweave.stt.speakers import (
    _DIARIZATION_MODEL,
    SpeakersUnavailable,
    _relabel_result,
    _s_label,
    cli_flags,
    make_diarizer,
    preflight_checks,
    relabel,
)
from frameweave.types import Chapter, Description, Frame, Resolved, Segment, Usage
from frameweave.types import Word as FwWord

FIXTURE = Path(__file__).parent / "fixtures" / "two-speakers"
TRUTH_PATH = FIXTURE / "truth.json"
EXAMPLE_FWV = Path(__file__).parent / "fixtures" / "example.fwv"


def _slow_tests_enabled() -> bool:
    return os.environ.get("FRAMEWEAVE_SLOW_TESTS") == "1"


def _config(**overrides: object) -> SimpleNamespace:
    base: dict[str, object] = {
        "stt_model": "large-v3-turbo",
        "stt_device": "cpu",
        "speakers": False,
        "timeout_s": 120.0,
        "out": None,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def _meta(**overrides: object) -> RunMeta:
    base: dict[str, Any] = {
        "range_spec": "full",
        "transcript_source": "captions-auto",
        "speakers": None,
        "vision": "claude:sonnet standard, 8 per call",
        "frame_width": 1280,
        "completion": "complete",
        "completion_reason": None,
        "warnings": [],
        "stats": {
            "segments": 5,
            "windows": 5,
            "frames_primary": 5,
            "frames_extra": 1,
            "dropped_duplicates": 0,
        },
        "generated_at": "2026-09-16T18:04:11Z",
        "tool_version": "0.1.0",
        "caption_track": None,
        "command_line": "frameweave run https://www.youtube.com/watch?v=XXXXXXXXXXX",
    }
    base.update(overrides)
    return RunMeta(**base)


def _example_inputs() -> tuple[
    Resolved, list[Segment], list[Frame], list[Description], RunMeta
]:
    """Same inputs as the committed example.fwv golden (no speaker labels)."""
    resolved = Resolved(
        video_id="XXXXXXXXXXX",
        title="Connecting a worker to a queue",
        channel="Example Channel",
        source="https://www.youtube.com/watch?v=XXXXXXXXXXX",
        duration=1800.0,
        chapters=[
            Chapter(0.0, "Intro"),
            Chapter(270.0, "The queue"),
            Chapter(772.0, "Results"),
        ],
    )
    src = "captions-auto"
    segments = [
        Segment(0.0, 9.0, "today we connect a worker to a queue and watch it drain", src),
        Segment(9.0, 21.0, "first the config file so the worker knows which queue to read", src),
        Segment(270.0, 287.0, "so I connect the worker to the jobs queue and watch it drain", src),
        Segment(772.0, 790.0, "and that is the whole run, forty five jobs in under a minute", src),
        Segment(790.0, 800.0, "thanks for watching", src),
    ]
    frames = [
        Frame("f0001", 0.0, "primary", "frames/f0001-00-00-00.0.jpg"),
        Frame("f0002", 9.0, "primary", "frames/f0002-00-00-09.0.jpg"),
        Frame("f0012", 270.0, "primary", "frames/f0012-00-04-30.0.jpg"),
        Frame("f0013", 315.0, "extra", "frames/f0013-00-05-15.0.jpg"),
        Frame("f0031", 772.0, "primary", "frames/f0031-00-12-52.0.jpg"),
        Frame("f0032", 790.0, "primary", "frames/f0032-00-13-10.0.jpg"),
    ]
    lane = "claude:sonnet"
    descriptions = [
        Description(
            "f0001", lane, "A title card over a dark background.",
            ["Connecting a worker to a queue"],
        ),
        Description(
            "f0002", lane, "An editor with a short config file.", ["QUEUE_NAME=jobs", "BATCH=10"],
        ),
        Description(
            "f0012",
            lane,
            "Terminal on the left, a config file open on the right.",
            ["QUEUE_NAME=jobs", "worker.py", "45 pending"],
        ),
        Description("f0013", lane, "Same terminal, the queue counter now lower.", ["12 pending"]),
        Description(
            "f0031", lane, "A summary slide with three numbers.", ["45 jobs", "58 s", "0 failed"],
        ),
        Description("f0032", lane, "The presenter on camera, no slide."),
    ]
    return resolved, segments, frames, descriptions, _meta()


def test_cli_flags_empty() -> None:
    assert cli_flags() == []


def test_preflight_checks_empty_when_speakers_off() -> None:
    assert preflight_checks(_config(speakers=False)) == []


def test_preflight_checks_reports_token_and_pyannote(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
        pytest.importorskip("pyannote.audio")  # extras group, not installed in CI
monkeypatch.delenv("HF_TOKEN", raising=False)
    checks = preflight_checks(_config(speakers=True))
    assert [c.name for c in checks] == ["HF_TOKEN", "pyannote.audio"]
    assert checks[0].ok is False
    assert "HF_TOKEN" in checks[0].remedy
    assert "huggingface.co/pyannote/segmentation-3.0" in checks[0].remedy
    assert "huggingface.co/pyannote/speaker-diarization-community-1" in checks[0].remedy
    assert "--speakers" in checks[0].remedy
    assert checks[1].remedy == "uv sync --extra speakers"

    monkeypatch.setenv("HF_TOKEN", "hf_test_token")
    checks_ok = preflight_checks(_config(speakers=True))
    assert checks_ok[0].ok is True


def test_make_diarizer_none_when_speakers_off() -> None:
    assert make_diarizer(_config(speakers=False)) is None


def test_make_diarizer_raises_without_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("HF_TOKEN", raising=False)
    with pytest.raises(SpeakersUnavailable, match="HF_TOKEN"):
        make_diarizer(_config(speakers=True))
    with pytest.raises(SpeakersUnavailable, match="speaker-diarization-community-1"):
        make_diarizer(_config(speakers=True))


def test_relabel_maps_pyannote_labels_by_first_appearance() -> None:
    segments = [
        Segment(0.0, 1.0, "one", "stt-whisperx", speaker="SPEAKER_01"),
        Segment(1.0, 2.0, "two", "stt-whisperx", speaker="SPEAKER_00"),
        Segment(2.0, 3.0, "three", "stt-whisperx", speaker="SPEAKER_01"),
        Segment(3.0, 4.0, "four", "stt-whisperx", speaker=None),
    ]
    labeled = relabel(segments)
    assert [s.speaker for s in labeled] == ["S1", "S2", "S1", None]
    assert labeled[0].text == "one"


def test_s_label_helper_shared_by_relabel_paths() -> None:
    """Both Segment and WhisperX-dict relabel paths share first-appearance numbering."""
    mapping_a: dict[str, str] = {}
    mapping_b: dict[str, str] = {}
    assert _s_label("SPEAKER_01", mapping_a) == "S1"
    assert _s_label("SPEAKER_00", mapping_a) == "S2"
    assert _s_label("SPEAKER_01", mapping_a) == "S1"

    result = _relabel_result(
        {
            "segments": [
                {"speaker": "SPEAKER_01", "words": [{"speaker": "SPEAKER_01"}]},
                {"speaker": "SPEAKER_00", "words": [{"speaker": "SPEAKER_00"}]},
            ]
        }
    )
    assert [s["speaker"] for s in result["segments"]] == ["S1", "S2"]
    assert result["segments"][0]["words"][0]["speaker"] == "S1"
    # Same helper drives both: empty mapping + same order yields S1 then S2.
    assert _s_label("SPEAKER_01", mapping_b) == result["segments"][0]["speaker"]
    assert _s_label("SPEAKER_00", mapping_b) == result["segments"][1]["speaker"]


def test_without_speakers_writer_output_matches_example_fwv(tmp_path: Path) -> None:
    """Regression: speaker=None transcript stays byte-identical to the committed golden."""
    resolved, segments, frames, descriptions, meta = _example_inputs()
    assert all(segment.speaker is None for segment in segments)
    path = write_transcript(tmp_path, resolved, segments, frames, descriptions, meta)
    assert path.read_text(encoding="utf-8") == EXAMPLE_FWV.read_text(encoding="utf-8")
    assert "S1:" not in path.read_text(encoding="utf-8")
    assert "speakers:" not in path.read_text(encoding="utf-8").split("\n\n", 1)[0]


def test_merge_and_diarize_calls_diarizer_once_with_full_audio(tmp_path: Path) -> None:
    """Diarization runs once after merge against the full audio path, not per chunk."""
    calls: list[tuple[Path, list[str]]] = []

    def fake_diarize(audio: Path, segments: list[Segment]) -> list[Segment]:
        calls.append((audio, [segment.text for segment in segments]))
        return [replace(segment, speaker="S1") for segment in segments]

    backend = WhisperXBackend(diarize=fake_diarize)
    full_audio = tmp_path / "audio.wav"
    full_audio.write_bytes(b"RIFF")
    first = Chunk(tmp_path / "000.wav", offset_s=0.0, duration_s=10.0)
    second = Chunk(tmp_path / "001.wav", offset_s=8.0, duration_s=10.0)
    first_result = SttResult(
        segments=[Segment(8.8, 9.4, "from first", "stt-whisperx")],
        source="stt-whisperx",
        usage=Usage(calls=1, seconds=0.1),
        audio_seconds=10.0,
    )
    second_result = SttResult(
        segments=[
            Segment(0.5, 1.0, "duplicate", "stt-whisperx"),
            Segment(1.5, 2.2, "from second", "stt-whisperx"),
        ],
        source="stt-whisperx",
        usage=Usage(calls=1, seconds=0.1),
        audio_seconds=10.0,
    )

    labeled = backend.merge_and_diarize(
        full_audio, [(second, second_result), (first, first_result)]
    )

    assert len(calls) == 1
    assert calls[0][0] == full_audio
    assert calls[0][1] == ["from first", "from second"]
    assert [segment.speaker for segment in labeled] == ["S1", "S1"]
    assert [segment.text for segment in labeled] == ["from first", "from second"]


def test_diarize_passes_community_1_model_name(monkeypatch: pytest.MonkeyPatch) -> None:
        pytest.importorskip("whisperx")  # extras group, not installed in CI
monkeypatch.setenv("HF_TOKEN", "hf_test_token")
    captured: dict[str, Any] = {}

    class FakePipeline:
        def __init__(self, **kwargs: Any) -> None:
            captured.update(kwargs)

        def __call__(self, _source: Any) -> MagicMock:
            return MagicMock()

    def fake_assign(_diarize_segments: Any, result: dict[str, Any]) -> dict[str, Any]:
        return result

    with (
        patch("whisperx.diarize.DiarizationPipeline", FakePipeline),
        patch("whisperx.diarize.assign_word_speakers", fake_assign),
    ):
        diarizer = make_diarizer(_config(speakers=True))
        assert diarizer is not None
        segments = [Segment(0.0, 1.0, "hi", "stt-whisperx", words=[FwWord(0.0, 1.0, "hi", 0.9)])]
        diarizer(Path("/tmp/audio.wav"), segments)

    assert captured["model_name"] == _DIARIZATION_MODEL
    assert captured["model_name"] == "pyannote/speaker-diarization-community-1"


def _turn_starts(segments: list[Segment]) -> list[tuple[float, str]]:
    turns: list[tuple[float, str]] = []
    previous: str | None = None
    for segment in segments:
        if not segment.speaker:
            continue
        if segment.speaker != previous:
            turns.append((segment.start, segment.speaker))
            previous = segment.speaker
    return turns


def _pyannote_weights_present() -> tuple[bool, str]:
    hub = stt_local._hub_cache()
    if not hub.is_dir():
        return False, f"Hugging Face cache not found at {hub}"
    name = f"models--{_DIARIZATION_MODEL.replace('/', '--')}"
    if (hub / name).is_dir():
        return True, name
    return False, f"{name} not found under {hub}"


@pytest.mark.slow
@pytest.mark.skipif(
    not (_slow_tests_enabled() and os.environ.get("FRAMEWEAVE_KICKOFF_DIR")),
    reason="set FRAMEWEAVE_SLOW_TESTS=1 and FRAMEWEAVE_KICKOFF_DIR",
)
def test_two_speaker_fixture_turn_boundaries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HF_HUB_OFFLINE", "1")
    monkeypatch.setenv("TRANSFORMERS_OFFLINE", "1")
    if not os.environ.get("HF_TOKEN"):
        pytest.skip("HF_TOKEN required for diarization")
    weights_ok, weights_detail = _pyannote_weights_present()
    if not weights_ok:
        pytest.skip(f"pyannote weights missing: {weights_detail}")

    truth = json.loads(TRUTH_PATH.read_text(encoding="utf-8"))
    root = Path(os.environ["FRAMEWEAVE_KICKOFF_DIR"])
    source = root / truth["source_file"]
    if not source.is_file():
        pytest.skip("wrap-up file absent under FRAMEWEAVE_KICKOFF_DIR")

    clip = tmp_path / "clip.mp4"
    subprocess.run(
        [
            "ffmpeg",
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(source),
            "-ss",
            str(truth["start_s"]),
            "-t",
            str(truth["duration_s"]),
            "-c",
            "copy",
            str(clip),
        ],
        check=True,
    )
    audio = extract(clip, tmp_path / "audio")

    config = _config(speakers=True, timeout_s=600.0)
    backend = WhisperXBackend(diarize=make_diarizer(config))
    chunk_result = backend.transcribe(audio, config)
    chunk = Chunk(audio, offset_s=0.0, duration_s=chunk_result.audio_seconds)
    segments = backend.merge_and_diarize(audio, [(chunk, chunk_result)])

    labels = {segment.speaker for segment in segments if segment.speaker}
    assert labels == {"S1", "S2"}, f"expected two labels, got {labels!r}"

    predicted = _turn_starts(segments)
    assert predicted, "expected at least one labeled turn"
    truth_turns = [
        (float(turn["start_s"]), str(turn["speaker"])) for turn in truth["turns"]
    ]
    for start, speaker in truth_turns:
        assert any(
            abs(pred_start - start) <= 1.0 and pred_speaker == speaker
            for pred_start, pred_speaker in predicted
        ), f"no predicted turn within 1s of truth {(start, speaker)}; predicted={predicted}"
    for pred_start, pred_speaker in predicted:
        assert any(
            abs(pred_start - start) <= 1.0 and pred_speaker == speaker
            for start, speaker in truth_turns
        ), (
            f"predicted {(pred_start, pred_speaker)} has no truth match within 1s; "
            f"truth={truth_turns}"
        )
