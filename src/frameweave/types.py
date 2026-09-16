"""Shared types: the records every stage reads or writes, and the Source protocol.

Every dataclass round-trips through ``to_dict`` / ``from_dict`` so artifacts are plain JSON.
Times are seconds as floats, kept to the tenth when they come from the frame planner.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, fields, is_dataclass
from pathlib import Path
from typing import Any, ClassVar, Literal, Protocol, Self, runtime_checkable

FrameKind = Literal["primary", "extra"]
Completion = Literal["complete", "complete-with-warnings", "incomplete"]
StageStatus = Literal["done", "reused", "failed", "skipped"]

FORMAT_VERSION = 1
TRANSCRIPT_NAME = "transcript.fwv"


class _Record:
    """Mixin: JSON-friendly dict conversion for dataclasses, nested records included."""

    _nested: ClassVar[dict[str, type]] = {}

    def to_dict(self) -> dict[str, Any]:
        return _plain(asdict(self))  # type: ignore[call-overload]

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Self:
        kwargs: dict[str, Any] = {}
        names = {f.name for f in fields(cls)}  # type: ignore[arg-type]
        for key, value in data.items():
            if key not in names:
                continue  # additive rule: unknown keys are ignorable
            nested = cls._nested.get(key)
            if nested is not None and value is not None:
                if isinstance(value, list):
                    value = [nested.from_dict(v) for v in value]
                else:
                    value = nested.from_dict(value)
            kwargs[key] = value
        return cls(**kwargs)


def _plain(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _plain(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_plain(v) for v in value]
    if isinstance(value, Path):
        return str(value)
    return value


@dataclass(frozen=True)
class Word(_Record):
    """One aligned word from speech-to-text."""

    start: float
    end: float
    text: str
    score: float | None = None


@dataclass(frozen=True)
class Segment(_Record):
    """One spoken span. ``source`` is the transcript source tag, e.g. ``captions-auto``."""

    _nested = {"words": Word}

    start: float
    end: float
    text: str
    source: str
    speaker: str | None = None
    words: list[Word] | None = None
    quality: float | None = None

    def contains(self, time: float) -> bool:
        """The association rule: a frame belongs to the segment whose range contains its time."""
        return self.start <= time < self.end or time == self.start == self.end


@dataclass(frozen=True)
class Frame(_Record):
    """One extracted frame. ``time`` is kept to the tenth; ``path`` is run-folder relative."""

    id: str
    time: float
    kind: FrameKind
    path: str
    segment_index: int | None = None

    def __post_init__(self) -> None:
        if not (self.id.startswith("f") and self.id[1:].isdigit() and len(self.id) == 5):
            raise ValueError(f"frame id must be f followed by four digits, got {self.id!r}")
        object.__setattr__(self, "time", round(self.time, 1))


@dataclass(frozen=True)
class Description(_Record):
    """What a vision lane said about one frame. ``strings`` are model-read, verbatim."""

    frame_id: str
    source: str
    summary: str
    strings: list[str] = field(default_factory=list)
    illegible: bool = False


@dataclass(frozen=True)
class Usage(_Record):
    """Tokens, time, and money for one request or one stage. Dollars are list-price arithmetic."""

    calls: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    tokens_reasoning: int = 0
    seconds: float = 0.0
    usd: float = 0.0
    unknown_usd: bool = False

    def __add__(self, other: Usage) -> Usage:
        return Usage(
            calls=self.calls + other.calls,
            tokens_in=self.tokens_in + other.tokens_in,
            tokens_out=self.tokens_out + other.tokens_out,
            tokens_reasoning=self.tokens_reasoning + other.tokens_reasoning,
            seconds=self.seconds + other.seconds,
            usd=self.usd + other.usd,
            unknown_usd=self.unknown_usd or other.unknown_usd,
        )


@dataclass(frozen=True)
class LedgerEntry(_Record):
    """One request, appended before the stage is marked done."""

    _nested = {"usage": Usage}

    stage: str
    provider: str
    model: str
    started: str
    ended: str
    usage: Usage
    status: Literal["ok", "retry", "failed", "timeout"] = "ok"
    note: str | None = None


@dataclass(frozen=True)
class StageResult(_Record):
    """What a stage returned: its artifact and whether it was reused from cache."""

    _nested = {"usage": Usage}

    stage: str
    status: StageStatus
    artifact: str | None = None
    usage: Usage = field(default_factory=Usage)
    warnings: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class Chapter(_Record):
    start: float
    title: str


@dataclass(frozen=True)
class Resolved(_Record):
    """Facts about an input before anything is downloaded."""

    _nested = {"chapters": Chapter}

    video_id: str
    title: str
    channel: str
    source: str
    duration: float
    description: str = ""
    links: list[str] = field(default_factory=list)
    chapters: list[Chapter] = field(default_factory=list)
    has_captions: bool | None = None
    published: str | None = None


@runtime_checkable
class Source(Protocol):
    """A place a video comes from. Implementations live in ``sources/``; this is the contract.

    ``fetch_media`` and ``fetch_captions`` are independent: a captions failure never fails media.
    """

    name: str

    def matches(self, raw_input: str) -> bool: ...

    def resolve(self, raw_input: str) -> Resolved: ...

    def fetch_media(self, resolved: Resolved, dest_dir: Path) -> Path: ...

    def fetch_captions(self, resolved: Resolved) -> list[Segment] | None: ...


def is_record(value: object) -> bool:
    return is_dataclass(value) and isinstance(value, _Record)
