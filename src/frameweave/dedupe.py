"""Near-duplicate frame suppression via difference hash (dHash).

Optional ``dedupe`` extras group (pillow). When pillow is missing the pipeline
stage skips with one warning and leaves every frame kept.
"""

from __future__ import annotations

import importlib.util
from dataclasses import dataclass
from pathlib import Path

from frameweave.config import Check, Config, FlagSpec
from frameweave.types import Frame

# Tuned against tests/fixtures/frames-small-change (file name / digit / tab title):
# size 16 left those pairs at distance 0–5; size 32 clears threshold 6 on all three.
DEFAULT_SIZE = 32
DEFAULT_THRESHOLD = 6


@dataclass(frozen=True)
class Dropped:
    """One suppressed frame and the kept neighbor it was near."""

    frame: Frame
    distance: int
    near: str


@dataclass(frozen=True)
class DedupeResult:
    kept: list[Frame]
    dropped: list[Dropped]
    stats: str  # ``"N of M kept"``


def pillow_available() -> bool:
    return importlib.util.find_spec("PIL") is not None


def dhash(path: Path, size: int = DEFAULT_SIZE) -> int:
    """Difference hash of an image: grayscale, downscale, adjacent-pixel bits."""
    from PIL import Image

    if size < 1:
        raise ValueError(f"dhash size must be >= 1, got {size}")
    with Image.open(path) as image:
        gray = image.convert("L").resize((size + 1, size), Image.Resampling.LANCZOS)
    bits = 0
    for row in range(size):
        for col in range(size):
            left = gray.getpixel((col, row))
            right = gray.getpixel((col + 1, row))
            bits = (bits << 1) | (1 if left > right else 0)
    return bits


def hamming(a: int, b: int) -> int:
    return (a ^ b).bit_count()


def suppress(
    frames: list[Frame],
    frames_dir: Path,
    threshold: int = DEFAULT_THRESHOLD,
    keep_duplicates: bool = False,
    *,
    size: int = DEFAULT_SIZE,
) -> DedupeResult:
    """Keep a frame when Hamming distance to the last kept exceeds ``threshold``.

    Always keeps the first frame of a segment (``kind == "primary"``). The first
    frame in ``frames`` is always kept so the chain has a seed.
    """
    total = len(frames)
    if keep_duplicates or total == 0:
        return DedupeResult(list(frames), [], f"{total} of {total} kept")

    kept: list[Frame] = []
    dropped: list[Dropped] = []
    last_hash: int | None = None
    last_id: str | None = None

    for frame in frames:
        image_path = _image_path(frames_dir, frame)
        if not kept or frame.kind == "primary":
            kept.append(frame)
            last_hash = dhash(image_path, size=size)
            last_id = frame.id
            continue
        assert last_hash is not None and last_id is not None
        digest = dhash(image_path, size=size)
        distance = hamming(digest, last_hash)
        if distance > threshold:
            kept.append(frame)
            last_hash = digest
            last_id = frame.id
        else:
            dropped.append(Dropped(frame=frame, distance=distance, near=last_id))

    return DedupeResult(kept, dropped, f"{len(kept)} of {total} kept")


def _image_path(frames_dir: Path, frame: Frame) -> Path:
    """Resolve ``frame.path`` against the frames directory or its parent run dir."""
    raw = Path(frame.path)
    if raw.is_absolute():
        return raw
    direct = frames_dir / raw.name
    if direct.is_file():
        return direct
    parent = frames_dir.parent / raw
    if parent.is_file():
        return parent
    return direct


def cli_flags() -> list[FlagSpec]:
    # Flag lives on Config (--keep-duplicates); this module adds none.
    return []


def preflight_checks(_config: Config) -> list[Check]:
    if pillow_available():
        return [
            Check("pillow (dedupe)", True, "installed", "uv sync --extra dedupe", required=False)
        ]
    return [
        Check(
            "pillow (dedupe)",
            True,
            "not installed; near-duplicate stage will skip",
            "uv sync --extra dedupe",
            required=False,
        )
    ]
