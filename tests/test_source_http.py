"""HTTP source: HEAD then GET, HTML body, signed query, 404, sha256 reuse."""

from __future__ import annotations

import hashlib
import json
import logging
import subprocess
from pathlib import Path
from urllib.parse import urlsplit

import pytest

from frameweave.config import load
from frameweave.sources import http as http_mod
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
_MISSING_TOML = Path("/nonexistent/frameweave-test/config.toml")


def _cfg(**flags: object):
    return load(flags or None, env={}, toml_path=_MISSING_TOML, dotenv_paths=[])


def make_tiny_webm(dest: Path) -> Path:
    """1-second VP8 webm. DocType sits past byte 16, so a 16-byte sniff fails."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "testsrc=duration=1:size=64x64:rate=5",
            "-c:v",
            "libvpx",
            "-f",
            "webm",
            str(dest),
        ],
        check=True,
    )
    return dest


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


def test_webm_sniff_buffers_past_16_bytes(tiny_mp4: Path, tmp_path: Path) -> None:
    """Finding 1: DocType past byte 16 must not be reported as HTML/not-media."""
    webm_path = make_tiny_webm(tmp_path / "clip.webm")
    data = webm_path.read_bytes()
    with pytest.raises(NotMediaError):
        sniff(data[:16])
    assert sniff(data) is MediaKind.webm
    src = HttpSource()
    with serve_media(
        tiny_mp4,
        bodies={"/clip.webm": (data, "video/webm")},
    ) as server:
        resolved = src.resolve(f"{server.url}/clip.webm")
        dest = tmp_path / "dest"
        path = src.fetch_media(resolved, dest)
        assert path.name == "media.webm"
        assert sniff(path) is MediaKind.webm
        assert src.resolved_after_fetch(dest).duration == pytest.approx(1.0, abs=0.5)


def test_non_media_aborts_after_sniff_limit(tiny_mp4: Path, tmp_path: Path) -> None:
    """Finding 1: sniff after 64 KiB and abort without keeping a partial file."""
    huge = b"\x00" * (http_mod._SNIFF_LIMIT + 50_000)
    src = HttpSource()
    with serve_media(tiny_mp4, bodies={"/junk.bin": (huge, "application/octet-stream")}) as server:
        resolved = src.resolve(f"{server.url}/junk.bin")
        dest = tmp_path / "dest"
        with pytest.raises(NotMediaError, match=_HTML_MSG):
            src.fetch_media(resolved, dest)
        assert not (dest / "media.partial").exists()
        assert list(dest.glob("media.*")) == []


def test_httpx_logger_is_warning() -> None:
    """Finding 2: httpx must not INFO-log full signed URLs."""
    assert logging.getLogger("httpx").level == logging.WARNING


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


def test_head_405_falls_back_to_ranged_get(tiny_mp4: Path, tmp_path: Path) -> None:
    """Finding 4: refused HEAD triggers Range: bytes=0-65535 GET."""
    src = HttpSource()
    with serve_media(tiny_mp4, head_status=405) as server:
        url = f"{server.url}/video.mp4"
        resolved = src.resolve(url)
        assert server.requests[0].startswith("HEAD")
        ranged = [
            headers
            for line, headers in zip(server.requests, server.request_headers, strict=True)
            if line.startswith("GET")
            and headers.get("Range") == "bytes=0-65535"
        ]
        assert ranged, f"no ranged GET in {server.requests!r} / {server.request_headers!r}"
        dest = tmp_path / "dest"
        path = src.fetch_media(resolved, dest)
        assert sniff(path) is MediaKind.mp4


def test_client_trust_env_false() -> None:
    """Finding 5: httpx must not pick up proxy env vars."""
    src = HttpSource()
    with src._client_ctx() as client:
        assert client.trust_env is False


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
        meta = json.loads((dest / "media.json").read_text())
        assert meta["video_id"] == resolved.video_id


def test_timeout_from_config() -> None:
    """Finding 6: timeout comes from config.timeout_s."""
    assert HttpSource()._timeout_s == 120.0
    cfg = _cfg(timeout_s=33.0)
    assert HttpSource(config=cfg)._timeout_s == 33.0


def test_missing_resolve_is_not_http_zero(tiny_mp4: Path, tmp_path: Path) -> None:
    """Finding 6: fetch without resolve must not raise 'HTTP 0'."""
    src = HttpSource()
    with serve_media(tiny_mp4) as server:
        url = f"{server.url}/video.mp4"
        # Build a Resolved without going through this instance's resolve.
        other = HttpSource()
        resolved = other.resolve(url)
        with pytest.raises(LookupError, match="no full URL"):
            src.fetch_media(resolved, tmp_path / "dest")


def test_probe_keeps_content_type(tiny_mp4: Path) -> None:
    """Finding 6: HEAD probe content-type is retained."""
    src = HttpSource()
    with serve_media(tiny_mp4) as server:
        url = f"{server.url}/video.mp4"
        resolved = src.resolve(url)
        probe = src._probes[resolved.video_id]
        assert probe.content_type == "video/mp4"
        assert probe.content_length == len(server.mp4)


def test_cli_flags_and_preflight() -> None:
    assert cli_flags() == []
    assert preflight_checks(_cfg()) == []


def test_fetch_captions_none(tiny_mp4: Path) -> None:
    src = HttpSource()
    with serve_media(tiny_mp4) as server:
        resolved = src.resolve(f"{server.url}/video.mp4")
        assert src.fetch_captions(resolved) is None
