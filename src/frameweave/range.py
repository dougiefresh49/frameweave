"""Range and chapter selection for a run.

``parse`` turns ``--start/--end``, ``--chapter``, and a URL ``t=`` into a
``RangeSpec``. Precedence: explicit start/end beat chapter, both beat ``t=``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal
from urllib.parse import parse_qs, urlparse

from frameweave.config import FlagSpec, slugify
from frameweave.types import Resolved
from frameweave.util import timecode

SourceKind = Literal["flags", "chapter", "url", "full"]

_YT_COMPOUND = re.compile(
    r"^(?:(?P<h>\d+)h)?(?:(?P<m>\d+)m)?(?:(?P<s>\d+(?:\.\d+)?)s)?$",
    re.IGNORECASE,
)
_WS = re.compile(r"\s+")


class RangeError(ValueError):
    """Invalid or ambiguous range. Message includes the values or candidates."""


@dataclass(frozen=True)
class RangeSpec:
    start: float
    end: float
    label: str
    slug: str
    source: SourceKind

    @property
    def header(self) -> str:
        """Value for the transcript ``range:`` header line."""
        if self.source == "chapter":
            return f"chapter: {self.label}"
        return self.label


def parse(
    start: str | None,
    end: str | None,
    chapter: str | None,
    url_t: float | None,
    resolved: Resolved,
    *,
    defer_bounds: bool = False,
) -> RangeSpec:
    """Build a ``RangeSpec`` from flags, chapter text, or a URL timestamp.

    When ``defer_bounds`` is true (duration not yet known), skip checks that
    need a real duration; the caller must re-parse after duration is final.
    """
    duration = float(resolved.duration)
    if start is not None or end is not None:
        return _from_flags(start, end, duration, defer_bounds=defer_bounds)
    if chapter is not None and chapter.strip() != "":
        return _from_chapter(chapter, resolved, duration, defer_bounds=defer_bounds)
    if url_t is not None:
        return _from_url_t(url_t, duration, defer_bounds=defer_bounds)
    return RangeSpec(start=0.0, end=duration, label="full", slug="", source="full")


def parse_url_t(raw: str | None) -> float | None:
    """Parse a YouTube ``t`` / ``start`` query value (``90``, ``1m30s``, …)."""
    if raw is None:
        return None
    text = raw.strip()
    if not text:
        return None
    return _parse_t_value(text)


def url_t_from(raw_input: str) -> float | None:
    """Extract and parse ``t`` or ``start`` from a URL, or return ``None``."""
    try:
        parsed = urlparse(raw_input.strip())
    except ValueError:
        return None
    qs = parse_qs(parsed.query)
    values = qs.get("t") or qs.get("start")
    if not values:
        # YouTube also accepts ``#t=90`` / ``&t=1m30s`` in the fragment.
        frag = parsed.fragment or ""
        if frag.startswith("t="):
            values = [frag[2:]]
        else:
            return None
    return parse_url_t(values[0])


def overlaps(seg_start: float, seg_end: float, window_start: float, window_end: float) -> bool:
    """True when ``[seg_start, seg_end)`` overlaps ``[window_start, window_end)``."""
    return seg_end > window_start and seg_start < window_end


def cli_flags() -> list[FlagSpec]:
    return [
        FlagSpec("--start", "start", str, "Range start: HH:MM:SS, MM:SS, or seconds."),
        FlagSpec("--end", "end", str, "Range end: HH:MM:SS, MM:SS, or seconds."),
        FlagSpec(
            "--chapter",
            "chapter",
            str,
            "Select a chapter by title prefix (case-insensitive).",
        ),
    ]


def preflight_checks(config: object) -> list:
    del config
    return []


def _from_flags(
    start: str | None,
    end: str | None,
    duration: float,
    *,
    defer_bounds: bool = False,
) -> RangeSpec:
    start_s = timecode.parse(start) if start is not None and start.strip() != "" else 0.0
    end_s = timecode.parse(end) if end is not None and end.strip() != "" else duration
    _validate_bounds(start_s, end_s, duration, defer_bounds=defer_bounds)
    label = f"{_clock(start_s)}-{_clock(end_s)}"
    slug = f"{_clock_slug(start_s)}_{_clock_slug(end_s)}"
    return RangeSpec(start=start_s, end=end_s, label=label, slug=slug, source="flags")


def _from_url_t(
    url_t: float, duration: float, *, defer_bounds: bool = False
) -> RangeSpec:
    start_s = float(url_t)
    end_s = duration
    _validate_bounds(start_s, end_s, duration, defer_bounds=defer_bounds)
    label = f"{_clock(start_s)}-{_clock(end_s)}"
    slug = f"{_clock_slug(start_s)}_{_clock_slug(end_s)}"
    return RangeSpec(start=start_s, end=end_s, label=label, slug=slug, source="url")


def _from_chapter(
    chapter: str,
    resolved: Resolved,
    duration: float,
    *,
    defer_bounds: bool = False,
) -> RangeSpec:
    needle = _collapse(chapter).lower()
    chapters = list(resolved.chapters)
    if not chapters:
        raise RangeError(
            f"no chapters on this video; cannot match --chapter {chapter!r}"
        )
    matches = [
        (index, ch)
        for index, ch in enumerate(chapters)
        if _collapse(ch.title).lower().startswith(needle)
    ]
    if not matches:
        listing = "; ".join(f"{_clock(ch.start)} {ch.title}" for ch in chapters)
        raise RangeError(
            f"no chapter matching {chapter!r}; chapters: {listing}"
        )
    if len(matches) > 1:
        listing = "; ".join(f"{_clock(ch.start)} {ch.title}" for _, ch in matches)
        raise RangeError(
            f"ambiguous --chapter {chapter!r}; candidates: {listing}"
        )
    index, ch = matches[0]
    start_s = float(ch.start)
    if index + 1 < len(chapters):
        end_s = float(chapters[index + 1].start)
    else:
        end_s = duration
    _validate_bounds(start_s, end_s, duration, defer_bounds=defer_bounds)
    return RangeSpec(
        start=start_s,
        end=end_s,
        label=ch.title,
        slug=slugify(ch.title),
        source="chapter",
    )


def _validate_bounds(
    start: float, end: float, duration: float, *, defer_bounds: bool = False
) -> None:
    if start < 0:
        raise RangeError(f"start < 0: start={start}")
    if defer_bounds:
        # Duration unknown: still reject an explicit start >= end when end is set.
        if end > 0 and start >= end:
            raise RangeError(
                f"start >= end: start={_clock(start)} ({start}), end={_clock(end)} ({end})"
            )
        return
    if start >= end:
        raise RangeError(
            f"start >= end: start={_clock(start)} ({start}), end={_clock(end)} ({end})"
        )
    if end > duration:
        raise RangeError(
            f"end > duration: end={_clock(end)} ({end}), "
            f"duration={_clock(duration)} ({duration})"
        )


def _parse_t_value(text: str) -> float:
    bare = text
    match = _YT_COMPOUND.fullmatch(bare)
    if match and any(match.group(name) for name in ("h", "m", "s")):
        hours = int(match.group("h") or 0)
        minutes = int(match.group("m") or 0)
        seconds = float(match.group("s") or 0)
        return hours * 3600 + minutes * 60 + seconds
    try:
        return timecode.parse(bare)
    except ValueError as exc:
        raise RangeError(f"invalid URL t= value: {text!r}") from exc


def _clock(seconds: float) -> str:
    return timecode.format(seconds, tenths=False)


def _clock_slug(seconds: float) -> str:
    return _clock(seconds).replace(":", "-")


def _collapse(text: str) -> str:
    return _WS.sub(" ", text.strip())
