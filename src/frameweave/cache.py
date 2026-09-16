"""Cache size report and prune for ``sources/`` and ``runs/``.

Never deletes outside ``cache_dir``. Sources with pending obligations under any
``runs/<id>/*/obligations.json`` are kept. Library code returns reports; the CLI
prints them.
"""

from __future__ import annotations

import json
import os
import re
import shutil
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from frameweave.config import Check, Config, FlagSpec

_OLDER_THAN_RE = re.compile(r"^(\d+)([dh])$", re.IGNORECASE)
_OBLIGATIONS = "obligations.json"


@dataclass(frozen=True)
class VideoSize:
    """Bytes for one video id under ``sources/`` and ``runs/``."""

    video_id: str
    sources_bytes: int
    runs_bytes: int
    oldest_mtime: float | None
    newest_mtime: float | None

    @property
    def total_bytes(self) -> int:
        return self.sources_bytes + self.runs_bytes


@dataclass(frozen=True)
class CacheReport:
    """Aggregate cache footprint under ``cache_dir``."""

    videos: tuple[VideoSize, ...]
    total_bytes: int
    oldest_mtime: float | None
    newest_mtime: float | None


@dataclass(frozen=True)
class PruneTarget:
    """One directory prune would remove (``sources/<id>`` or ``runs/<id>``)."""

    kind: str  # "sources" | "runs"
    video_id: str
    path: Path
    bytes: int


@dataclass(frozen=True)
class PruneReport:
    """Plan or result of a prune pass."""

    targets: tuple[PruneTarget, ...]
    bytes_freed: int
    dry_run: bool


def cli_flags() -> list[FlagSpec]:
    return []


def preflight_checks(config: Config) -> list[Check]:
    del config
    return []


def parse_older_than(text: str) -> timedelta:
    """Parse ``Nd`` / ``Nh`` into a timedelta. Raises ``ValueError`` on bad input."""
    match = _OLDER_THAN_RE.fullmatch(text.strip())
    if not match:
        raise ValueError(f"invalid --older-than {text!r}; expected Nd or Nh (e.g. 30d, 12h)")
    amount = int(match.group(1))
    unit = match.group(2).lower()
    if unit == "d":
        return timedelta(days=amount)
    return timedelta(hours=amount)


def size(cache_dir: Path) -> CacheReport:
    """Return per-video and total byte counts plus oldest/newest file mtimes."""
    root = Path(cache_dir)
    sources_root = root / "sources"
    runs_root = root / "runs"
    ids = _video_ids(sources_root, runs_root)
    rows: list[VideoSize] = []
    oldest: float | None = None
    newest: float | None = None
    total = 0
    for video_id in ids:
        src_bytes, src_old, src_new = _dir_stats(sources_root / video_id)
        run_bytes, run_old, run_new = _dir_stats(runs_root / video_id)
        vid_old = _min_mtime(src_old, run_old)
        vid_new = _max_mtime(src_new, run_new)
        rows.append(
            VideoSize(
                video_id=video_id,
                sources_bytes=src_bytes,
                runs_bytes=run_bytes,
                oldest_mtime=vid_old,
                newest_mtime=vid_new,
            )
        )
        total += src_bytes + run_bytes
        oldest = _min_mtime(oldest, vid_old)
        newest = _max_mtime(newest, vid_new)
    rows.sort(key=lambda row: (-row.total_bytes, row.video_id))
    # Files outside sources/ and runs/ still count toward the total.
    other = _other_bytes(root, sources_root, runs_root)
    total += other
    if other:
        other_old, other_new = _dir_mtime_bounds(root, skip={sources_root, runs_root})
        oldest = _min_mtime(oldest, other_old)
        newest = _max_mtime(newest, other_new)
    return CacheReport(
        videos=tuple(rows),
        total_bytes=total,
        oldest_mtime=oldest,
        newest_mtime=newest,
    )


def prune(
    cache_dir: Path,
    older_than: timedelta,
    *,
    dry_run: bool,
    now: datetime | None = None,
) -> PruneReport:
    """Delete eligible ``sources/<id>`` and ``runs/<id>`` trees.

    A source is eligible when its newest file is older than the cutoff and no
    ``runs/<id>/*/obligations.json`` still lists a pending item. A runs tree is
    eligible when its source is gone (or also being pruned) or its newest file
    is older than the cutoff. Trees with pending obligations are never removed.
    """
    root = Path(cache_dir).resolve()
    sources_root = root / "sources"
    runs_root = root / "runs"
    moment = now if now is not None else datetime.now().astimezone()
    cutoff = moment.timestamp() - older_than.total_seconds()

    pending = _video_ids_with_pending(runs_root)
    sources_to_delete: set[str] = set()
    if sources_root.is_dir():
        for path in sorted(sources_root.iterdir()):
            if not path.is_dir():
                continue
            video_id = path.name
            if video_id in pending:
                continue
            _bytes, _old, newest = _dir_stats(path)
            if newest is not None and newest < cutoff:
                sources_to_delete.add(video_id)

    runs_to_delete: set[str] = set()
    if runs_root.is_dir():
        for path in sorted(runs_root.iterdir()):
            if not path.is_dir():
                continue
            video_id = path.name
            if video_id in pending:
                continue
            source_path = sources_root / video_id
            source_gone = (not source_path.is_dir()) or video_id in sources_to_delete
            _bytes, _old, newest = _dir_stats(path)
            age_ok = newest is not None and newest < cutoff
            if source_gone or age_ok:
                runs_to_delete.add(video_id)

    targets: list[PruneTarget] = []
    # Runs first so a later source delete does not leave orphan run trees mid-pass.
    for video_id in sorted(runs_to_delete):
        path = runs_root / video_id
        targets.append(
            PruneTarget(
                kind="runs",
                video_id=video_id,
                path=path,
                bytes=_dir_stats(path)[0],
            )
        )
    for video_id in sorted(sources_to_delete):
        path = sources_root / video_id
        targets.append(
            PruneTarget(
                kind="sources",
                video_id=video_id,
                path=path,
                bytes=_dir_stats(path)[0],
            )
        )

    freed = sum(t.bytes for t in targets)
    if not dry_run:
        for target in targets:
            _safe_rmtree(target.path, root)

    return PruneReport(targets=tuple(targets), bytes_freed=freed, dry_run=dry_run)


def _video_ids(sources_root: Path, runs_root: Path) -> list[str]:
    ids: set[str] = set()
    for root in (sources_root, runs_root):
        if not root.is_dir():
            continue
        for path in root.iterdir():
            if path.is_dir():
                ids.add(path.name)
    return sorted(ids)


def _dir_stats(path: Path) -> tuple[int, float | None, float | None]:
    if not path.is_dir():
        return 0, None, None
    total = 0
    oldest: float | None = None
    newest: float | None = None
    for root, _dirs, files in os.walk(path):
        for name in files:
            file_path = Path(root) / name
            try:
                st = file_path.stat()
            except OSError:
                continue
            total += st.st_size
            oldest = st.st_mtime if oldest is None else min(oldest, st.st_mtime)
            newest = st.st_mtime if newest is None else max(newest, st.st_mtime)
    return total, oldest, newest


def _other_bytes(root: Path, sources_root: Path, runs_root: Path) -> int:
    if not root.is_dir():
        return 0
    total = 0
    skip = {sources_root.resolve(), runs_root.resolve()}
    for dirpath, dirnames, filenames in os.walk(root):
        current = Path(dirpath).resolve()
        # Do not descend into sources/ or runs/; their sizes are counted per video.
        if current in skip:
            dirnames.clear()
            continue
        dirnames[:] = [
            name
            for name in dirnames
            if (current / name).resolve() not in skip
        ]
        for name in filenames:
            try:
                total += (current / name).stat().st_size
            except OSError:
                continue
    return total


def _dir_mtime_bounds(
    root: Path, *, skip: set[Path]
) -> tuple[float | None, float | None]:
    if not root.is_dir():
        return None, None
    skip_resolved = {p.resolve() for p in skip}
    oldest: float | None = None
    newest: float | None = None
    for dirpath, dirnames, filenames in os.walk(root):
        current = Path(dirpath).resolve()
        if current in skip_resolved:
            dirnames.clear()
            continue
        dirnames[:] = [
            name
            for name in dirnames
            if (current / name).resolve() not in skip_resolved
        ]
        for name in filenames:
            try:
                mtime = (current / name).stat().st_mtime
            except OSError:
                continue
            oldest = mtime if oldest is None else min(oldest, mtime)
            newest = mtime if newest is None else max(newest, mtime)
    return oldest, newest


def _video_ids_with_pending(runs_root: Path) -> set[str]:
    pending: set[str] = set()
    if not runs_root.is_dir():
        return pending
    for video_dir in runs_root.iterdir():
        if not video_dir.is_dir():
            continue
        for run_dir in video_dir.iterdir():
            if not run_dir.is_dir():
                continue
            if _has_pending_obligations(run_dir / _OBLIGATIONS):
                pending.add(video_dir.name)
                break
    return pending


def _has_pending_obligations(path: Path) -> bool:
    if not path.is_file():
        return False
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    if not isinstance(data, list):
        return False
    return any(isinstance(item, dict) and item.get("id") for item in data)


def _safe_rmtree(path: Path, cache_root: Path) -> None:
    resolved = path.resolve()
    root = cache_root.resolve()
    if resolved == root or root not in resolved.parents:
        raise ValueError(f"refusing to delete outside cache_dir: {path}")
    if resolved.is_dir():
        shutil.rmtree(resolved)


def _min_mtime(a: float | None, b: float | None) -> float | None:
    if a is None:
        return b
    if b is None:
        return a
    return min(a, b)


def _max_mtime(a: float | None, b: float | None) -> float | None:
    if a is None:
        return b
    if b is None:
        return a
    return max(a, b)
