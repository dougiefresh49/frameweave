"""HTTP source: HEAD then GET, HTML body, signed query, 404, sha256 reuse."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from urllib.parse import urlsplit

import pytest

from frameweave.sources.http import (
    HttpSource,
    SourceUnavailable,
    cli_flags,
    preflight_checks,
    redact_url,
)
from frameweave.types import Source
from frameweave.util.media import MediaKind, NotMediaError, sniff
from tests.fakes.httpserver import make_tiny_mp4, serve_media

_HTML_MSG = (
    "That URL returned a web page, not a video. Download it in your browser and pass the file."
)


@pytest.fixture
def tiny_mp4(tmp_path: Path) -> Path:
    return make_tiny_mp4(tmp_path / "clip.mp4")


def test_source_protocol() -> None:
    assert isinstance(HttpSource(), Source)


def test_matches_http_not_youtube() -> None:
    src = HttpSource()
    assert src.matches("https://cdn.example.com/v.mp4")
    assert src.matches("http://127.0.0.1:8000/v.mp4")
    assert not src.matches("https://www.youtube.com/watch?v=dQw4w9WgXcQ")
    assert not src.matches("https://youtu.be/dQw4w9WgXcQ")
    assert not src.matches("https://m.youtube.com/watch?v=dQw4w9WgXcQ")
    assert not src.matches("https://music.youtube.com/watch?v=dQw4w9WgXcQ")
    assert not src.matches("/tmp/video.mp4")


def test_head_then_get(tiny_mp4: Path, tmp_path: Path) -> None:
    src = HttpSource()
    with serve_media(tiny_mp4) as server:
        url = f"{server.url}/video.mp4"
        resolved = src.resolve(url)
        assert resolved.duration == 0.0
        assert resolved.title == "video.mp4"
        assert resolved.channel == "127.0.0.1"
        assert resolved.has_captions is False
        parts = urlsplit(url)
        stripped = f"{parts.scheme}://{parts.netloc}{parts.path}"
        assert resolved.video_id == hashlib.sha256(stripped.encode()).hexdigest()
        dest = tmp_path / "dest"
        path = src.fetch_media(resolved, dest)
        assert sniff(path) is MediaKind.mp4
        methods = [line.split()[0] for line in server.requests]
        assert methods[0] == "HEAD"
        assert "GET" in methods
        after = src.resolved_after_fetch(dest)
        assert after.duration == pytest.approx(1.0, abs=0.25)


def test_html_body_message(tiny_mp4: Path, tmp_path: Path) -> None:
    src = HttpSource()
    with serve_media(tiny_mp4) as server:
        resolved = src.resolve(f"{server.url}/page.html")
        dest = tmp_path / "dest"
        with pytest.raises(NotMediaError, match=_HTML_MSG) as exc:
            src.fetch_media(resolved, dest)
        assert str(exc.value) == _HTML_MSG


def test_signed_query_kept_on_wire_redacted_in_media_json(tiny_mp4: Path, tmp_path: Path) -> None:
    src = HttpSource()
    secret = "s3cret-value"
    with serve_media(tiny_mp4) as server:
        url = f"{server.url}/video.mp4?token={secret}&sig=abc123"
        resolved = src.resolve(url)
        assert secret not in resolved.source
        assert "abc123" not in resolved.source
        assert "***" in resolved.source
        dest = tmp_path / "dest"
        src.fetch_media(resolved, dest)
        assert any(secret in line and "abc123" in line for line in server.requests)
        meta = json.loads((dest / "media.json").read_text())
        blob = json.dumps(meta)
        assert secret not in blob
        assert "abc123" not in blob
        assert meta["url"] == redact_url(url)


def test_404_raises_source_unavailable(tiny_mp4: Path) -> None:
    src = HttpSource()
    with serve_media(tiny_mp4) as server:
        url = f"{server.url}/missing.mp4"
        with pytest.raises(SourceUnavailable) as exc:
            src.resolve(url)
        assert "404" in str(exc.value)
        assert url in str(exc.value) or "/missing.mp4" in str(exc.value)


def test_sha256_reuse_skips_fetch(tiny_mp4: Path, tmp_path: Path) -> None:
    src = HttpSource()
    with serve_media(tiny_mp4) as server:
        url = f"{server.url}/video.mp4"
        resolved = src.resolve(url)
        dest = tmp_path / "dest"
        first = src.fetch_media(resolved, dest)
        n = len(server.requests)
        second = src.fetch_media(resolved, dest)
        assert second == first
        assert len(server.requests) == n


def test_cli_flags_and_preflight() -> None:
    assert cli_flags() == []
    assert preflight_checks(None) == []


def test_fetch_captions_none(tiny_mp4: Path) -> None:
    src = HttpSource()
    with serve_media(tiny_mp4) as server:
        resolved = src.resolve(f"{server.url}/video.mp4")
        assert src.fetch_captions(resolved) is None
