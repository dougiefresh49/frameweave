"""Range and chapter selection: parse, precedence, pipeline filtering."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from frameweave.config import load, output_dir, run_key, slugify
from frameweave.frames import plan as plan_frames
from frameweave.pipeline import run
from frameweave.range import (
    RangeError,
    RangeSpec,
    cli_flags,
    overlaps,
    parse,
    parse_url_t,
    preflight_checks,
    url_t_from,
)
from frameweave.types import Chapter, Resolved, Segment
from frameweave.util import timecode
from tests.fakes.pipeline import FakeSource, FakeStt, FakeVision
from tests.make_synthetic import make as make_synthetic

MISSING_TOML = Path("/nonexistent/frameweave-test/config.toml")


def _resolved(
    *,
    duration: float = 600.0,
    chapters: list[Chapter] | None = None,
) -> Resolved:
    return Resolved(
        video_id="vid",
        title="Demo",
        channel="Ch",
        source="https://www.youtube.com/watch?v=vid",
        duration=duration,
        chapters=chapters or [],
    )


def _cfg(tmp_path: Path, **flags: object):
    out = tmp_path / "out"
    cache = tmp_path / "cache"
    out.mkdir(exist_ok=True)
    cache.mkdir(exist_ok=True)
    base = {
        "out": out,
        "cache_dir": cache,
        "vision_lane": "claude",
        "frames_per_call": 2,
        "frame_interval_s": 5.0,
        "captions_mode": "none",
    }
    base.update(flags)
    return load(flags=base, env={}, toml_path=MISSING_TOML, dotenv_paths=[])


def test_cli_flags_and_preflight() -> None:
    names = {flag.name for flag in cli_flags()}
    assert names == {"--start", "--end", "--chapter"}
    assert preflight_checks(object()) == []


def test_parse_full() -> None:
    rs = parse(None, None, None, None, _resolved())
    assert rs == RangeSpec(0.0, 600.0, "full", "", "full")
    assert rs.header == "full"


def test_parse_start_end_formats() -> None:
    resolved = _resolved(duration=3600.0)
    cases = [
        ("90", "180", 90.0, 180.0),
        ("1:30", "3:00", 90.0, 180.0),
        ("00:01:30", "00:03:00", 90.0, 180.0),
        ("00:01:30.5", "00:03:00", 90.5, 180.0),
    ]
    for start, end, start_s, end_s in cases:
        rs = parse(start, end, None, None, resolved)
        assert rs.source == "flags"
        assert rs.start == start_s
        assert rs.end == end_s
        label = (
            f"{timecode.format(start_s, tenths=False)}-"
            f"{timecode.format(end_s, tenths=False)}"
        )
        assert rs.label == label
        assert rs.slug == (
            f"{timecode.format(start_s, tenths=False).replace(':', '-')}_"
            f"{timecode.format(end_s, tenths=False).replace(':', '-')}"
        )


def test_parse_end_defaults_to_duration() -> None:
    rs = parse("1:00", None, None, None, _resolved(duration=500.0))
    assert rs.start == 60.0
    assert rs.end == 500.0


def test_parse_start_defaults_to_zero() -> None:
    rs = parse(None, "2:00", None, None, _resolved())
    assert rs.start == 0.0
    assert rs.end == 120.0


def test_parse_rejects_start_ge_end() -> None:
    with pytest.raises(RangeError, match="start >= end"):
        parse("2:00", "1:00", None, None, _resolved())


def test_parse_rejects_end_past_duration() -> None:
    with pytest.raises(RangeError, match="end > duration"):
        parse("0", "999", None, None, _resolved(duration=100.0))


def test_parse_chapter_exact_and_prefix() -> None:
    chapters = [
        Chapter(0.0, "Intro"),
        Chapter(270.0, "The queue"),
        Chapter(772.0, "Results"),
    ]
    resolved = _resolved(duration=1800.0, chapters=chapters)
    rs = parse(None, None, "the que", None, resolved)
    assert rs.source == "chapter"
    assert rs.start == 270.0
    assert rs.end == 772.0
    assert rs.label == "The queue"
    assert rs.slug == slugify("The queue")
    assert rs.header == "chapter: The queue"


def test_parse_chapter_whitespace_collapse() -> None:
    chapters = [Chapter(0.0, "The   Queue"), Chapter(100.0, "End")]
    rs = parse(None, None, "  the  queue ", None, _resolved(chapters=chapters, duration=200.0))
    assert rs.label == "The   Queue"
    assert rs.end == 100.0


def test_parse_chapter_last_uses_duration() -> None:
    chapters = [Chapter(0.0, "Only")]
    rs = parse(None, None, "only", None, _resolved(chapters=chapters, duration=50.0))
    assert rs.start == 0.0
    assert rs.end == 50.0


def test_parse_chapter_zero_matches_lists_all() -> None:
    chapters = [Chapter(0.0, "Intro"), Chapter(60.0, "Body")]
    with pytest.raises(RangeError, match="no chapter matching") as exc:
        parse(None, None, "missing", None, _resolved(chapters=chapters))
    msg = str(exc.value)
    assert "00:00:00 Intro" in msg
    assert "00:01:00 Body" in msg


def test_parse_chapter_ambiguous_lists_candidates() -> None:
    chapters = [
        Chapter(0.0, "Part one"),
        Chapter(60.0, "Part two"),
        Chapter(120.0, "Other"),
    ]
    with pytest.raises(RangeError, match="ambiguous") as exc:
        parse(None, None, "part", None, _resolved(chapters=chapters))
    msg = str(exc.value)
    assert "Part one" in msg
    assert "Part two" in msg
    assert "Other" not in msg


def test_flags_beat_chapter_and_url_t() -> None:
    chapters = [Chapter(0.0, "Intro"), Chapter(100.0, "Next")]
    resolved = _resolved(duration=500.0, chapters=chapters)
    rs = parse("10", "20", "Intro", 90.0, resolved)
    assert rs.source == "flags"
    assert rs.start == 10.0
    assert rs.end == 20.0


def test_chapter_beats_url_t() -> None:
    chapters = [Chapter(0.0, "Intro"), Chapter(100.0, "Next")]
    resolved = _resolved(duration=500.0, chapters=chapters)
    rs = parse(None, None, "Next", 90.0, resolved)
    assert rs.source == "chapter"
    assert rs.start == 100.0


def test_url_t_becomes_start_to_duration() -> None:
    rs = parse(None, None, None, 90.0, _resolved(duration=500.0))
    assert rs.source == "url"
    assert rs.start == 90.0
    assert rs.end == 500.0
    assert rs.label == "00:01:30-00:08:20"
    assert rs.slug == "00-01-30_00-08-20"


def test_parse_url_t_forms() -> None:
    assert parse_url_t("90") == 90.0
    assert parse_url_t("90s") == 90.0
    assert parse_url_t("1m30s") == 90.0
    assert parse_url_t("1h2m3s") == 3723.0
    assert parse_url_t("1:30") == 90.0
    assert url_t_from("https://www.youtube.com/watch?v=abc&t=1m30s") == 90.0
    assert url_t_from("https://www.youtube.com/watch?v=abc&t=90") == 90.0
    assert url_t_from("https://www.youtube.com/watch?v=abc") is None


def test_overlaps_half_open() -> None:
    assert overlaps(0.0, 10.0, 5.0, 15.0)
    assert overlaps(5.0, 15.0, 0.0, 10.0)
    assert not overlaps(0.0, 5.0, 5.0, 10.0)
    assert not overlaps(10.0, 20.0, 0.0, 10.0)


def test_frame_interval_changes_run_key_and_plan(tmp_path: Path) -> None:
    cfg5 = _cfg(tmp_path, frame_interval_s=5.0)
    cfg15 = _cfg(tmp_path, frame_interval_s=15.0)
    assert run_key(cfg5, "00:00:00-00:01:00") != run_key(cfg15, "00:00:00-00:01:00")
    segments = [Segment(0.0, 60.0, "long", "none")]
    plan5 = plan_frames(segments, 60.0, cfg5)
    plan15 = plan_frames(segments, 60.0, cfg15)
    assert plan5.interval_s != plan15.interval_s or len(plan5.frames) != len(plan15.frames)


def test_range_run_filters_segments_frames_and_output_slug(tmp_path: Path) -> None:
    video = tmp_path / "clip.mp4"
    make_synthetic(video, 20)
    cfg = _cfg(tmp_path, frame_interval_s=15.0, vision_lane="none")
    source = FakeSource(video=video)
    stt = FakeStt(
        segments=[
            Segment(0.0, 4.0, "before window", "stt-fake", id="s0001"),
            Segment(3.0, 6.0, "straddle start", "stt-fake", id="s0002"),
            Segment(5.0, 9.0, "inside window", "stt-fake", id="s0003"),
            Segment(9.0, 13.0, "straddle end", "stt-fake", id="s0004"),
            Segment(12.0, 16.0, "after window", "stt-fake", id="s0005"),
        ]
    )
    vision = FakeVision()
    outcome = run(
        str(video),
        cfg,
        source=source,
        stt=stt,
        vision=vision,
        start="5",
        end="10",
    )
    expected_slug = "00-00-05_00-00-10"
    assert outcome.output_path.name == expected_slug
    assert outcome.output_path.parent.name == slugify("Synthetic twenty")

    run_dir = Path(cfg.cache_dir) / "runs"
    run_dirs = [p for p in run_dir.rglob("transcript.json")]
    assert len(run_dirs) == 1
    data = json.loads(run_dirs[0].read_text(encoding="utf-8"))
    kept = data["segments"]
    assert [seg["text"] for seg in kept] == [
        "straddle start",
        "inside window",
        "straddle end",
    ]
    # Boundary-straddling segments keep absolute times.
    assert (kept[0]["start"], kept[0]["end"]) == (3.0, 6.0)
    assert (kept[2]["start"], kept[2]["end"]) == (9.0, 13.0)

    frames = json.loads(run_dirs[0].with_name("frames.json").read_text(encoding="utf-8"))
    assert frames["interval_s"] == 15.0
    for frame in frames["frames"]:
        assert 5.0 <= frame["time"] < 10.0

    header = (outcome.output_path / "transcript.fwv").read_text(encoding="utf-8")
    assert "range: 00:00:05-00:00:10" in header

    # Same range at 5 s interval gets a different run key.
    (tmp_path / "out5").mkdir(exist_ok=True)
    (tmp_path / "cache5").mkdir(exist_ok=True)
    cfg5 = load(
        flags={
            "out": tmp_path / "out5",
            "cache_dir": tmp_path / "cache5",
            "vision_lane": "none",
            "frames_per_call": 2,
            "frame_interval_s": 5.0,
            "captions_mode": "none",
        },
        env={},
        toml_path=MISSING_TOML,
        dotenv_paths=[],
    )
    outcome5 = run(
        str(video),
        cfg5,
        source=FakeSource(video=video),
        stt=FakeStt(segments=list(stt.segments)),
        vision=FakeVision(),
        start="5",
        end="10",
    )
    assert outcome5.run_key != outcome.run_key


def test_range_run_through_duration_zero_source(tmp_path: Path) -> None:
    """Http-like sources resolve with duration 0; range must wait for post-fetch."""
    from dataclasses import replace

    video = tmp_path / "clip.mp4"
    make_synthetic(video, 20)
    cfg = _cfg(tmp_path, frame_interval_s=15.0, vision_lane="none")

    class HttpishSource(FakeSource):
        def resolve(self, raw_input: str) -> Resolved:
            resolved = super().resolve(raw_input)
            return replace(resolved, duration=0.0)

        def resolved_after_fetch(self, dest_dir: Path) -> Resolved:
            del dest_dir
            assert self.video is not None
            return Resolved(
                video_id=self._video_id,
                title="Synthetic twenty",
                channel="Test Channel",
                source=str(self.video.resolve()),
                duration=20.0,
                has_captions=False,
            )

    source = HttpishSource(video=video)
    stt = FakeStt(
        segments=[
            Segment(0.0, 4.0, "before", "stt-fake", id="s0001"),
            Segment(5.0, 9.0, "inside", "stt-fake", id="s0002"),
            Segment(12.0, 16.0, "after", "stt-fake", id="s0003"),
        ]
    )
    outcome = run(
        str(video),
        cfg,
        source=source,
        stt=stt,
        vision=FakeVision(),
        start="5",
        end="10",
    )
    assert outcome.output_path.name == "00-00-05_00-00-10"
    assert outcome.run_key == run_key(cfg, "00:00:05-00:00:10")
    assert outcome.run_key != "_pending_range"

    transcript = next((Path(cfg.cache_dir) / "runs").rglob("transcript.json"))
    assert transcript.parent.name == outcome.run_key
    texts = [s["text"] for s in json.loads(transcript.read_text(encoding="utf-8"))["segments"]]
    assert texts == ["inside"]


def test_output_dir_nests_range_slug(tmp_path: Path) -> None:
    root = tmp_path / "out"
    path = output_dir(root, "ch", "vid", range_slug="intro")
    assert path == root / "ch" / "vid" / "intro"
    assert output_dir(root, "ch", "vid", range_slug=None) == root / "ch" / "vid"
