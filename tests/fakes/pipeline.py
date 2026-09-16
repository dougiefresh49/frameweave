"""Offline fakes for the pipeline: source, STT, and vision that count calls."""

from __future__ import annotations

import hashlib
import json
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from frameweave.stt.base import SttResult
from frameweave.types import Description, Frame, Resolved, Segment, Usage
from tests.make_synthetic import make as make_synthetic

_SYNTHETIC_SECONDS = 20


@dataclass
class FakeSource:
    """Resolves a synthetic 20 s video; no captions. Counts resolve/fetch calls."""

    video: Path | None = None
    name: str = "fake"
    resolve_calls: int = 0
    fetch_calls: int = 0
    _video_id: str = "synthetic20"

    def ensure_video(self, dest: Path) -> Path:
        if self.video is not None and self.video.is_file():
            self._video_id = hashlib.sha256(self.video.read_bytes()).hexdigest()
            return self.video
        path = dest / "synthetic.mp4"
        make_synthetic(path, _SYNTHETIC_SECONDS)
        self.video = path
        self._video_id = hashlib.sha256(path.read_bytes()).hexdigest()
        return path

    def matches(self, raw_input: str) -> bool:
        del raw_input
        return True

    def resolve(self, raw_input: str) -> Resolved:
        self.resolve_calls += 1
        candidate = Path(raw_input)
        if candidate.is_file():
            video = candidate
        else:
            parent = candidate.parent if candidate.parent != candidate else Path.cwd()
            video = self.ensure_video(parent)
        self.video = video
        self._video_id = hashlib.sha256(video.read_bytes()).hexdigest()
        return Resolved(
            video_id=self._video_id,
            title="Synthetic twenty",
            channel="Test Channel",
            source=str(video.resolve()),
            duration=float(_SYNTHETIC_SECONDS),
            has_captions=False,
        )

    def fetch_media(self, resolved: Resolved, dest_dir: Path) -> Path:
        self.fetch_calls += 1
        dest_dir.mkdir(parents=True, exist_ok=True)
        src = Path(resolved.source)
        dest = dest_dir / "media.mp4"
        if not dest.is_file():
            # Mux silent audio so STT extract/chunk always has a stream (testsrc is
            # video-only). Real sources already carry audio.
            import subprocess

            proc = subprocess.run(
                [
                    "ffmpeg",
                    "-nostdin",
                    "-y",
                    "-i",
                    str(src),
                    "-f",
                    "lavfi",
                    "-i",
                    "anullsrc=r=16000:cl=mono",
                    "-shortest",
                    "-c:v",
                    "copy",
                    "-c:a",
                    "aac",
                    str(dest),
                ],
                capture_output=True,
            )
            if proc.returncode != 0:
                shutil.copyfile(src, dest)
        meta = dest_dir / "media.json"
        digest = hashlib.sha256(dest.read_bytes()).hexdigest()
        meta.write_text(
            json.dumps({"sha256": digest, "bytes": dest.stat().st_size}) + "\n",
            encoding="utf-8",
        )
        return dest

    def fetch_captions(self, resolved: Resolved) -> list[Segment] | None:
        del resolved
        return None


@dataclass
class FakeStt:
    """Scripted STT backend. Counts ``transcribe`` calls."""

    segments: list[Segment] = field(default_factory=list)
    name: str = "stt-fake"
    calls: int = 0
    paths: list[Path] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.segments:
            self.segments = [
                Segment(0.0, 5.0, "hello from the synthetic clip", self.name, id="s0001"),
                Segment(5.0, 12.0, "second line of speech for context", self.name, id="s0002"),
            ]

    def transcribe(self, audio: Path, config: Any) -> SttResult:
        self.paths.append(Path(audio))
        del config
        self.calls += 1
        return SttResult(
            segments=list(self.segments),
            source=self.name,
            usage=Usage(calls=1, seconds=0.01, usd=0.0),
            audio_seconds=20.0,
        )


@dataclass
class FakeVision:
    """Scripted vision backend. Counts calls and records context strings."""

    name: str = "fake-vision"
    model: str = "fake-model"
    calls: int = 0
    contexts: list[str] = field(default_factory=list)
    fail_after: int | None = None
    usd_per_call: float = 0.01

    def describe(
        self,
        batch: list[Frame],
        context: str,
        frames_dir: Path,
        config: Any,
    ) -> tuple[list[Description], Usage]:
        del frames_dir, config
        self.contexts.append(context)
        self.calls += 1
        if self.fail_after is not None and self.calls > self.fail_after:
            raise RuntimeError("fake vision failed after first batch")
        descriptions = [
            Description(
                frame_id=frame.id,
                source=f"{self.name}:{self.model}",
                summary=f"frame at {frame.time}",
                strings=[f"t={frame.time}"],
            )
            for frame in batch
        ]
        usage = Usage(
            calls=1,
            tokens_in=10 * len(batch),
            tokens_out=5 * len(batch),
            seconds=0.05,
            usd=self.usd_per_call,
        )
        return descriptions, usage
