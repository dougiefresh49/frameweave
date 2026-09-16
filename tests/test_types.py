"""Shared types round-trip through JSON, and the format's rules hold on the fixtures."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from frameweave.types import (
    FORMAT_VERSION,
    Chapter,
    Description,
    Frame,
    LedgerEntry,
    Resolved,
    Segment,
    Source,
    StageResult,
    Usage,
    Word,
)

FIXTURES = Path(__file__).parent / "fixtures"
EVENT = re.compile(
    r"^\[(?P<time>[\d:.]+)(?:-(?P<end>[\d:.]+))?\] "
    r"(?P<kind>[a-z+]+)/(?P<source>[^ ]+) (?P<payload>.*)$"
)


def _roundtrip(obj):
    data = json.loads(json.dumps(obj.to_dict()))
    return type(obj).from_dict(data)


def test_segment_with_words_round_trips() -> None:
    seg = Segment(
        start=4.0, end=12.4, text="okay so the first feature", source="stt-whisperx",
        speaker="S1", words=[Word(4.0, 4.3, "okay", 0.98), Word(4.3, 4.5, "so", 0.7)],
        quality=-0.21,
    )
    assert _roundtrip(seg) == seg
    assert seg.to_dict()["words"][0] == {"start": 4.0, "end": 4.3, "text": "okay", "score": 0.98}


def test_segment_contains_follows_the_association_rule() -> None:
    seg = Segment(start=260.0, end=330.0, text="x", source="captions")
    assert seg.contains(260.0)
    assert seg.contains(305.0)
    assert not seg.contains(330.0)  # end is exclusive; the next segment starts here
    assert not seg.contains(420.0)


def test_frame_keeps_tenths_and_validates_id() -> None:
    frame = Frame(id="f0012", time=270.04, kind="primary", path="frames/f0012-00-04-30.0.jpg")
    assert frame.time == 270.0
    assert _roundtrip(frame) == frame
    with pytest.raises(ValueError):
        Frame(id="12", time=1.0, kind="primary", path="x")


def test_description_usage_ledger_stage_round_trip() -> None:
    desc = Description(
        frame_id="f0012", source="claude:sonnet", summary="Terminal", strings=["45 pending"]
    )
    usage = Usage(calls=1, tokens_in=5310, tokens_out=196, seconds=4.1, usd=0.0)
    entry = LedgerEntry(
        stage="describe", provider="claude", model="sonnet",
        started="2026-09-16T18:00:00Z", ended="2026-09-16T18:00:04Z", usage=usage,
    )
    result = StageResult(
        stage="describe", status="done", artifact="descriptions.json", usage=usage,
        warnings=["one frame illegible"],
    )
    for obj in (desc, usage, entry, result):
        assert _roundtrip(obj) == obj
    assert _roundtrip(entry).usage == usage  # nested record comes back typed
    assert (usage + Usage(calls=2, tokens_in=10, unknown_usd=True)).calls == 3
    assert (usage + Usage(unknown_usd=True)).unknown_usd is True


def test_resolved_round_trips_with_chapters_and_ignores_unknown_keys() -> None:
    res = Resolved(
        video_id="abc", title="T", channel="C", source="https://x", duration=1800.0,
        chapters=[Chapter(0.0, "Intro"), Chapter(270.0, "The queue")],
    )
    data = res.to_dict()
    data["future_field"] = 1  # additive rule
    assert Resolved.from_dict(data) == res


def test_source_protocol_is_structural() -> None:
    class Fake:
        name = "fake"

        def matches(self, raw_input: str) -> bool:
            return True

        def resolve(self, raw_input: str) -> Resolved:
            return Resolved(video_id="v", title="t", channel="c", source=raw_input, duration=1.0)

        def fetch_media(self, resolved: Resolved, dest_dir: Path) -> Path:
            return dest_dir / "media.mp4"

        def fetch_captions(self, resolved: Resolved) -> list[Segment] | None:
            return None

    assert isinstance(Fake(), Source)


# --- the format on the fixtures: a reader written only from docs/format.md ---


def read(path: Path):
    text = path.read_text().splitlines()
    assert text[0] == f"frameweave {FORMAT_VERSION}"
    header: dict[str, str] = {}
    i = 1
    while i < len(text) and text[i]:
        key, _, value = text[i].partition(": ")
        header[key] = value
        i += 1
    events = []
    for line in text[i:]:
        if line.startswith("  ") and events:
            events[-1]["continuation"].append(line[2:])
        elif line.startswith("## "):
            events.append({"kind": "chapter", "line": line, "continuation": []})
        elif m := EVENT.match(line):
            d = m.groupdict()
            d["source"] = d["source"].rstrip(":")  # said lines put a colon after the source tag
            events.append({**d, "continuation": []})
    return header, events


@pytest.mark.parametrize("name", ["example.fwv", "example-extended.fwv"])
def test_primary_frame_count_matches_header(name: str) -> None:
    header, events = read(FIXTURES / name)
    m = re.match(r"(\d+) primary (\d+) extra (\d+) at (\d+)px", header["frames"])
    assert m
    primary = [e for e in events if e.get("kind") == "seen"]
    extra = [e for e in events if e.get("kind") == "seen+"]
    assert len(primary) == int(m.group(2))
    assert len(extra) == int(m.group(3))
    assert len(primary) + len(extra) == int(m.group(1))


def test_extended_fixture_adds_a_kind_and_a_header_readers_ignore() -> None:
    header, events = read(FIXTURES / "example-extended.fwv")
    assert "x-experimental" in header  # present, and nothing above depended on it
    kinds = {e.get("kind") for e in events}
    assert "note" in kinds and "seen+" in kinds
    said = [e for e in events if e.get("kind") == "said"]
    assert all(e["payload"].startswith(("S1: ", "S2: ")) for e in said)
    assert header["speakers"] == "2"


def test_text_lines_are_continuations_and_illegible_is_a_value() -> None:
    _, events = read(FIXTURES / "example-extended.fwv")
    by_id = {e["payload"].split()[0]: e for e in events if e.get("kind") in ("seen", "seen+")}
    assert by_id["#f0005"]["continuation"] == ["text: illegible"]
    assert by_id["#f0006"]["continuation"] == ["text:"]
    assert by_id["#f0003"]["continuation"] == ['text: "max_buffer: 10000", "shed: true"']


def test_chapter_map_is_one_grep() -> None:
    lines = (FIXTURES / "example.fwv").read_text().splitlines()
    assert [ln for ln in lines if ln.startswith("## ")] == [
        "## [00:00:00] Intro",
        "## [00:04:30] The queue",
        "## [00:12:52] Results",
    ]


def test_association_rule_on_the_extended_fixture() -> None:
    _, events = read(FIXTURES / "example-extended.fwv")

    def secs(t: str) -> float:
        h, m, s = t.split(":")
        return int(h) * 3600 + int(m) * 60 + float(s)

    said = [e for e in events if e.get("kind") == "said"]
    segments = [Segment(secs(e["time"]), secs(e["end"]), e["payload"], e["source"]) for e in said]
    seen = [e for e in events if e.get("kind") in ("seen", "seen+")]
    frames = {e["payload"].split()[0]: secs(e["time"]) for e in seen}
    owner = {fid: next((s for s in segments if s.contains(t)), None) for fid, t in frames.items()}
    assert owner["#f0004"] is not None and owner["#f0004"].start == secs("00:04:20")
    assert owner["#f0008"] is None  # the silent stretch stands alone
