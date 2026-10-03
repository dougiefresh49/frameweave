"""X/Twitter status posts via yt-dlp, sharing YouTube's download path."""

from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import urlparse

from frameweave.sources.youtube import (
    YouTubeSource,
    _chapters,
    _has_english_captions,
    _links_from_description,
    _upload_date_iso,
)
from frameweave.types import Resolved

_HOSTS = frozenset(
    {
        "x.com",
        "www.x.com",
        "mobile.x.com",
        "twitter.com",
        "www.twitter.com",
        "mobile.twitter.com",
    }
)
# /<user>/status/<digits> with an optional /video/<n> or /photo/<n> and trailing slash.
_STATUS_PATH_RE = re.compile(
    r"^/(?P<user>[^/]+)/status/(?P<status_id>[0-9]+)"
    r"(?:/(?:video|photo)/[0-9]+)?/?$"
)


def _status_parts(raw: str) -> tuple[str, str] | None:
    """Return ``(user, status_id)`` for a status URL, else ``None``."""
    try:
        parsed = urlparse(raw.strip())
    except ValueError:
        return None
    if parsed.scheme.lower() not in {"http", "https"}:
        return None
    host = (parsed.hostname or "").lower()
    if host not in _HOSTS:
        return None
    match = _STATUS_PATH_RE.match(parsed.path)
    if match is None:
        return None
    return match.group("user"), match.group("status_id")


def _canonical_source(user: str, status_id: str) -> str:
    return f"https://x.com/{user}/status/{status_id}"


class XSource(YouTubeSource):
    """Resolve and fetch an X/Twitter post. No YouTube player-client chain."""

    name = "x"

    def matches(self, raw_input: str) -> bool:
        return _status_parts(raw_input) is not None

    def resolve(self, raw_input: str) -> Resolved:
        parts = _status_parts(raw_input)
        if parts is None:
            raise ValueError(f"not an X/Twitter input: {raw_input!r}")
        user, status_id = parts
        canonical = _canonical_source(user, status_id)
        info = self._resolve_info(canonical)
        description = str(info.get("description") or "")
        channel = str(info.get("uploader") or info.get("uploader_id") or "")
        return Resolved(
            video_id=f"x-{status_id}",
            title=str(info.get("title") or ""),
            channel=channel,
            source=canonical,
            duration=float(info.get("duration") or 0.0),
            description=description,
            links=_links_from_description(description),
            chapters=_chapters(info),
            has_captions=_has_english_captions(info),
            published=_upload_date_iso(info.get("upload_date")),
        )

    def fetch_media(self, resolved: Resolved, dest_dir: Path) -> Path:
        dest_dir.mkdir(parents=True, exist_ok=True)
        cached = self._reuse_cached(dest_dir)
        if cached is not None:
            return cached
        result = self._attempt_download(resolved, dest_dir, None, degraded=False)
        return self._publish(result, dest_dir)
