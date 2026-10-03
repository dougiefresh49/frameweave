"""X/Twitter status posts via yt-dlp, sharing YouTube's download path."""

from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import urlparse

from frameweave.sources.youtube import (
    YouTubeSource,
    YtDlpError,
    _chapters,
    _has_english_captions,
    _links_from_description,
    _upload_date_iso,
)
from frameweave.types import Resolved
from frameweave.util.retry import RequestFailed

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
# /<user>/status/<id>, /i/web/status/<id>, or /statuses/<id>, plus optional
# /video/<n> or /photo/<n> and a trailing slash. Query and fragment are ignored.
_STATUS_PATH_RE = re.compile(
    r"^/(?:"
    r"(?P<user>[^/]+)/status/(?P<status_id>[0-9]+)"
    r"|i/web/status/(?P<iweb_id>[0-9]+)"
    r"|statuses/(?P<statuses_id>[0-9]+)"
    r")"
    r"(?:/(?P<kind>video|photo)/(?P<index>[0-9]+))?/?$"
)


def _status_ref(raw: str) -> tuple[str, str, int] | None:
    """Return ``(user, status_id, video_index)`` for a status URL, else ``None``.

    ``/i/web/status`` and ``/statuses`` have no screen name; those canonicalize
    with user ``i``. A ``/photo/<n>`` URL and a missing index both mean video 1.
    """
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
    status_id = (
        match.group("status_id") or match.group("iweb_id") or match.group("statuses_id")
    )
    if status_id is None:
        return None
    user = match.group("user") or "i"
    if match.group("kind") == "video":
        index = int(match.group("index"))
    else:
        index = 1
    return user, status_id, index


def _canonical_source(user: str, status_id: str, index: int) -> str:
    return f"https://x.com/{user}/status/{status_id}/video/{index}"


def _video_id(status_id: str, index: int) -> str:
    if index == 1:
        return f"x-{status_id}"
    return f"x-{status_id}-v{index}"


class XSource(YouTubeSource):
    """Resolve and fetch an X/Twitter post. No YouTube player-client chain."""

    name = "x"

    def matches(self, raw_input: str) -> bool:
        return _status_ref(raw_input) is not None

    def resolve(self, raw_input: str) -> Resolved:
        ref = _status_ref(raw_input)
        if ref is None:
            raise ValueError(f"not an X/Twitter input: {raw_input!r}")
        user, status_id, index = ref
        canonical = _canonical_source(user, status_id, index)
        try:
            info = self._resolve_info(canonical)
        except RequestFailed as exc:
            # retry.call replaces a permanent YtDlpError with "request failed".
            cause = exc.__context__
            if isinstance(cause, YtDlpError):
                raise YtDlpError(cause.message, returncode=cause.returncode) from exc
            raise
        description = str(info.get("description") or "")
        channel = str(info.get("uploader") or info.get("uploader_id") or "")
        return Resolved(
            video_id=_video_id(status_id, index),
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
