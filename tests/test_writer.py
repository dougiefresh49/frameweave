"""Transcript writer: fixtures byte for byte, sidecars, completion, relocation."""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path

from frameweave.format.writer import (
    INCOMPLETE_NO_SPEECH,
    RunMeta,
    cli_flags,
    derive_completion,
    preflight_checks,
    write_cost,
    write_meta,
    write_transcript,
)
from frameweave.types import FORMAT_VERSION, Chapter, Description, Frame, Resolved, Segment

FIXTURES = Path(__file__).parent / "fixtures"


def _stats(**overrides: object) -> dict:
    data = {
        "segments": 0,
        "windows": 0,
        "frames_primary": 0,
        "frames_extra": 0,
        "dropped_duplicates": 0,
    }
    data.update(overrides)
    return data


def _meta(**overrides: object) -> RunMeta:
    base = dict(
        range_spec="full",
        transcript_source="captions-auto",
        speakers=None,
        vision="claude:sonnet standard, 8 per call",
        frame_width=1280,
        completion="complete",
        completion_reason=None,
        warnings=[],
        stats=_stats(),
        generated_at="2026-09-16T18:04:11Z",
        tool_version="0.1.0",
        caption_track=None,
        command_line="frameweave run https://www.youtube.com/watch?v=XXXXXXXXXXX",
    )
    base.update(overrides)
    return RunMeta(**base)  # type: ignore[arg-type]


def _example() -> tuple[Resolved, list[Segment], list[Frame], list[Description], RunMeta]:
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
    meta = _meta(
        stats=_stats(segments=5, windows=5, frames_primary=5, frames_extra=1),
    )
    return resolved, segments, frames, descriptions, meta


def _extended() -> tuple[
    Resolved,
    list[Segment],
    list[Frame],
    list[Description],
    RunMeta,
    list[str],
    dict[str, str],
]:
    resolved = Resolved(
        video_id="kickoff",
        title="Kickoff: features walkthrough",
        channel="Local recording",
        source="/Users/example/Movies/kickoff.mp4",
        duration=1980.0,
    )
    segments = [
        Segment(
            240.0, 252.0,
            "okay so the first feature is the buffer between the two queues",
            "stt-whisperx", speaker="S1",
        ),
        Segment(
            252.0, 260.0, "and the buffer is bounded or unbounded", "stt-whisperx", speaker="S2",
        ),
        Segment(
            260.0, 330.0,
            "bounded, we cap it at ten thousand and shed load above that, let me show the config",
            "stt-whisperx", speaker="S1",
        ),
        Segment(
            330.0, 341.0,
            "got it, and what happens to the shed messages",
            "stt-whisperx", speaker="S2",
        ),
        Segment(370.0, 375.0, "they go to a dead letter topic", "stt-whisperx", speaker="S1"),
    ]
    frames = [
        Frame("f0001", 240.0, "primary", "frames/f0001-00-04-00.0.jpg"),
        Frame("f0002", 252.0, "primary", "frames/f0002-00-04-12.0.jpg"),
        Frame("f0003", 260.0, "primary", "frames/f0003-00-04-20.0.jpg"),
        Frame("f0004", 305.0, "extra", "frames/f0004-00-05-05.0.jpg"),
        Frame("f0005", 320.0, "extra", "frames/f0005-00-05-20.0.jpg"),
        Frame("f0006", 330.0, "primary", "frames/f0006-00-05-30.0.jpg"),
        Frame("f0007", 370.0, "primary", "frames/f0007-00-06-10.0.jpg"),
        Frame("f0008", 420.0, "primary", "frames/f0008-00-07-00.0.jpg"),
    ]
    tag = "codex:gpt-5.6-sol"
    descriptions = [
        Description(
            "f0001", tag, "A diagram with two boxes and an arrow.",
            ["ingest", "buffer", "workers"],
        ),
        Description(
            "f0002", tag, "Same diagram, the presenter's cursor on the middle box.", ["buffer"],
        ),
        Description(
            "f0003", tag, "An editor with a yaml file.", ["max_buffer: 10000", "shed: true"],
        ),
        Description("f0004", tag, "The same yaml file scrolled down.", ["retry_after_s: 30"]),
        Description("f0005", tag, "A terminal running the service.", illegible=True),
        Description("f0006", tag, "The presenter on camera."),
        Description("f0007", tag, "A dashboard with a topic list.", ["dead-letter", "3 msgs"]),
        Description(
            "f0008", tag, "A silent stretch: a loading spinner over the dashboard.", ["Loading"],
        ),
    ]
    meta = _meta(
        range_spec="00:04:00-00:09:00",
        transcript_source="stt-whisperx",
        speakers=2,
        vision="codex:gpt-5.6-sol standard, 8 per call",
        completion="complete-with-warnings",
        warnings=["near-duplicate of f0004 dropped"],
        stats=_stats(segments=5, windows=5, frames_primary=6, frames_extra=2, dropped_duplicates=1),
        generated_at="2026-09-16T18:20:00Z",
        tool_version="0.2.0",
        command_line="frameweave run /Users/example/Movies/kickoff.mp4 --speakers",
    )
    extra_events = [
        "[00:05:20] note/dedupe: frame f0005 kept; near-duplicate of f0004 dropped"
        " (a kind readers must ignore)"
    ]
    extra_headers = {"x-experimental": "a header key readers must ignore"}
    return resolved, segments, frames, descriptions, meta, extra_events, extra_headers


def test_example_fixture_byte_for_byte(tmp_path: Path) -> None:
    resolved, segments, frames, descriptions, meta = _example()
    path = write_transcript(tmp_path, resolved, segments, frames, descriptions, meta)
    got = path.read_text(encoding="utf-8")
    assert got == (FIXTURES / "example.fwv").read_text(encoding="utf-8")


def test_extended_fixture_byte_for_byte(tmp_path: Path) -> None:
    resolved, segments, frames, descriptions, meta, extra_events, extra_headers = _extended()
    path = write_transcript(
        tmp_path, resolved, segments, frames, descriptions, meta,
        extra_events=extra_events, extra_headers=extra_headers,
    )
    assert path.read_text(encoding="utf-8") == (FIXTURES / "example-extended.fwv").read_text(
        encoding="utf-8"
    )


def test_derive_completion_three_ways() -> None:
    spoken = [Segment(0.0, 1.0, "hi", "captions")]
    assert derive_completion([], [], True) == ("incomplete", INCOMPLETE_NO_SPEECH)
    assert derive_completion(spoken, ["cap hit"], True) == ("complete-with-warnings", None)
    assert derive_completion(spoken, [], True) == ("complete", None)
    assert derive_completion([], [], False) == ("complete", None)


def test_meta_json_has_every_listed_field(tmp_path: Path) -> None:
    resolved, segments, frames, descriptions, meta = _example()
    write_transcript(tmp_path, resolved, segments, frames, descriptions, meta)
    path = write_meta(tmp_path, resolved, frames, meta)
    data = json.loads(path.read_text(encoding="utf-8"))
    for key in resolved.to_dict():
        assert key in data
    for key in (
        "range",
        "transcript_source",
        "caption_track",
        "speakers",
        "vision",
        "frames",
        "completion",
        "completion_reason",
        "warnings",
        "stats",
        "generated_at",
        "tool_version",
        "command_line",
        "format_version",
    ):
        assert key in data
    assert data["format_version"] == FORMAT_VERSION
    assert data["frames"].keys() >= {"total", "primary", "extra", "width"}
    assert data["frames"]["total"] == 6
    assert data["frames"]["primary"] == 5
    assert data["frames"]["extra"] == 1
    assert data["frames"]["width"] == 1280
    for key in ("segments", "windows", "frames_primary", "frames_extra", "dropped_duplicates"):
        assert key in data["stats"]
    signed = Resolved(
        video_id="v", title="t", channel="c",
        source="https://cdn.example/v.mp4?token=SECRET", duration=1.0,
    )
    write_meta(tmp_path, signed, [], _meta())
    assert json.loads((tmp_path / "meta.json").read_text(encoding="utf-8"))["source"].endswith(
        "?token=SECRET"
    )


def test_relocated_folder_has_no_broken_frame_refs(tmp_path: Path) -> None:
    dest = tmp_path / "run"
    dest.mkdir()
    resolved, segments, frames, descriptions, meta = _example()
    (dest / "frames").mkdir()
    for frame in frames:
        (dest / frame.path).write_bytes(b"jpeg")
    write_transcript(dest, resolved, segments, frames, descriptions, meta)
    write_meta(dest, resolved, frames, meta)
    moved = tmp_path / "elsewhere" / "run"
    shutil.move(dest, moved)
    text = (moved / "transcript.fwv").read_text(encoding="utf-8")
    refs = re.findall(r"frames/[^\s:]+\.jpg", text)
    assert refs
    for ref in refs:
        assert (moved / ref).is_file()
    meta_data = json.loads((moved / "meta.json").read_text(encoding="utf-8"))
    for ref in meta_data["frames"]["files"]:
        assert not Path(ref).is_absolute()
        assert (moved / ref).is_file()


def test_frame_without_a_description(tmp_path: Path) -> None:
    resolved = Resolved(video_id="v", title="t", channel="c", source="/v.mp4", duration=10.0)
    frames = [Frame("f0001", 0.0, "primary", "frames/f0001-00-00-00.0.jpg")]
    meta = _meta()
    text = write_transcript(tmp_path, resolved, [], frames, [], meta).read_text(encoding="utf-8")
    assert "seen/claude:sonnet #f0001 frames/f0001-00-00-00.0.jpg: (no description)" in text
    assert "\n  text:\n" in text or text.endswith("  text:\n")
    assert "frame f0001 has no description" in meta.warnings
    assert meta.completion == "complete-with-warnings"


def test_chapter_with_no_events(tmp_path: Path) -> None:
    resolved = Resolved(
        video_id="v", title="t", channel="c", source="/v.mp4", duration=180.0,
        chapters=[Chapter(0.0, "Intro"), Chapter(60.0, "Empty"), Chapter(120.0, "End")],
    )
    segments = [
        Segment(0.0, 5.0, "hello", "captions"),
        Segment(120.0, 125.0, "bye", "captions"),
    ]
    text = write_transcript(tmp_path, resolved, segments, [], [], _meta()).read_text(
        encoding="utf-8"
    )
    assert "## [00:01:00] Empty" in text
    intro = text.index("## [00:00:00] Intro")
    empty = text.index("## [00:01:00] Empty")
    end = text.index("## [00:02:00] End")
    assert intro < empty < end
    between = text[empty:end]
    assert "said/" not in between
    assert "seen/" not in between


def test_ordering_at_equal_times(tmp_path: Path) -> None:
    resolved = Resolved(video_id="v", title="t", channel="c", source="/v.mp4", duration=30.0)
    segments = [Segment(10.0, 20.0, "talk", "captions-auto")]
    frames = [
        Frame("f0002", 10.0, "extra", "frames/f0002-00-00-10.0.jpg"),
        Frame("f0001", 10.0, "primary", "frames/f0001-00-00-10.0.jpg"),
    ]
    descriptions = [
        Description("f0001", "claude:sonnet", "Primary."),
        Description("f0002", "claude:sonnet", "Extra."),
    ]
    extra_events = ["[00:00:10] note/dedupe: dropped a copy"]
    text = write_transcript(
        tmp_path, resolved, segments, frames, descriptions, _meta(), extra_events=extra_events,
    ).read_text(encoding="utf-8")
    body = text.split("\n\n", 1)[1]
    kinds = []
    for line in body.splitlines():
        match = re.match(r"^\[\S+\] ([a-z+]+)/", line)
        if match:
            kinds.append(match.group(1))
    assert kinds == ["said", "seen", "seen+", "note"]


def test_write_cost_serializes_the_ledger_dict(tmp_path: Path) -> None:
    summary = {
        "stages": [
            {
                "provider": "claude",
                "model": "sonnet",
                "tokens_in": 100,
                "tokens_out": 20,
                "tokens_reasoning": 0,
                "seconds": 1.5,
                "usd": 0.0,
            }
        ],
        "current_run_usd": 0.0,
        "reused_usd": 0.0,
        "unknown_usd": 0.0,
        "total_usd": 0.0,
        "rates_dated": "2026-09-15",
    }
    path = write_cost(tmp_path, summary)
    assert json.loads(path.read_text(encoding="utf-8")) == summary


def test_cli_seams_are_empty() -> None:
    assert cli_flags() == []
    assert preflight_checks(object()) == []
