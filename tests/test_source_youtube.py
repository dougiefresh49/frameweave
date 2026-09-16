"""YouTubeSource: matches, resolve, client chain, cache, claim, ffprobe rejection."""

from __future__ import annotations

import json
import logging
import multiprocessing as mp
from pathlib import Path

import pytest

from frameweave.sources.youtube import (
    MediaUnreadable,
    SourceBusy,
    YouTubeSource,
    claim,
)
from tests.fakes.ytdlp import FakeYtDlp

RECORDED = Path(__file__).parent / "recorded" / "youtube" / "resolve-dQw4w9WgXcQ.json"
VIDEO_ID = "dQw4w9WgXcQ"


def _tiny_mp4(tmp_path: Path) -> Path:
    out = tmp_path / "tiny.mp4"
    if out.exists():
        return out
    import subprocess

    subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc=duration=1:size=64x64:rate=5",
            "-pix_fmt",
            "yuv420p",
            str(out),
        ],
        check=True,
    )
    return out


@pytest.mark.parametrize(
    "raw",
    [
        f"https://www.youtube.com/watch?v={VIDEO_ID}",
        f"https://youtu.be/{VIDEO_ID}",
        f"https://m.youtube.com/watch?v={VIDEO_ID}",
        f"https://music.youtube.com/watch?v={VIDEO_ID}",
        f"https://www.youtube.com/embed/{VIDEO_ID}",
        f"https://www.youtube.com/shorts/{VIDEO_ID}",
        VIDEO_ID,
    ],
)
def test_matches_url_shapes_and_bare_id(raw: str) -> None:
    assert YouTubeSource().matches(raw) is True


def test_matches_rejects_non_youtube() -> None:
    assert YouTubeSource().matches("https://example.com/watch?v=abc") is False
    assert YouTubeSource().matches("not-a-video-id") is False


def test_resolve_from_recorded_json_links_chapters_and_t() -> None:
    info = json.loads(RECORDED.read_text())
    fake = FakeYtDlp(mode="resolve_only", resolve_json=info)
    src = YouTubeSource(runner=fake, attempts=0)
    raw = f"https://www.youtube.com/watch?v={VIDEO_ID}&t=43"
    resolved = src.resolve(raw)
    assert resolved.video_id == VIDEO_ID
    assert resolved.title == info["title"]
    assert resolved.channel == info["uploader"]
    assert resolved.source == f"https://www.youtube.com/watch?v={VIDEO_ID}&t=43"
    assert resolved.duration == float(info["duration"])
    assert resolved.description == info["description"]
    assert resolved.published == "2009-10-25"
    assert resolved.has_captions is True
    assert resolved.chapters[0].title == "Intro"
    assert resolved.chapters[0].start == 0.0
    assert resolved.chapters[1].title == "The song"
    assert "https://linktr.ee/rickastleynever" in resolved.links
    assert len(resolved.links) == len(set(resolved.links))
    assert fake.calls and "-J" in fake.calls[0]


def test_fetch_chain_403_then_web_success(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    media = _tiny_mp4(tmp_path)
    fake = FakeYtDlp(mode="chain_403", media_path=media, resolve_json=RECORDED)
    src = YouTubeSource(runner=fake, attempts=0)
    resolved = src.resolve(VIDEO_ID)
    dest = tmp_path / "dest"
    with caplog.at_level(logging.INFO, logger="frameweave.sources.youtube"):
        path = src.fetch_media(resolved, dest)
    assert path.exists()
    assert path.name.startswith("media.")
    meta = json.loads((dest / "media.json").read_text())
    assert meta["client"] == "web"
    assert meta["sha256"]
    assert meta["bytes"] == path.stat().st_size
    clients = [
        next(
            a.split("youtube:player_client=", 1)[1]
            for a in call
            if isinstance(a, str) and a.startswith("youtube:player_client=")
        )
        for call in fake.calls
        if "-J" not in call
    ]
    assert clients == ["android", "mweb", "web"]
    assert any("player_client=web" in r.message for r in caplog.records)


def test_part_cleanup(tmp_path: Path) -> None:
    media = _tiny_mp4(tmp_path)
    fake = FakeYtDlp(mode="leave_part", media_path=media)
    src = YouTubeSource(runner=fake, attempts=0)
    from frameweave.types import Resolved

    resolved = Resolved(
        video_id=VIDEO_ID,
        title="t",
        channel="c",
        source=f"https://www.youtube.com/watch?v={VIDEO_ID}",
        duration=1.0,
    )
    dest = tmp_path / "dest"
    dest.mkdir()
    (dest / "orphan.part").write_bytes(b"old")
    (dest / "orphan.ytdl").write_text("old")
    path = src.fetch_media(resolved, dest)
    assert path.exists()
    assert list(dest.glob("*.part")) == []
    assert list(dest.glob("*.ytdl")) == []


def test_sha256_reuse_zero_runner_calls(tmp_path: Path) -> None:
    media = _tiny_mp4(tmp_path)
    fake = FakeYtDlp(mode="chain_403", media_path=media)
    src = YouTubeSource(runner=fake, attempts=0)
    from frameweave.types import Resolved

    resolved = Resolved(
        video_id=VIDEO_ID,
        title="t",
        channel="c",
        source=f"https://www.youtube.com/watch?v={VIDEO_ID}",
        duration=1.0,
    )
    dest = tmp_path / "dest"
    first = src.fetch_media(resolved, dest)
    calls_after_first = len(fake.calls)
    second = src.fetch_media(resolved, dest)
    assert second == first
    assert len(fake.calls) == calls_after_first


def _claim_worker(
    source_dir: str,
    ready: mp.synchronize.Event,
    hold: mp.synchronize.Event,
    result: mp.Queue,
) -> None:
    try:
        with claim(Path(source_dir)):
            ready.set()
            hold.wait(timeout=10)
            result.put(("ok", None))
    except SourceBusy as exc:
        result.put(("busy", exc.pid))
    except Exception as exc:  # noqa: BLE001
        result.put(("err", repr(exc)))


def test_claim_second_process_raises_source_busy(tmp_path: Path) -> None:
    """Second process raises SourceBusy (does not wait)."""
    ctx = mp.get_context("spawn")
    source_dir = tmp_path / "source"
    ready = ctx.Event()
    hold = ctx.Event()
    queue: mp.Queue = ctx.Queue()

    holder = ctx.Process(target=_claim_worker, args=(str(source_dir), ready, hold, queue))
    holder.start()
    assert ready.wait(timeout=10)

    with pytest.raises(SourceBusy) as exc:
        with claim(source_dir):
            pass
    assert exc.value.pid == holder.pid

    hold.set()
    holder.join(timeout=10)
    assert holder.exitcode == 0
    assert queue.get(timeout=2)[0] == "ok"


def test_claim_stale_pid_is_taken(tmp_path: Path) -> None:
    source_dir = tmp_path / "source"
    claim_dir = source_dir / ".claim"
    claim_dir.mkdir(parents=True)
    claim_dir.joinpath("pid").write_text("999999999\n")
    with claim(source_dir):
        assert (source_dir / ".claim" / "pid").exists()
    assert not (source_dir / ".claim").exists()


def test_ffprobe_rejects_html_body(tmp_path: Path) -> None:
    fake = FakeYtDlp(mode="html_body")
    src = YouTubeSource(runner=fake, attempts=0)
    from frameweave.types import Resolved

    resolved = Resolved(
        video_id=VIDEO_ID,
        title="t",
        channel="c",
        source=f"https://www.youtube.com/watch?v={VIDEO_ID}",
        duration=1.0,
    )
    dest = tmp_path / "dest"
    with pytest.raises(MediaUnreadable) as exc:
        src.fetch_media(resolved, dest)
    assert "media" in str(exc.value.path)
    assert not any(dest.glob("media.*")) or not (dest / "media.mp4").exists()


def test_fetch_captions_returns_none() -> None:
    from frameweave.types import Resolved

    resolved = Resolved(
        video_id=VIDEO_ID,
        title="t",
        channel="c",
        source=f"https://www.youtube.com/watch?v={VIDEO_ID}",
        duration=1.0,
    )
    assert YouTubeSource().fetch_captions(resolved) is None
