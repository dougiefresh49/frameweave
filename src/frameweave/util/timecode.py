"""Parse and format HH:MM:SS(.t) timestamps."""

from __future__ import annotations

import re

_BARE = re.compile(r"^\d+(?:\.\d+)?$")


def parse(text: str) -> float:
    """Parse ``HH:MM:SS``, ``MM:SS``, ``HH:MM:SS.t`` (or finer), or bare seconds."""
    raw = text.strip()
    if not raw:
        raise ValueError("empty timecode")
    if raw.startswith("-"):
        raise ValueError(f"negative time: {text!r}")
    if _BARE.fullmatch(raw):
        value = float(raw)
        if value < 0:
            raise ValueError(f"negative time: {text!r}")
        return value
    parts = raw.split(":")
    if len(parts) == 2:
        minutes, seconds = parts
        value = _nonneg_int(minutes, text) * 60 + _nonneg_float(seconds, text)
    elif len(parts) == 3:
        hours, minutes, seconds = parts
        value = (
            _nonneg_int(hours, text) * 3600
            + _nonneg_int(minutes, text) * 60
            + _nonneg_float(seconds, text)
        )
    else:
        raise ValueError(f"invalid timecode: {text!r}")
    if value < 0:
        raise ValueError(f"negative time: {text!r}")
    return value


def format(seconds: float, tenths: bool = True) -> str:
    """Format seconds as ``HH:MM:SS.t`` (or ``HH:MM:SS`` when ``tenths`` is false)."""
    if seconds < 0:
        raise ValueError(f"negative time: {seconds}")
    if tenths:
        total_tenths = round(seconds * 10)
        whole, tenth = divmod(total_tenths, 10)
        hours, rem = divmod(whole, 3600)
        minutes, secs = divmod(rem, 60)
        return f"{hours:02d}:{minutes:02d}:{secs:02d}.{tenth}"
    whole = round(seconds)
    hours, rem = divmod(whole, 3600)
    minutes, secs = divmod(rem, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


def _nonneg_int(part: str, text: str) -> int:
    if not part.isdigit():
        raise ValueError(f"invalid timecode: {text!r}")
    return int(part)


def _nonneg_float(part: str, text: str) -> float:
    try:
        value = float(part)
    except ValueError:
        raise ValueError(f"invalid timecode: {text!r}") from None
    if value < 0:
        raise ValueError(f"negative time: {text!r}")
    return value
