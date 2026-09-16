"""Local file source. ``video_id`` is the file's sha256; later stages read the dest_dir copy."""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from frameweave.types import Resolved, Segment
from frameweave.util.media import sniff
from frameweave.util.retry import Outcome, RequestFailed, RequestTimeout, RetryExhausted, call

_TIMEOUT_S = 120.0
_MEDIA_SKIP = {".json", ".partial", ".tmp"}
_FFPROBE_DURATION = [
    "ffprobe",
    "-v",
    "error",
    "-show_entries",
    "format=duration",
    "-of",
    "csv=p=0",
]


try:
    from frameweave.config import Check as Check
except ModuleNotFoundError:  # issue #4 not in this worktree yet

    from dataclasses import dataclass

    @dataclass(frozen=True)
    class Check:
        """Same fields as ``frameweave.config.Check``. Fallback until #4 merges."""

        name: str
        ok: bool
        detail: str
        remedy: str
        required: bool = True


class MediaUnreadable(Exception):
    """ffprobe could not read the file. Message names the path and stderr."""

    def __init__(self, path: Path, stderr: str) -> None:
        self.path = path
        self.stderr = stderr
        super().__init__(f"ffprobe failed on {path}: {stderr}")


class LocalFileSource:
    name = "local"

    def __init__(self, *, timeout_s: float = _TIMEOUT_S) -> None:
        self._timeout_s = timeout_s

    def matches(self, raw_input: str) -> bool:
        try:
            return Path(raw_input).expanduser().is_file()
        except OSError:
            return False

    def resolve(self, raw_input: str) -> Resolved:
        path = Path(raw_input).expanduser().absolute()
        video_id = sha256_file(path)
        title = path.stem.replace("_", " ").replace("-", " ")
        published = datetime.fromtimestamp(path.stat().st_mtime, UTC).date().isoformat()
        duration = ffprobe_duration(path, timeout_s=self._timeout_s)
        return Resolved(
            video_id=video_id,
            title=title,
            channel="Local recording",
            source=str(path),
            duration=duration,
            has_captions=False,
            published=published,
        )

    def fetch_media(self, resolved: Resolved, dest_dir: Path) -> Path:
        dest_dir.mkdir(parents=True, exist_ok=True)
        cached = reused_media(dest_dir)
        if cached is not None:
            return cached
        original = Path(resolved.source)
        kind = sniff(original)
        dest = dest_dir / f"media.{kind.value}"
        tmp = dest_dir / "media.partial"
        try:
            shutil.copyfile(original, tmp)
            tmp.replace(dest)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
        _remove_other_media(dest_dir, dest)
        digest = sha256_file(dest)
        write_json(
            dest_dir / "media.json",
            {
                "sha256": digest,
                "bytes": dest.stat().st_size,
                "original_path": str(original),
                "fetched_at": _fetched_at(),
            },
        )
        return dest

    def fetch_captions(self, resolved: Resolved) -> list[Segment] | None:
        del resolved
        return None


def cli_flags() -> list:
    return []


def preflight_checks(config: object) -> list[Check]:
    del config
    try:
        proc = subprocess.run(
            ["ffprobe", "-version"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except FileNotFoundError:
        return [
            Check(
                name="ffprobe",
                ok=False,
                detail="ffprobe not on PATH",
                remedy="brew install ffmpeg",
            )
        ]
    except subprocess.TimeoutExpired:
        return [
            Check(
                name="ffprobe",
                ok=False,
                detail="ffprobe -version timed out",
                remedy="brew install ffmpeg",
            )
        ]
    text = proc.stdout or proc.stderr
    first = text.splitlines()[0] if text.strip() else "ffprobe present"
    return [
        Check(
            name="ffprobe",
            ok=proc.returncode == 0,
            detail=first,
            remedy="brew install ffmpeg",
        )
    ]


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def ffprobe_duration(path: Path, *, timeout_s: float = _TIMEOUT_S) -> float:
    def run() -> subprocess.CompletedProcess[str]:
        try:
            proc = subprocess.run(
                [*_FFPROBE_DURATION, str(path)],
                capture_output=True,
                text=True,
                timeout=timeout_s,
            )
        except FileNotFoundError as exc:
            raise MediaUnreadable(path, "ffprobe not on PATH") from exc
        if proc.returncode != 0:
            raise MediaUnreadable(path, (proc.stderr or proc.stdout).strip())
        return proc

    def classify(exc_or_response: object) -> Outcome:
        if isinstance(exc_or_response, MediaUnreadable):
            raise exc_or_response
        if isinstance(exc_or_response, subprocess.TimeoutExpired):
            return Outcome(status="timeout")
        if isinstance(exc_or_response, subprocess.CompletedProcess):
            return Outcome(status="ok")
        return Outcome(status="fail")

    try:
        proc = call(run, attempts=0, timeout_s=timeout_s, classify=classify)
    except RequestTimeout as exc:
        raise MediaUnreadable(path, str(exc)) from exc
    except (RequestFailed, RetryExhausted) as exc:
        raise MediaUnreadable(path, "ffprobe failed") from exc
    raw = proc.stdout.strip()
    try:
        return float(raw)
    except ValueError:
        raise MediaUnreadable(path, proc.stderr.strip() or raw) from None


def write_json(path: Path, data: dict[str, Any]) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n")
    tmp.replace(path)


def find_media(dest_dir: Path) -> Path | None:
    found = [
        p
        for p in dest_dir.iterdir()
        if p.is_file() and p.name.startswith("media.") and p.suffix not in _MEDIA_SKIP
    ]
    return found[0] if found else None


def reused_media(dest_dir: Path) -> Path | None:
    meta_path = dest_dir / "media.json"
    if not meta_path.is_file():
        return None
    try:
        meta = json.loads(meta_path.read_text())
    except json.JSONDecodeError:
        return None
    expected = meta.get("sha256")
    media = find_media(dest_dir)
    if not expected or media is None:
        return None
    if sha256_file(media) == expected:
        return media
    return None


def _fetched_at() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _remove_other_media(dest_dir: Path, keep: Path) -> None:
    for path in dest_dir.iterdir():
        if (
            path.is_file()
            and path.name.startswith("media.")
            and path.suffix not in _MEDIA_SKIP
            and path != keep
        ):
            path.unlink()
