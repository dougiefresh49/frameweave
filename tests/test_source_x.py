"""XSource: status-URL matching, recorded resolve, and yt-dlp argv without a client."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from frameweave.captions import CaptionsResult
from frameweave.cli import _pick_source as cli_pick_source
from frameweave.pipeline import _pick_source as pipeline_pick_source
from frameweave.pipeline import _stage_fetch_captions
from frameweave.sources.http import HttpSource
from frameweave.sources.x import XSource
from frameweave.sources.youtube import FORMAT_LADDER, YouTubeSource, YtDlpError
from frameweave.types import Resolved, Usage
from tests.fakes.ytdlp import FakeYtDlp

RECORDED = Path(__file__).parent / "recorded" / "x" / "resolve-2102050467505430555.json"
STATUS_ID = "2102050467505430555"
CANONICAL = f"https://x.com/poteto/status/{STATUS_ID}"


@pytest.mark.parametrize(
    "raw",
    [
        f"https://x.com/poteto/status/{STATUS_ID}",
        f"https://x.com/poteto/status/{STATUS_ID}/",
        f"https://x.com/poteto/status/{STATUS_ID}/video/1",
        f"https://x.com/poteto/status/{STATUS_ID}/video/1/",
        f"https://x.com/poteto/status/{STATUS_ID}/photo/2",
        f"http://x.com/poteto/status/{STATUS_ID}",
        f"https://www.x.com/poteto/status/{STATUS_ID}",
        f"https://mobile.x.com/poteto/status/{STATUS_ID}",
        f"https://twitter.com/poteto/status/{STATUS_ID}",
        f"https://www.twitter.com/poteto/status/{STATUS_ID}",
        f"https://mobile.twitter.com/poteto/status/{STATUS_ID}/video/1",
        f"https://x.com/poteto/status/{STATUS_ID}/video/1?s=20&t=3",
        f"https://x.com/poteto/status/{STATUS_ID}?s=20",
    ],
)
def test_matches_status_urls(raw: str) -> None:
    assert XSource().matches(raw) is True


@pytest.mark.parametrize(
    "raw",
    [
        "x.com/poteto",
        "https://x.com/poteto",
        "https://x.com/poteto/status/not-digits",
        "https://x.com/poteto/status/123/likes",
        "https://example.com/poteto/status/123",
        "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
        "https://youtu.be/dQw4w9WgXcQ",
        "",
    ],
)
def test_matches_rejects_non_status(raw: str) -> None:
    assert XSource().matches(raw) is False


def test_resolve_from_recorded_fixture() -> None:
    info = json.loads(RECORDED.read_text())
    fake = FakeYtDlp(mode="resolve_only", resolve_json=info)
    src = XSource(runner=fake, attempts=0)
    raw = f"https://x.com/poteto/status/{STATUS_ID}/video/1?s=20"
    resolved = src.resolve(raw)
    assert resolved.video_id == f"x-{STATUS_ID}"
    assert resolved.video_id != info["id"]
    assert resolved.source == CANONICAL
    assert resolved.title == info["title"]
    assert resolved.channel == "lauren"
    assert resolved.duration == 2281.984
    assert resolved.has_captions is True
    assert resolved.published == "2026-09-21"
    assert resolved.description == info["description"]
    assert fake.calls
    assert "-J" in fake.calls[0]
    assert fake.calls[0][-1] == CANONICAL


def _failing_runner(calls: list[list[str]]):
    def runner(args: list[str]) -> subprocess.CompletedProcess[str]:
        calls.append(list(args))
        return subprocess.CompletedProcess(args=[], returncode=1, stdout="", stderr="boom")

    return runner


def _resolved(source: str) -> Resolved:
    return Resolved(
        video_id="x-1",
        title="t",
        channel="c",
        source=source,
        duration=1.0,
    )


def test_download_once_omits_extractor_args_when_client_is_none(tmp_path: Path) -> None:
    calls: list[list[str]] = []
    src = YouTubeSource(runner=_failing_runner(calls), attempts=0)
    dest = tmp_path / "dest"
    dest.mkdir()
    with pytest.raises(YtDlpError):
        src._download_once(_resolved(CANONICAL), dest, None, degraded=False)
    assert calls == [
        [
            "-f",
            FORMAT_LADDER,
            "-o",
            str(dest / "media.%(ext)s"),
            "--print-json",
            "--no-playlist",
            "--",
            CANONICAL,
        ]
    ]
    assert "--extractor-args" not in calls[0]


def test_download_once_youtube_still_sends_extractor_args(tmp_path: Path) -> None:
    calls: list[list[str]] = []
    src = YouTubeSource(runner=_failing_runner(calls), attempts=0)
    dest = tmp_path / "dest"
    dest.mkdir()
    watch = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
    with pytest.raises(YtDlpError):
        src._download_once(_resolved(watch), dest, "android", degraded=False)
    assert calls == [
        [
            "-f",
            FORMAT_LADDER,
            "--extractor-args",
            "youtube:player_client=android",
            "-o",
            str(dest / "media.%(ext)s"),
            "--print-json",
            "--no-playlist",
            "--",
            watch,
        ]
    ]


def test_pick_source_prefers_x_over_http() -> None:
    url = f"https://x.com/poteto/status/{STATUS_ID}/video/1?s=20"
    assert HttpSource().matches(url) is True
    for picked in (pipeline_pick_source(url), cli_pick_source(url, None)):
        assert isinstance(picked, XSource)
        assert picked.name == "x"
        assert not isinstance(picked, HttpSource)


def test_captions_stage_fetches_for_x(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, object] = {}

    def fake_fetch(video_url: str, dest_dir: Path, config: object) -> CaptionsResult:
        seen["url"] = video_url
        seen["dest"] = dest_dir
        seen["config"] = config
        return CaptionsResult(
            segments=[],
            source="none",
            track=None,
            reason=None,
            usage=Usage(),
        )

    monkeypatch.setattr("frameweave.pipeline.fetch_captions_track", fake_fetch)
    config = object()
    ctx = SimpleNamespace(
        resolved=_resolved(CANONICAL),
        source=SimpleNamespace(name="x"),
        source_dir=tmp_path,
        config=config,
    )
    result = _stage_fetch_captions(ctx)
    assert seen == {"url": CANONICAL, "dest": tmp_path, "config": config}
    assert result.status == "done"
    assert result.stage == "fetch_captions"
