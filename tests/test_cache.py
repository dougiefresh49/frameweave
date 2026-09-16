"""Cache size and prune: per-video bytes, age cutoff, obligation guard."""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from frameweave.cache import (
    CacheReport,
    parse_older_than,
    prune,
    size,
)


def _touch(path: Path, payload: bytes = b"x", *, mtime: float | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    if mtime is not None:
        os.utime(path, (mtime, mtime))


def test_parse_older_than() -> None:
    assert parse_older_than("30d") == timedelta(days=30)
    assert parse_older_than("12h") == timedelta(hours=12)
    assert parse_older_than("1D") == timedelta(days=1)
    with pytest.raises(ValueError, match="invalid --older-than"):
        parse_older_than("30")
    with pytest.raises(ValueError, match="invalid --older-than"):
        parse_older_than("2w")


def test_size_per_video_sorted(tmp_path: Path) -> None:
    cache = tmp_path / "cache"
    # vid-big: sources 100 + runs 50
    _touch(cache / "sources" / "vid-big" / "media.mp4", b"a" * 100)
    _touch(cache / "runs" / "vid-big" / "rk" / "frames.json", b"b" * 50)
    # vid-small: sources 10
    _touch(cache / "sources" / "vid-small" / "media.mp4", b"c" * 10)
    # orphan runs only
    _touch(cache / "runs" / "vid-orphan" / "rk" / "ledger.jsonl", b"d" * 20)
    # Loose file at cache root counts toward total but not a video row.
    _touch(cache / "marker.txt", b"hi\n")

    report = size(cache)
    assert isinstance(report, CacheReport)
    assert [row.video_id for row in report.videos] == [
        "vid-big",
        "vid-orphan",
        "vid-small",
    ]
    assert report.videos[0].sources_bytes == 100
    assert report.videos[0].runs_bytes == 50
    assert report.videos[0].total_bytes == 150
    assert report.videos[1].sources_bytes == 0
    assert report.videos[1].runs_bytes == 20
    assert report.videos[2].total_bytes == 10
    assert report.total_bytes == 150 + 20 + 10 + len(b"hi\n")
    assert report.oldest_mtime is not None
    assert report.newest_mtime is not None
    assert report.oldest_mtime <= report.newest_mtime


def test_prune_dry_run_and_yes(tmp_path: Path) -> None:
    cache = tmp_path / "cache"
    now = datetime(2026, 9, 16, 12, 0, 0, tzinfo=UTC)
    old = (now - timedelta(days=40)).timestamp()
    fresh = (now - timedelta(days=1)).timestamp()

    _touch(cache / "sources" / "old-vid" / "media.mp4", b"old-source", mtime=old)
    _touch(cache / "runs" / "old-vid" / "rk" / "frames.json", b"old-run", mtime=old)
    _touch(cache / "sources" / "fresh-vid" / "media.mp4", b"fresh", mtime=fresh)
    _touch(cache / "runs" / "fresh-vid" / "rk" / "frames.json", b"fresh-run", mtime=fresh)
    # Orphan runs (source already gone).
    _touch(cache / "runs" / "orphan" / "rk" / "ledger.jsonl", b"orphan", mtime=fresh)

    plan = prune(cache, timedelta(days=30), dry_run=True, now=now)
    kinds = {(t.kind, t.video_id) for t in plan.targets}
    assert ("sources", "old-vid") in kinds
    assert ("runs", "old-vid") in kinds
    assert ("runs", "orphan") in kinds  # source gone
    assert ("sources", "fresh-vid") not in kinds
    assert ("runs", "fresh-vid") not in kinds
    assert plan.dry_run is True
    assert (cache / "sources" / "old-vid").is_dir()

    done = prune(cache, timedelta(days=30), dry_run=False, now=now)
    assert done.dry_run is False
    assert done.bytes_freed == plan.bytes_freed
    assert not (cache / "sources" / "old-vid").exists()
    assert not (cache / "runs" / "old-vid").exists()
    assert not (cache / "runs" / "orphan").exists()
    assert (cache / "sources" / "fresh-vid").is_dir()
    assert (cache / "runs" / "fresh-vid").is_dir()


def test_prune_skips_pending_obligations(tmp_path: Path) -> None:
    cache = tmp_path / "cache"
    now = datetime(2026, 9, 16, 12, 0, 0, tzinfo=UTC)
    old = (now - timedelta(days=60)).timestamp()

    _touch(cache / "sources" / "owed" / "media.mp4", b"media", mtime=old)
    run_dir = cache / "runs" / "owed" / "rk"
    _touch(run_dir / "frames.json", b"frames", mtime=old)
    (run_dir / "obligations.json").write_text(
        json.dumps([{"kind": "fake", "id": "upload-1"}]) + "\n",
        encoding="utf-8",
    )
    # Force obligations.json mtime old too.
    os.utime(run_dir / "obligations.json", (old, old))

    report = prune(cache, timedelta(days=30), dry_run=False, now=now)
    assert report.targets == ()
    assert (cache / "sources" / "owed").is_dir()
    assert (cache / "runs" / "owed").is_dir()


def test_prune_never_touches_outside_cache(tmp_path: Path) -> None:
    cache = tmp_path / "cache"
    outside = tmp_path / "outside" / "secret.txt"
    _touch(outside, b"keep-me")
    now = datetime(2026, 9, 16, tzinfo=UTC)
    old = (now - timedelta(days=90)).timestamp()
    _touch(cache / "sources" / "v" / "media.mp4", b"x", mtime=old)

    prune(cache, timedelta(days=1), dry_run=False, now=now)
    assert outside.read_bytes() == b"keep-me"
    assert not (cache / "sources" / "v").exists()


def test_prune_empty_obligations_list_is_not_pending(tmp_path: Path) -> None:
    cache = tmp_path / "cache"
    now = datetime(2026, 9, 16, tzinfo=UTC)
    old = (now - timedelta(days=40)).timestamp()
    _touch(cache / "sources" / "cleared" / "media.mp4", b"m", mtime=old)
    run_dir = cache / "runs" / "cleared" / "rk"
    _touch(run_dir / "frames.json", b"f", mtime=old)
    (run_dir / "obligations.json").write_text("[]\n", encoding="utf-8")
    os.utime(run_dir / "obligations.json", (old, old))

    report = prune(cache, timedelta(days=30), dry_run=False, now=now)
    assert {t.video_id for t in report.targets} == {"cleared"}
    assert not (cache / "sources" / "cleared").exists()


def test_size_empty_cache(tmp_path: Path) -> None:
    cache = tmp_path / "empty"
    cache.mkdir()
    report = size(cache)
    assert report.videos == ()
    assert report.total_bytes == 0
    assert report.oldest_mtime is None
    assert report.newest_mtime is None


def test_mtime_drives_age_not_ctime(tmp_path: Path) -> None:
    """Newest file mtime is the age signal (create then backdate)."""
    cache = tmp_path / "cache"
    now = datetime(2026, 9, 16, tzinfo=UTC)
    path = cache / "sources" / "aged" / "media.mp4"
    _touch(path, b"payload")
    # File exists "now" in wall clock but we set mtime far in the past.
    old = (now - timedelta(days=100)).timestamp()
    os.utime(path, (old, old))
    assert path.stat().st_mtime == pytest.approx(old, abs=1)

    report = prune(cache, timedelta(days=30), dry_run=True, now=now)
    assert any(t.video_id == "aged" and t.kind == "sources" for t in report.targets)
