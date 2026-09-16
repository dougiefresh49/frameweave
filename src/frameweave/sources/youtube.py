"""YouTube resolve and media fetch with the android → mweb → web client chain.

``fetch_captions`` returns ``None`` here; issue #8 owns caption downloading.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import subprocess
import sys
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qs, urlparse

from frameweave.types import Chapter, Resolved, Segment
from frameweave.util.media import NotMediaError, sniff
from frameweave.util.retry import (
    Outcome,
    RequestFailed,
    RequestTimeout,
    RetryExhausted,
    call,
)

if TYPE_CHECKING:
    from frameweave.config import Check, Config, FlagSpec

log = logging.getLogger("frameweave.sources.youtube")

YTDLP_PIN = "2026.8.19"
CLIENT_CHAIN = ("android", "mweb", "web")
FORMAT_LADDER = "bv*[height<=720]+ba/b[height<=720]/b"
FORMAT_PROGRESSIVE = "b"

_VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
_URL_RE = re.compile(r"https?://[^\s<>\"']+")
_YOUTUBE_HOSTS = {
    "youtube.com",
    "www.youtube.com",
    "m.youtube.com",
    "music.youtube.com",
    "youtu.be",
    "www.youtu.be",
}

Runner = Callable[[list[str]], subprocess.CompletedProcess[str]]


class SourceBusy(Exception):
    """Another process holds the source claim directory."""

    def __init__(self, pid: int) -> None:
        super().__init__(f"source busy: claimed by pid {pid}")
        self.pid = pid


class MediaUnreadable(Exception):
    """Downloaded bytes exist but ffprobe cannot read them."""

    def __init__(self, path: Path, stderr: str) -> None:
        super().__init__(f"media unreadable: {path}: {stderr.strip()}")
        self.path = path
        self.stderr = stderr


class YtDlpError(Exception):
    """yt-dlp exited non-zero or returned unusable output."""

    def __init__(self, message: str, *, returncode: int | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.returncode = returncode


class ClientAdvance(Exception):
    """403 or format-unavailable — try the next player client."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


@dataclass(frozen=True)
class _DownloadOk:
    path: Path
    info: dict[str, Any]
    client: str
    degraded: bool


@dataclass(frozen=True)
class _AdvanceResult:
    """Returned through ``retry.call`` so 403 does not become a bare RequestFailed."""

    message: str


def _default_runner(args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "yt_dlp", *args],
        capture_output=True,
        text=True,
        check=False,
    )


def _version_tuple(version: str) -> tuple[int, ...]:
    parts: list[int] = []
    for piece in version.split("."):
        if piece.isdigit():
            parts.append(int(piece))
        else:
            break
    return tuple(parts)


def _is_client_advance(message: str) -> bool:
    lower = message.lower()
    return "403" in lower or "requested format not available" in lower


def classify(exc_or_response: object) -> Outcome:
    """Classify a yt-dlp result for ``retry.call``."""
    if isinstance(exc_or_response, (_DownloadOk, _AdvanceResult, dict)):
        return Outcome(status="ok")
    if isinstance(exc_or_response, subprocess.TimeoutExpired):
        return Outcome(status="timeout")
    if isinstance(exc_or_response, YtDlpError):
        lower = exc_or_response.message.lower()
        if "timed out" in lower or "timeout" in lower:
            return Outcome(status="timeout")
        return Outcome(status="retry")
    if isinstance(exc_or_response, Exception):
        return Outcome(status="fail")
    return Outcome(status="fail")


def _extract_video_id(raw: str) -> str | None:
    raw = raw.strip()
    if _VIDEO_ID_RE.match(raw):
        return raw
    try:
        parsed = urlparse(raw)
    except ValueError:
        return None
    host = (parsed.hostname or "").lower()
    if host not in _YOUTUBE_HOSTS:
        return None
    if host in {"youtu.be", "www.youtu.be"}:
        candidate = parsed.path.lstrip("/").split("/")[0]
        return candidate if _VIDEO_ID_RE.match(candidate) else None
    qs = parse_qs(parsed.query)
    if "v" in qs and qs["v"]:
        candidate = qs["v"][0]
        return candidate if _VIDEO_ID_RE.match(candidate) else None
    parts = [p for p in parsed.path.split("/") if p]
    if len(parts) >= 2 and parts[0] in {"embed", "shorts", "live", "v"}:
        candidate = parts[1]
        return candidate if _VIDEO_ID_RE.match(candidate) else None
    return None


def _start_time_param(raw: str) -> str | None:
    try:
        parsed = urlparse(raw.strip())
    except ValueError:
        return None
    qs = parse_qs(parsed.query)
    values = qs.get("t") or qs.get("start")
    if not values:
        return None
    return values[0]


def _canonical_source(video_id: str, raw: str) -> str:
    base = f"https://www.youtube.com/watch?v={video_id}"
    t = _start_time_param(raw)
    if t is None:
        return base
    return f"{base}&t={t}"


def _links_from_description(description: str) -> list[str]:
    seen: set[str] = set()
    links: list[str] = []
    for match in _URL_RE.finditer(description or ""):
        url = match.group(0).rstrip(").,;]'\"")
        if url not in seen:
            seen.add(url)
            links.append(url)
    return links


def _has_english_captions(info: dict[str, Any]) -> bool:
    for key in ("subtitles", "automatic_captions"):
        tracks = info.get(key) or {}
        if not isinstance(tracks, dict):
            continue
        for lang in tracks:
            if str(lang).lower().startswith("en"):
                return True
    return False


def _upload_date_iso(value: object) -> str | None:
    if not value or not isinstance(value, str) or len(value) != 8 or not value.isdigit():
        return None
    return f"{value[0:4]}-{value[4:6]}-{value[6:8]}"


def _chapters(info: dict[str, Any]) -> list[Chapter]:
    raw = info.get("chapters") or []
    out: list[Chapter] = []
    if not isinstance(raw, list):
        return out
    for item in raw:
        if not isinstance(item, dict):
            continue
        start = item.get("start_time", item.get("start"))
        title = item.get("title")
        if start is None or title is None:
            continue
        out.append(Chapter(start=float(start), title=str(title)))
    return out


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _cleanup_partials(dest_dir: Path) -> None:
    for pattern in ("*.part", "*.ytdl"):
        for path in dest_dir.glob(pattern):
            path.unlink(missing_ok=True)


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _remove_claim(claim_dir: Path) -> None:
    if not claim_dir.exists():
        return
    for child in claim_dir.iterdir():
        child.unlink(missing_ok=True)
    try:
        claim_dir.rmdir()
    except OSError:
        pass


@contextmanager
def claim(source_dir: Path) -> Iterator[None]:
    """Exclusive mkdir claim under ``source_dir/.claim/`` with a pid file.

    A second concurrent claim raises ``SourceBusy`` naming the live holder pid
    (does not wait). A claim whose pid is dead is treated as stale and taken.
    """
    source_dir.mkdir(parents=True, exist_ok=True)
    claim_dir = source_dir / ".claim"
    while True:
        try:
            claim_dir.mkdir()
            break
        except FileExistsError:
            pid_path = claim_dir / "pid"
            try:
                recorded = int(pid_path.read_text().strip())
            except (OSError, ValueError):
                _remove_claim(claim_dir)
                continue
            if _pid_alive(recorded):
                raise SourceBusy(recorded) from None
            _remove_claim(claim_dir)
    (claim_dir / "pid").write_text(f"{os.getpid()}\n")
    try:
        yield
    finally:
        _remove_claim(claim_dir)


def cli_flags() -> list[FlagSpec]:
    from frameweave.config import FlagSpec

    return [
        FlagSpec(
            name="--youtube-client",
            dest="youtube_client",
            type=str,
            help="force one player client instead of the chain",
            default=None,
        )
    ]


def preflight_checks(config: Config) -> list[Check]:
    from frameweave.config import Check

    del config  # version check does not read config fields
    try:
        import yt_dlp.version as ytdlp_version

        version = ytdlp_version.__version__
        ok = _version_tuple(version) >= _version_tuple(YTDLP_PIN)
        detail = f"yt-dlp {version}" + ("" if ok else f" (need >={YTDLP_PIN})")
    except Exception as exc:  # noqa: BLE001 — doctor row must never raise
        return [
            Check(
                name="yt-dlp",
                ok=False,
                detail=f"not importable: {exc}",
                remedy="uv sync",
                required=True,
            )
        ]
    return [
        Check(
            name="yt-dlp",
            ok=ok,
            detail=detail,
            remedy="uv sync",
            required=True,
        )
    ]


class YouTubeSource:
    """Resolve and fetch YouTube videos via yt-dlp as a subprocess."""

    name = "youtube"

    def __init__(
        self,
        *,
        runner: Runner | None = None,
        timeout_s: float = 120.0,
        youtube_client: str | None = None,
        attempts: int = 3,
    ) -> None:
        self._runner = runner or _default_runner
        self._timeout_s = timeout_s
        self._youtube_client = youtube_client
        self._attempts = attempts

    def matches(self, raw_input: str) -> bool:
        return _extract_video_id(raw_input) is not None

    def resolve(self, raw_input: str) -> Resolved:
        video_id = _extract_video_id(raw_input)
        if video_id is None:
            raise ValueError(f"not a YouTube input: {raw_input!r}")
        info = self._resolve_info(raw_input if "://" in raw_input else video_id)
        channel = str(info.get("uploader") or info.get("channel") or "")
        description = str(info.get("description") or "")
        return Resolved(
            video_id=str(info.get("id") or video_id),
            title=str(info.get("title") or ""),
            channel=channel,
            source=_canonical_source(video_id, raw_input),
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

        clients = (
            [self._youtube_client] if self._youtube_client else list(CLIENT_CHAIN)
        )
        last_error: BaseException | None = None
        for index, client in enumerate(clients):
            is_last = index == len(clients) - 1
            try:
                result = self._attempt_download(
                    resolved, dest_dir, client, degraded=False
                )
                return self._publish(result, dest_dir)
            except ClientAdvance as exc:
                last_error = exc
                if is_last:
                    try:
                        result = self._attempt_download(
                            resolved, dest_dir, client, degraded=True
                        )
                        return self._publish(result, dest_dir)
                    except Exception as fallback_exc:
                        last_error = fallback_exc
                        break
                continue
            except RequestTimeout:
                raise
            except (RequestFailed, RetryExhausted) as exc:
                last_error = exc
                if is_last:
                    break
                continue

        if last_error is not None:
            raise last_error
        raise YtDlpError("all player clients failed")

    def fetch_captions(self, resolved: Resolved) -> list[Segment] | None:
        """Captions are owned by issue #8; this method only satisfies the protocol."""
        del resolved
        return None

    def _resolve_info(self, target: str) -> dict[str, Any]:
        def once() -> dict[str, Any]:
            proc = self._runner(
                ["-J", "--no-download", "--skip-download", "--", target]
            )
            if proc.returncode != 0:
                err = (proc.stderr or proc.stdout or "").strip() or "yt-dlp failed"
                raise YtDlpError(err, returncode=proc.returncode)
            try:
                return json.loads(proc.stdout)
            except json.JSONDecodeError as exc:
                raise YtDlpError(f"invalid yt-dlp JSON: {exc}") from exc

        return call(
            once,
            attempts=self._attempts,
            timeout_s=self._timeout_s,
            classify=classify,
        )

    def _reuse_cached(self, dest_dir: Path) -> Path | None:
        meta_path = dest_dir / "media.json"
        if not meta_path.exists():
            return None
        try:
            meta = json.loads(meta_path.read_text())
        except (OSError, json.JSONDecodeError):
            return None
        expected = meta.get("sha256")
        if not expected:
            return None
        for path in sorted(dest_dir.glob("media.*")):
            if path.name == "media.json" or path.suffix in {".part", ".ytdl"}:
                continue
            if path.name.endswith(".part") or path.name.endswith(".ytdl"):
                continue
            try:
                if _sha256_file(path) == expected:
                    return path
            except OSError:
                continue
        return None

    def _attempt_download(
        self,
        resolved: Resolved,
        dest_dir: Path,
        client: str,
        *,
        degraded: bool,
    ) -> _DownloadOk:
        result = call(
            lambda: self._download_once(resolved, dest_dir, client, degraded=degraded),
            attempts=self._attempts,
            timeout_s=self._timeout_s,
            classify=classify,
        )
        if isinstance(result, _AdvanceResult):
            raise ClientAdvance(result.message)
        return result

    def _download_once(
        self,
        resolved: Resolved,
        dest_dir: Path,
        client: str,
        *,
        degraded: bool,
    ) -> _DownloadOk | _AdvanceResult:
        _cleanup_partials(dest_dir)
        fmt = FORMAT_PROGRESSIVE if degraded else FORMAT_LADDER
        outtmpl = str(dest_dir / "media.%(ext)s")
        args = [
            "-f",
            fmt,
            "--extractor-args",
            f"youtube:player_client={client}",
            "-o",
            outtmpl,
            "--",
            resolved.source,
        ]
        proc = self._runner(args)
        _cleanup_partials(dest_dir)
        if proc.returncode != 0:
            err = (proc.stderr or proc.stdout or "").strip() or "yt-dlp failed"
            if _is_client_advance(err):
                return _AdvanceResult(err)
            raise YtDlpError(err, returncode=proc.returncode)
        media = self._find_media_file(dest_dir)
        if media is None:
            raise YtDlpError("yt-dlp exited 0 but wrote no media file")
        info: dict[str, Any] = {}
        for candidate in dest_dir.glob("*.info.json"):
            try:
                info = json.loads(candidate.read_text())
                candidate.unlink(missing_ok=True)
                break
            except (OSError, json.JSONDecodeError):
                continue
        if not info and (proc.stdout or "").strip().startswith("{"):
            try:
                info = json.loads(proc.stdout.strip().splitlines()[-1])
            except json.JSONDecodeError:
                info = {}
        return _DownloadOk(path=media, info=info, client=client, degraded=degraded)

    def _find_media_file(self, dest_dir: Path) -> Path | None:
        candidates = [
            p
            for p in dest_dir.glob("media.*")
            if p.name != "media.json"
            and not p.name.endswith(".part")
            and not p.name.endswith(".ytdl")
            and p.suffix != ".json"
        ]
        if not candidates:
            return None
        return max(candidates, key=lambda p: p.stat().st_mtime)

    def _publish(self, result: _DownloadOk, dest_dir: Path) -> Path:
        path = result.path
        try:
            kind = sniff(path)
        except NotMediaError:
            self._ffprobe_or_raise(path)
            raise
        final = dest_dir / f"media.{kind.value}"
        if path.resolve() != final.resolve():
            path.replace(final)
            path = final
        self._ffprobe_or_raise(path)

        sha = _sha256_file(path)
        meta = {
            **{k: v for k, v in result.info.items() if k != "formats"},
            "client": result.client,
            "format_id": result.info.get("format_id"),
            "sha256": sha,
            "bytes": path.stat().st_size,
            "fetched_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        }
        if result.degraded:
            meta["degraded"] = True
        tmp = dest_dir / "media.json.partial"
        tmp.write_text(json.dumps(meta, indent=2) + "\n")
        tmp.replace(dest_dir / "media.json")
        log.info("youtube fetch used player_client=%s path=%s", result.client, path)
        return path

    def _ffprobe_or_raise(self, path: Path) -> float:
        proc = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "csv=p=0",
                str(path),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.returncode != 0:
            stderr = proc.stderr or proc.stdout or "ffprobe failed"
            path.unlink(missing_ok=True)
            raise MediaUnreadable(path, stderr)
        try:
            return float((proc.stdout or "").strip().splitlines()[0])
        except (ValueError, IndexError):
            stderr = proc.stderr or "ffprobe returned no duration"
            path.unlink(missing_ok=True)
            raise MediaUnreadable(path, stderr) from None
