"""Direct HTTP(S) media source. Query credentials stay on the wire and never land in artifacts.

``resolve`` does not ffprobe; duration is 0.0 until ``fetch_media``. Call
``resolved_after_fetch(dest_dir)`` for the ``Resolved`` with the real duration.
The same ``HttpSource`` instance must run ``resolve`` then ``fetch_media``: the
full URL is kept only in memory, keyed by ``video_id``.
"""

from __future__ import annotations

import hashlib
import logging
from contextlib import AbstractContextManager, nullcontext
from dataclasses import dataclass, replace
from pathlib import Path
from urllib.parse import parse_qsl, unquote, urlencode, urlsplit, urlunsplit

import httpx

from frameweave.config import Check, Config
from frameweave.sources.local import (
    MediaUnreadable,
    _fetched_at,
    _remove_other_media,
    _resolve_timeout,
    ffprobe_duration,
    reused_media,
    sha256_file,
    write_json,
)
from frameweave.types import Resolved, Segment
from frameweave.util.media import NotMediaError, sniff
from frameweave.util.retry import Outcome, RequestFailed, RequestTimeout, RetryExhausted, call

logging.getLogger("httpx").setLevel(logging.WARNING)

_NOT_MEDIA = (
    "That URL returned a web page, not a video. Download it in your browser and pass the file."
)
_SNIFF_LIMIT = 65_536
_RANGE = {"Range": f"bytes=0-{_SNIFF_LIMIT - 1}"}
_CRED_KEYS = frozenset({"sig", "signature", "token", "key", "expires", "se", "sp"})
_YOUTUBE_HOSTS = frozenset(
    {
        "youtube.com",
        "www.youtube.com",
        "m.youtube.com",
        "music.youtube.com",
        "youtu.be",
        "www.youtu.be",
    }
)


class SourceUnavailable(Exception):
    """HTTP status was not 2xx after retries. Message includes status and the redacted URL."""

    def __init__(self, status: int, url: str) -> None:
        self.status = status
        self.url = url
        super().__init__(f"HTTP {status} for {url}")


@dataclass(frozen=True)
class _Probe:
    status_code: int
    content_type: str | None
    content_length: int | None


class HttpSource:
    name = "http"

    def __init__(
        self,
        *,
        config: Config | None = None,
        timeout_s: float | None = None,
        client: httpx.Client | None = None,
    ) -> None:
        self._timeout_s = _resolve_timeout(config, timeout_s)
        self._client = client
        self._full_urls: dict[str, str] = {}
        self._after: dict[str, Resolved] = {}
        self._probes: dict[str, _Probe] = {}

    def matches(self, raw_input: str) -> bool:
        parts = urlsplit(raw_input)
        if parts.scheme.lower() not in {"http", "https"}:
            return False
        host = parts.hostname
        if not host:
            return False
        return not _is_youtube_host(host)

    def resolve(self, raw_input: str) -> Resolved:
        redacted = redact_url(raw_input)
        video_id = _url_id(raw_input)
        probe = self._head_or_range(raw_input, redacted)
        self._full_urls[video_id] = raw_input
        self._probes[video_id] = probe
        return Resolved(
            video_id=video_id,
            title=_title(raw_input),
            channel=urlsplit(raw_input).hostname or "",
            source=redacted,
            duration=0.0,
            has_captions=False,
        )

    def fetch_media(self, resolved: Resolved, dest_dir: Path) -> Path:
        dest_dir.mkdir(parents=True, exist_ok=True)
        cached = reused_media(dest_dir, resolved.video_id)
        if cached is not None:
            self._after[str(dest_dir)] = replace(
                resolved, duration=ffprobe_duration(cached, timeout_s=self._timeout_s)
            )
            return cached
        full_url = self._full_urls.get(resolved.video_id)
        if full_url is None:
            raise LookupError(
                f"no full URL for {resolved.video_id}; call resolve on this HttpSource first"
            )
        redacted = redact_url(full_url)
        dest, content_type = self._download(full_url, redacted, dest_dir)
        probe = self._probes.get(resolved.video_id)
        if content_type is None and probe is not None:
            content_type = probe.content_type
        write_json(
            dest_dir / "media.json",
            {
                "url": redacted,
                "content_type": content_type,
                "sha256": sha256_file(dest),
                "video_id": resolved.video_id,
                "bytes": dest.stat().st_size,
                "fetched_at": _fetched_at(),
            },
        )
        duration = ffprobe_duration(dest, timeout_s=self._timeout_s)
        self._after[str(dest_dir)] = replace(resolved, duration=duration)
        return dest

    def resolved_after_fetch(self, dest_dir: Path) -> Resolved:
        """``Resolved`` with ffprobe duration, filled in by ``fetch_media``."""
        try:
            return self._after[str(dest_dir)]
        except KeyError as exc:
            raise LookupError(f"no fetch for {dest_dir}") from exc

    def fetch_captions(self, resolved: Resolved) -> list[Segment] | None:
        del resolved
        return None

    def _client_ctx(self) -> AbstractContextManager[httpx.Client]:
        if self._client is not None:
            return nullcontext(self._client)
        return httpx.Client(
            timeout=self._timeout_s,
            follow_redirects=True,
            trust_env=False,
        )

    def _head_or_range(self, url: str, redacted: str) -> _Probe:
        last_status = 0

        def head() -> httpx.Response:
            nonlocal last_status
            with self._client_ctx() as client:
                resp = client.head(url)
            last_status = resp.status_code
            if _is_success(resp.status_code) or resp.status_code in {403, 405, 501}:
                return resp
            if _is_retryable(resp.status_code):
                return resp
            raise SourceUnavailable(resp.status_code, redacted)

        try:
            head_resp = call(head, timeout_s=self._timeout_s, classify=self._classify_head)
        except RetryExhausted as exc:
            raise SourceUnavailable(last_status, redacted) from exc
        except RequestFailed as exc:
            raise SourceUnavailable(last_status, redacted) from exc

        if _is_success(head_resp.status_code):
            return _probe_from_headers(head_resp.status_code, head_resp.headers)

        def ranged() -> _Probe:
            nonlocal last_status
            with self._client_ctx() as client, client.stream("GET", url, headers=_RANGE) as resp:
                last_status = resp.status_code
                headers = resp.headers
                if _is_success(resp.status_code):
                    _drain(resp, _SNIFF_LIMIT)
                    return _probe_from_headers(resp.status_code, headers)
                if _is_retryable(resp.status_code):
                    return _probe_from_headers(resp.status_code, headers)
                raise SourceUnavailable(resp.status_code, redacted)

        try:
            probe = call(ranged, timeout_s=self._timeout_s, classify=self._classify_get)
        except RetryExhausted as exc:
            raise SourceUnavailable(last_status, redacted) from exc
        except RequestFailed as exc:
            raise SourceUnavailable(last_status, redacted) from exc
        if not _is_success(probe.status_code):
            raise SourceUnavailable(probe.status_code, redacted)
        return probe

    def _download(self, url: str, redacted: str, dest_dir: Path) -> tuple[Path, str | None]:
        partial = dest_dir / "media.partial"
        last_status = 0
        content_type: str | None = None

        def pull() -> Path | _Probe:
            nonlocal last_status, content_type
            kind = None
            head = bytearray()
            try:
                with self._client_ctx() as client, client.stream("GET", url) as resp:
                    last_status = resp.status_code
                    content_type = resp.headers.get("content-type")
                    if _is_retryable(resp.status_code):
                        return _probe_from_headers(resp.status_code, resp.headers)
                    if not _is_success(resp.status_code):
                        raise SourceUnavailable(resp.status_code, redacted)
                    with partial.open("wb") as out:
                        for chunk in resp.iter_bytes():
                            if not chunk:
                                continue
                            out.write(chunk)
                            if kind is None:
                                remain = _SNIFF_LIMIT - len(head)
                                if remain > 0:
                                    head.extend(chunk[:remain])
                                if len(head) >= _SNIFF_LIMIT:
                                    kind = _sniff_or_raise(bytes(head))
                        if kind is None:
                            kind = _sniff_or_raise(bytes(head))
            except BaseException:
                partial.unlink(missing_ok=True)
                raise
            dest = dest_dir / f"media.{kind.value}"
            partial.replace(dest)
            _remove_other_media(dest_dir, dest)
            return dest

        try:
            dest = call(pull, timeout_s=self._timeout_s, classify=self._classify_get)
        except RetryExhausted as exc:
            partial.unlink(missing_ok=True)
            raise SourceUnavailable(last_status, redacted) from exc
        except RequestFailed as exc:
            partial.unlink(missing_ok=True)
            raise SourceUnavailable(last_status, redacted) from exc
        except RequestTimeout:
            partial.unlink(missing_ok=True)
            raise
        if not isinstance(dest, Path):
            raise SourceUnavailable(last_status, redacted)
        return dest, content_type

    def _classify_head(self, exc_or_response: object) -> Outcome:
        if isinstance(exc_or_response, (NotMediaError, MediaUnreadable, SourceUnavailable)):
            raise exc_or_response
        if isinstance(exc_or_response, httpx.TimeoutException):
            return Outcome(status="timeout")
        if isinstance(exc_or_response, httpx.RequestError):
            return Outcome(status="retry")
        status = getattr(exc_or_response, "status_code", None)
        if isinstance(status, int):
            if _is_success(status) or status in {403, 405, 501}:
                return Outcome(status="ok")
            if _is_retryable(status):
                return Outcome(status="retry", retry_after=_retry_after(exc_or_response))
            return Outcome(status="fail")
        return Outcome(status="fail")

    def _classify_get(self, exc_or_response: object) -> Outcome:
        if isinstance(exc_or_response, Path):
            return Outcome(status="ok")
        if isinstance(exc_or_response, (NotMediaError, MediaUnreadable, SourceUnavailable)):
            raise exc_or_response
        if isinstance(exc_or_response, httpx.TimeoutException):
            return Outcome(status="timeout")
        if isinstance(exc_or_response, httpx.RequestError):
            return Outcome(status="retry")
        status = getattr(exc_or_response, "status_code", None)
        if isinstance(status, int):
            if _is_success(status):
                return Outcome(status="ok")
            if _is_retryable(status):
                return Outcome(status="retry", retry_after=_retry_after(exc_or_response))
            return Outcome(status="fail")
        return Outcome(status="fail")


def cli_flags() -> list:
    return []


def preflight_checks(config: Config) -> list[Check]:
    del config
    return []


def redact_url(url: str) -> str:
    parts = urlsplit(url)
    if not parts.query:
        return url
    pairs = parse_qsl(parts.query, keep_blank_values=True)
    if not any(_is_cred_key(key) for key, _ in pairs):
        return url
    redacted = [(key, "***" if _is_cred_key(key) else value) for key, value in pairs]
    query = urlencode(redacted, safe="*")
    return urlunsplit((parts.scheme, parts.netloc, parts.path, query, parts.fragment))


def _url_id(url: str) -> str:
    parts = urlsplit(url)
    stripped = urlunsplit((parts.scheme, parts.netloc, parts.path, "", parts.fragment))
    return hashlib.sha256(stripped.encode()).hexdigest()


def _title(url: str) -> str:
    segment = urlsplit(url).path.rsplit("/", 1)[-1]
    if not segment:
        return urlsplit(url).hostname or url
    return unquote(segment)


def _is_youtube_host(host: str) -> bool:
    host = host.lower().rstrip(".")
    if host in _YOUTUBE_HOSTS:
        return True
    return host.endswith(".youtube.com") or host.endswith(".youtu.be")


def _is_cred_key(name: str) -> bool:
    lower = name.lower()
    if lower.startswith("x-amz-"):
        return True
    token = lower.replace("_", "-")
    first = token.split("-", 1)[0]
    return first in _CRED_KEYS or token in _CRED_KEYS


def _is_success(status: int) -> bool:
    return 200 <= status < 300


def _is_retryable(status: int) -> bool:
    return status == 429 or status >= 500


def _retry_after(response: object) -> float | None:
    headers = getattr(response, "headers", None)
    if headers is None:
        return None
    raw = headers.get("Retry-After")
    if raw is None:
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def _drain(resp: httpx.Response, limit: int) -> None:
    n = 0
    for chunk in resp.iter_bytes():
        n += len(chunk)
        if n >= limit:
            break


def _probe_from_headers(status_code: int, headers: httpx.Headers) -> _Probe:
    raw_len = headers.get("content-length")
    length: int | None
    try:
        length = int(raw_len) if raw_len is not None else None
    except ValueError:
        length = None
    return _Probe(
        status_code=status_code,
        content_type=headers.get("content-type"),
        content_length=length,
    )


def _sniff_or_raise(data: bytes):
    try:
        return sniff(data)
    except NotMediaError:
        raise NotMediaError(_NOT_MEDIA) from None
