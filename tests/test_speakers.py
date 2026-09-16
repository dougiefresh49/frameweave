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
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from frameweave.format.writer import RunMeta, write_transcript
from frameweave.stt.audio import extract
from frameweave.stt.local import WhisperXBackend
from frameweave.stt.speakers import (
    SpeakersUnavailable,
    cli_flags,
    make_diarizer,
    preflight_checks,
    relabel,
)
from frameweave.types import Resolved, Segment

FIXTURE = Path(__file__).parent / "fixtures" / "two-speakers"
TRUTH_PATH = FIXTURE / "truth.json"


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
        "transcript_source": "stt-whisperx",
        "speakers": None,
        "vision": "none",
        "frame_width": 768,
        "completion": "complete",
        "completion_reason": None,
        "warnings": [],
        "stats": {"segments": 1, "windows": 1, "frames_primary": 0, "frames_extra": 0},
        "generated_at": "2026-09-16T00:00:00Z",
        "tool_version": "0.0.0",
        "caption_track": None,
        "command_line": "frameweave run example.mp4",
    }
    base.update(overrides)
    return RunMeta(**base)


def _resolved() -> Resolved:
    return Resolved(
        video_id="example",
        title="Example",
        channel="Local",
        source="/tmp/example.mp4",
        duration=60.0,
    )


def test_cli_flags_empty() -> None:
    assert cli_flags() == []


def test_preflight_checks_empty_when_speakers_off() -> None:
    assert preflight_checks(_config(speakers=False)) == []


def test_preflight_checks_reports_token_and_pyannote(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("HF_TOKEN", raising=False)
    checks = preflight_checks(_config(speakers=True))
    assert [c.name for c in checks] == ["HF_TOKEN", "pyannote.audio"]
    assert checks[0].ok is False
    assert "HF_TOKEN" in checks[0].remedy
    assert "huggingface.co/pyannote/segmentation-3.0" in checks[0].remedy
    assert "huggingface.co/pyannote/speaker-diarization-3.1" in checks[0].remedy
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


def test_without_speakers_writer_output_has_no_label_prefix(tmp_path: Path) -> None:
    """Regression: speaker=None segments stay byte-identical to pre-speakers shape."""
    segments = [
        Segment(0.0, 1.5, "hello from the room", "stt-whisperx", speaker=None),
        Segment(2.0, 3.0, "and another line", "stt-whisperx"),
    ]
    path = write_transcript(tmp_path, _resolved(), segments, [], [], _meta(speakers=None))
    text = path.read_text(encoding="utf-8")
    again = write_transcript(
        tmp_path / "b", _resolved(), segments, [], [], _meta(speakers=None)
    ).read_text(encoding="utf-8")
    assert text == again
    assert "S1:" not in text
    assert "said/stt-whisperx: hello from the room" in text
    assert "speakers:" not in text.split("\n\n", 1)[0]


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
    hub = Path.home() / ".cache" / "huggingface" / "hub"
    if not hub.is_dir():
        return False, f"Hugging Face cache not found at {hub}"
    names = [
        "models--pyannote--speaker-diarization-community-1",
        "models--pyannote--speaker-diarization-3.1",
    ]
    for name in names:
        if (hub / name).is_dir():
            return True, name
    return False, f"none of {names} under {hub}"


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
    result = backend.transcribe(audio, config)

    labels = {segment.speaker for segment in result.segments if segment.speaker}
    assert labels == {"S1", "S2"}, f"expected two labels, got {labels!r}"

    predicted = _turn_starts(result.segments)
    assert predicted, "expected at least one labeled turn"
    for turn in truth["turns"]:
        start = float(turn["start_s"])
        assert any(abs(pred_start - start) <= 1.0 for pred_start, _ in predicted), (
            f"no predicted turn start within 1s of truth {start}; predicted={predicted}"
        )
