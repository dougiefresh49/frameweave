"""Local file source: resolve fields, copy-then-move, ffprobe failure."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from frameweave.config import Check, load
from frameweave.sources.local import (
    LocalFileSource,
    MediaUnreadable,
    cli_flags,
    preflight_checks,
    reused_media,
)
from frameweave.types import Resolved, Source
from frameweave.util.media import MediaKind, sniff
from tests.fakes.httpserver import make_tiny_mp4

_MISSING_TOML = Path("/nonexistent/frameweave-test/config.toml")


def _cfg(**flags: object):
    return load(flags or None, env={}, toml_path=_MISSING_TOML, dotenv_paths=[])


@pytest.fixture
def tiny_mp4(tmp_path: Path) -> Path:
    return make_tiny_mp4(tmp_path / "my-cool_video.mp4")


def test_source_protocol() -> None:
    assert isinstance(LocalFileSource(), Source)


def test_matches_existing_file(tiny_mp4: Path, tmp_path: Path) -> None:
    src = LocalFileSource()
    assert src.matches(str(tiny_mp4))
    assert not src.matches(str(tmp_path / "missing.mp4"))
    assert not src.matches(str(tmp_path))


def test_resolve_fields(tiny_mp4: Path) -> None:
    src = LocalFileSource()
    resolved = src.resolve(str(tiny_mp4))
    assert resolved.video_id == hashlib.sha256(tiny_mp4.read_bytes()).hexdigest()
    assert resolved.title == "my cool video"
    assert resolved.channel == "Local recording"
    assert resolved.source == str(tiny_mp4.absolute())
    assert resolved.duration == pytest.approx(1.0, abs=0.25)
    assert resolved.has_captions is False
    expected = datetime.fromtimestamp(tiny_mp4.stat().st_mtime, UTC).date().isoformat()
    assert resolved.published == expected


def test_sha256_id_is_full_hex(tiny_mp4: Path) -> None:
    resolved = LocalFileSource().resolve(str(tiny_mp4))
    assert len(resolved.video_id) == 64
    assert resolved.video_id == hashlib.sha256(tiny_mp4.read_bytes()).hexdigest()


def test_copy_then_move_original(tiny_mp4: Path, tmp_path: Path) -> None:
    src = LocalFileSource()
    resolved = src.resolve(str(tiny_mp4))
    dest_dir = tmp_path / "cache"
    copied = src.fetch_media(resolved, dest_dir)
    assert copied.name == "media.mp4"
    assert sniff(copied) is MediaKind.mp4
    size = tiny_mp4.stat().st_size
    tiny_mp4.rename(tmp_path / "relocated.mp4")
    assert not Path(resolved.source).exists()
    assert copied.is_file()
    assert copied.stat().st_size == size
    assert sniff(copied) is MediaKind.mp4
    meta = (dest_dir / "media.json").read_text()
    assert resolved.video_id in meta


def test_ffprobe_failure_on_text_file(tmp_path: Path) -> None:
    notes = tmp_path / "notes.txt"
    notes.write_text("not a video")
    src = LocalFileSource()
    assert src.matches(str(notes))
    with pytest.raises(MediaUnreadable) as exc:
        src.resolve(str(notes))
    assert str(notes) in str(exc.value)


def test_reuse_skips_copy(tiny_mp4: Path, tmp_path: Path) -> None:
    src = LocalFileSource()
    resolved = src.resolve(str(tiny_mp4))
    dest_dir = tmp_path / "cache"
    first = src.fetch_media(resolved, dest_dir)
    mtime = first.stat().st_mtime_ns
    second = src.fetch_media(resolved, dest_dir)
    assert second == first
    assert first.stat().st_mtime_ns == mtime


def test_reuse_requires_video_id_match(tiny_mp4: Path, tmp_path: Path) -> None:
    """Finding 3: reuse must require media.json sha256 == resolved.video_id."""
    src = LocalFileSource()
    resolved = src.resolve(str(tiny_mp4))
    dest_dir = tmp_path / "cache"
    media = src.fetch_media(resolved, dest_dir)
    meta_path = dest_dir / "media.json"
    meta = json.loads(meta_path.read_text())
    assert meta["sha256"] == resolved.video_id
    # Same on-disk bytes, but media.json claims a different id.
    meta["sha256"] = "0" * 64
    meta_path.write_text(json.dumps(meta, indent=2) + "\n")
    assert reused_media(dest_dir, resolved.video_id) is None
    other = Resolved(
        video_id="0" * 64,
        title=resolved.title,
        channel=resolved.channel,
        source=resolved.source,
        duration=resolved.duration,
    )
    assert reused_media(dest_dir, other.video_id) is None
    # Restore matching meta so a correct id still reuses.
    meta["sha256"] = hashlib.sha256(media.read_bytes()).hexdigest()
    meta_path.write_text(json.dumps(meta, indent=2) + "\n")
    assert reused_media(dest_dir, meta["sha256"]) == media


def test_timeout_from_config() -> None:
    """Finding 6: timeout comes from config.timeout_s (default 120)."""
    assert LocalFileSource()._timeout_s == 120.0
    cfg = _cfg(timeout_s=45.0)
    assert LocalFileSource(config=cfg)._timeout_s == 45.0
    assert LocalFileSource(config=cfg, timeout_s=10.0)._timeout_s == 10.0


def test_check_imported_from_config() -> None:
    """Finding 6: Check comes from frameweave.config, not a local fallback."""
    checks = preflight_checks(_cfg())
    assert len(checks) == 1
    assert type(checks[0]) is Check
    assert checks[0].__class__.__module__ == "frameweave.config"


def test_fetch_captions_and_flags(tiny_mp4: Path) -> None:
    src = LocalFileSource()
    resolved = Resolved(
        video_id="x",
        title="t",
        channel="c",
        source=str(tiny_mp4),
        duration=1.0,
    )
    assert src.fetch_captions(resolved) is None
    assert cli_flags() == []
    checks = preflight_checks(_cfg())
    assert len(checks) == 1
    assert checks[0].name == "ffprobe"
    assert checks[0].remedy == "brew install ffmpeg"
    assert checks[0].ok is True
