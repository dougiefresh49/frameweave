"""Generate a tiny testsrc video with a burned-in timecode. Not committed."""

from __future__ import annotations

import subprocess
import warnings
from pathlib import Path

drew_timecode = False

_FONT_CANDIDATES = (
    "/System/Library/Fonts/Supplemental/Arial.ttf",
    "/System/Library/Fonts/Supplemental/Helvetica.ttc",
    "/Library/Fonts/Arial.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/TTF/DejaVuSans.ttf",
)


def _drawtext_filter() -> str:
    """Build drawtext vf; omit fontfile when no known font is on disk."""
    font = next((path for path in _FONT_CANDIDATES if Path(path).is_file()), None)
    prefix = f"drawtext=fontfile={font}:" if font else "drawtext="
    return (
        prefix
        + r"text='%{pts\:hms}':fontsize=72:fontcolor=white:x=20:y=20:"
        "box=1:boxcolor=black@0.7"
    )


def make(dest: Path, seconds: int, fps: int = 5, size: str = "1280x720") -> Path:
    """Write an ffmpeg ``testsrc`` clip with ``drawtext`` timecode when available."""
    global drew_timecode
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    lavfi = f"testsrc=size={size}:rate={fps}:duration={seconds}"
    if _encode(dest, lavfi, vf=_drawtext_filter()):
        drew_timecode = True
        return dest
    warnings.warn(
        "ffmpeg drawtext unavailable; synthetic video has no burned-in timecode",
        UserWarning,
        stacklevel=2,
    )
    if not _encode(dest, lavfi, vf=None):
        raise RuntimeError("ffmpeg testsrc encode failed")
    drew_timecode = False
    return dest


def _encode(dest: Path, lavfi: str, vf: str | None) -> bool:
    cmd = ["ffmpeg", "-nostdin", "-y", "-f", "lavfi", "-i", lavfi]
    if vf:
        cmd.extend(["-vf", vf])
    cmd.extend(["-c:v", "libx264", "-pix_fmt", "yuv420p", str(dest)])
    proc = subprocess.run(cmd, capture_output=True)
    return proc.returncode == 0
