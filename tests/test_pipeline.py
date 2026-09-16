"""Pipeline registry: reuse, redo, frames-only, ledger timing, cleanup."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
import types
from dataclasses import replace
from pathlib import Path

import pytest

from frameweave.config import ConfigError, load
from frameweave.ledger import DELETERS, reconcile
from frameweave.pipeline import (
    STAGES,
    cli_flags,
    preflight_checks,
    run,
)
from frameweave.types import Chapter, Resolved, Segment
from tests.fakes.pipeline import FakeSource, FakeStt, FakeVision
from tests.make_synthetic import make as make_synthetic

MISSING_TOML = Path("/nonexistent/frameweave-test/config.toml")


def _child_vision_env(tmp_path: Path) -> dict[str, str]:
    """Env for a spawned child where claude/codex vision-lane presence passes.

    A child subprocess is a fresh interpreter: this test's monkeypatched
    ``shutil.which``/``os.environ`` (the conftest presence fixture) never
    reaches it, so on a runner with no real `claude`/`codex` CLI the child's
    own presence check fails. Give it stub executables on `PATH` (prepended,
    so real tools like ffmpeg/yt-dlp still resolve) and a `GEMINI_API_KEY`.
    """
    bin_dir = tmp_path / "stub-bin"
    bin_dir.mkdir(exist_ok=True)
    for name in ("claude", "codex"):
        stub = bin_dir / name
        stub.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        stub.chmod(0o755)
    env = dict(os.environ)
    env["PATH"] = f"{bin_dir}{os.pathsep}{env.get('PATH', '')}"
    env["GEMINI_API_KEY"] = "test-key"
    return env


def _cfg(tmp_path: Path, **flags: object):
    out = tmp_path / "out"
    cache = tmp_path / "cache"
    out.mkdir(exist_ok=True)
    cache.mkdir(exist_ok=True)
    base = {
        "out": out,
        "cache_dir": cache,
        "vision_lane": "claude",
        "frames_per_call": 2,
        "frame_interval_s": 5.0,
        "captions_mode": "none",
    }
    base.update(flags)
    return load(flags=base, env={}, toml_path=MISSING_TOML, dotenv_paths=[])


def _video(tmp_path: Path) -> Path:
    path = tmp_path / "clip.mp4"
    make_synthetic(path, 20)
    return path


def test_cli_flags_and_preflight(tmp_path: Path) -> None:
    names = {flag.name for flag in cli_flags()}
    assert "--redo" in names
    assert "--frames-only" in names
    checks = preflight_checks(_cfg(tmp_path))
    assert checks and checks[0].ok


def test_full_run_and_identical_rerun(tmp_path: Path) -> None:
    """Second run: zero STT/vision/fetch calls. Resolve runs every time by design."""
    video = _video(tmp_path)
    cfg = _cfg(tmp_path)
    source = FakeSource(video=video)
    stt = FakeStt()
    vision = FakeVision()
    lines: list[str] = []

    outcome = run(
        str(video),
        cfg,
        progress=lines.append,
        source=source,
        stt=stt,
        vision=vision,
    )
    out = outcome.output_path
    assert out.is_dir()
    for name in ("transcript.fwv", "meta.json", "cost.json", "README.md"):
        assert (out / name).is_file(), name
    assert (out / "frames").is_dir()
    assert any((out / "frames").glob("*.jpg"))
    assert list((cfg.cache_dir / "runs").rglob("ledger.jsonl"))

    stt_calls = stt.calls
    vision_calls = vision.calls
    fetch_calls = source.fetch_calls
    resolve_calls = source.resolve_calls
    assert stt_calls >= 1
    assert vision_calls >= 1
    assert any(line.startswith("stage ") for line in lines)

    outcome2 = run(
        str(video),
        cfg,
        source=source,
        stt=stt,
        vision=vision,
    )
    assert outcome2.output_path == out
    assert stt.calls == stt_calls
    assert vision.calls == vision_calls
    assert source.fetch_calls == fetch_calls
    # resolve is called every run by design (picks video_id / run key path)
    assert source.resolve_calls > resolve_calls


def test_frame_interval_reruns_frames_describe_assemble(tmp_path: Path) -> None:
    video = _video(tmp_path)
    cfg = _cfg(tmp_path, frame_interval_s=5.0)
    source = FakeSource(video=video)
    stt = FakeStt()
    vision = FakeVision()
    run(str(video), cfg, source=source, stt=stt, vision=vision)
    stt_after = stt.calls
    vision_after = vision.calls

    cfg2 = load(
        flags={
            "out": cfg.out,
            "cache_dir": cfg.cache_dir,
            "vision_lane": "claude",
            "frames_per_call": 2,
            "frame_interval_s": 3.0,
            "captions_mode": "none",
        },
        env={},
        toml_path=MISSING_TOML,
        dotenv_paths=[],
    )
    run(str(video), cfg2, source=source, stt=stt, vision=vision)
    assert stt.calls == stt_after
    assert vision.calls > vision_after


def test_stt_model_change_reruns_transcript(tmp_path: Path) -> None:
    """Only stt_model changes → sibling transcript must not be reused; STT runs again."""
    video = _video(tmp_path)
    cfg = _cfg(tmp_path, stt_model="large-v3-turbo")
    source = FakeSource(video=video)
    stt = FakeStt()
    vision = FakeVision()
    run(str(video), cfg, source=source, stt=stt, vision=vision)
    stt_after = stt.calls

    cfg2 = load(
        flags={
            "out": cfg.out,
            "cache_dir": cfg.cache_dir,
            "vision_lane": "claude",
            "frames_per_call": 2,
            "frame_interval_s": 5.0,
            "captions_mode": "none",
            "stt_model": "large-v3",
        },
        env={},
        toml_path=MISSING_TOML,
        dotenv_paths=[],
    )
    run(str(video), cfg2, source=source, stt=stt, vision=vision)
    assert stt.calls > stt_after


def test_redo_describe(tmp_path: Path) -> None:
    video = _video(tmp_path)
    cfg = _cfg(tmp_path)
    source = FakeSource(video=video)
    stt = FakeStt()
    vision = FakeVision()
    run(str(video), cfg, source=source, stt=stt, vision=vision)
    stt_n = stt.calls
    vision_n = vision.calls
    run(str(video), cfg, redo=("describe",), source=source, stt=stt, vision=vision)
    assert stt.calls == stt_n
    assert vision.calls > vision_n


def test_redo_describe_does_not_double_count_dollars(tmp_path: Path) -> None:
    video = _video(tmp_path)
    cfg = _cfg(tmp_path)
    source = FakeSource(video=video)
    stt = FakeStt()
    vision = FakeVision(usd_per_call=0.01)
    first = run(str(video), cfg, source=source, stt=stt, vision=vision)
    vision_before_redo = vision.calls
    second = run(
        str(video), cfg, redo=("describe",), source=source, stt=stt, vision=vision
    )
    describe_lines = 0
    for path in (cfg.cache_dir / "runs").rglob("ledger.jsonl"):
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            if json.loads(line).get("stage") == "describe":
                describe_lines += 1
    assert describe_lines == vision.calls - vision_before_redo
    assert second.cost_usd == pytest.approx(first.cost_usd)


def test_frames_only(tmp_path: Path) -> None:
    video = _video(tmp_path)
    cfg = _cfg(tmp_path)
    source = FakeSource(video=video)
    stt = FakeStt()
    vision = FakeVision()
    outcome = run(
        str(video),
        cfg,
        frames_only=True,
        source=source,
        stt=stt,
        vision=vision,
    )
    assert stt.calls == 0
    assert vision.calls == 0
    assert outcome.completion == "complete (speech not requested)"
    assert (outcome.output_path / "transcript.fwv").is_file()
    assert (outcome.output_path / "frames").is_dir()


def test_vision_context_window(tmp_path: Path) -> None:
    """frames_per_call=1: second batch gets the second segment, not the first."""
    video = _video(tmp_path)
    cfg = _cfg(tmp_path, frames_per_call=1, frame_interval_s=5.0)
    source = FakeSource(video=video)
    stt = FakeStt(
        segments=[
            Segment(0.0, 4.0, "alpha window text", "stt-fake", id="s0001"),
            Segment(8.0, 12.0, "beta window text", "stt-fake", id="s0002"),
        ]
    )
    vision = FakeVision()
    run(str(video), cfg, source=source, stt=stt, vision=vision)
    assert len(vision.contexts) >= 2
    second = vision.contexts[1]
    assert "beta window text" in second
    assert "alpha window text" not in second


def test_injected_stt_still_extracts_and_chunks(tmp_path: Path) -> None:
    video = _video(tmp_path)
    cfg = _cfg(tmp_path)
    source = FakeSource(video=video)
    stt = FakeStt()
    vision = FakeVision()
    run(str(video), cfg, source=source, stt=stt, vision=vision)
    assert stt.paths
    assert all(path.suffix == ".wav" for path in stt.paths)
    run_dirs = list((cfg.cache_dir / "runs").rglob("transcript.json"))
    assert run_dirs
    run_dir = run_dirs[0].parent
    assert (run_dir / "audio.wav").is_file() or list((run_dir / "chunks").glob("*.wav"))


def test_http_duration_refreshed_after_fetch(tmp_path: Path) -> None:
    video = _video(tmp_path)
    cfg = _cfg(tmp_path)

    class HttpishSource(FakeSource):
        def resolve(self, raw_input: str) -> Resolved:
            resolved = super().resolve(raw_input)
            return replace(resolved, duration=0.0)

        def resolved_after_fetch(self, dest_dir: Path) -> Resolved:
            del dest_dir
            assert self.video is not None
            return Resolved(
                video_id=self._video_id,
                title="Synthetic twenty",
                channel="Test Channel",
                source=str(self.video.resolve()),
                duration=20.0,
                has_captions=False,
            )

    source = HttpishSource(video=video)
    stt = FakeStt()
    vision = FakeVision()
    run(str(video), cfg, source=source, stt=stt, vision=vision)
    resolved_path = next((cfg.cache_dir / "sources").rglob("resolved.json"))
    data = json.loads(resolved_path.read_text(encoding="utf-8"))
    assert data["duration"] == 20.0
    frames_path = next((cfg.cache_dir / "runs").rglob("frames.json"))
    frames = json.loads(frames_path.read_text(encoding="utf-8"))["frames"]
    assert frames, "duration 0 would plan zero frames"


def test_command_line_uses_redacted_source(tmp_path: Path) -> None:
    video = _video(tmp_path)
    cfg = _cfg(tmp_path)
    secret = "https://cdn.example/vid.mp4?token=super-secret"
    redacted = "https://cdn.example/vid.mp4?token=***"

    class RedactingSource(FakeSource):
        def resolve(self, raw_input: str) -> Resolved:
            del raw_input
            resolved = super().resolve(str(video))
            return replace(resolved, source=redacted)

        def fetch_media(self, resolved: Resolved, dest_dir: Path):
            assert self.video is not None
            return super().fetch_media(
                replace(resolved, source=str(self.video.resolve())),
                dest_dir,
            )

    source = RedactingSource(video=video)
    outcome = run(
        secret,
        cfg,
        source=source,
        stt=FakeStt(),
        vision=FakeVision(),
    )
    meta = json.loads((outcome.output_path / "meta.json").read_text(encoding="utf-8"))
    assert "super-secret" not in meta["command_line"]
    assert redacted in meta["command_line"]


def test_assemble_not_reused_when_output_missing(tmp_path: Path) -> None:
    video = _video(tmp_path)
    cfg = _cfg(tmp_path)
    source = FakeSource(video=video)
    stt = FakeStt()
    vision = FakeVision()
    first = run(str(video), cfg, source=source, stt=stt, vision=vision)
    import shutil

    shutil.rmtree(first.output_path)
    second = run(str(video), cfg, source=source, stt=stt, vision=vision)
    assert second.output_path.is_dir()
    assert (second.output_path / "transcript.fwv").is_file()


def test_require_out_before_stage_1(tmp_path: Path) -> None:
    video = _video(tmp_path)
    cfg = _cfg(tmp_path)
    cfg = replace(cfg, out=None)
    source = FakeSource(video=video)
    vision = FakeVision()
    with pytest.raises(ConfigError, match="FRAMEWEAVE_OUT"):
        run(str(video), cfg, source=source, stt=FakeStt(), vision=vision)
    assert vision.calls == 0


def test_out_override_uses_exact_path(tmp_path: Path) -> None:
    """--out / out_override is the folder itself, not channel/title under it."""
    video = _video(tmp_path)
    cfg = replace(_cfg(tmp_path), out=None)
    exact = tmp_path / "exact-out"
    source = FakeSource(video=video)
    outcome = run(
        str(video),
        cfg,
        source=source,
        stt=FakeStt(),
        vision=FakeVision(),
        out_override=exact,
    )
    assert outcome.output_path == exact.resolve()
    assert (exact / "transcript.fwv").is_file()


def test_resolved_kwarg_skips_second_resolve(tmp_path: Path) -> None:
    """Caller-supplied resolved facts are used; source.resolve is not called again."""
    video = _video(tmp_path)
    cfg = _cfg(tmp_path)
    source = FakeSource(video=video)
    resolved = source.resolve(str(video))
    assert source.resolve_calls == 1
    outcome = run(
        str(video),
        cfg,
        source=source,
        stt=FakeStt(),
        vision=FakeVision(),
        resolved=resolved,
    )
    assert source.resolve_calls == 1
    assert outcome.output_path.is_dir()


def test_speakers_unavailable_before_stage_1(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    video = _video(tmp_path)
    cfg = _cfg(tmp_path, speakers=True)

    class SpeakersUnavailable(RuntimeError):
        pass

    def make_diarizer(_config: object) -> None:
        raise SpeakersUnavailable("HF_TOKEN missing")

    fake = types.ModuleType("frameweave.stt.speakers")
    fake.SpeakersUnavailable = SpeakersUnavailable  # type: ignore[attr-defined]
    fake.make_diarizer = make_diarizer  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "frameweave.stt.speakers", fake)

    source = FakeSource(video=video)
    vision = FakeVision()
    with pytest.raises(SpeakersUnavailable):
        run(str(video), cfg, source=source, vision=vision)
    assert vision.calls == 0
    assert source.fetch_calls == 0


def test_pipeline_two_chunk_stt_presentation_keeps_boundary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Finding 5: two-chunk STT path yields presentation spans; no drop at boundary."""
    from dataclasses import dataclass, field

    from frameweave.stt.audio import Chunk
    from frameweave.stt.base import (
        SttResult,
        clamp_to_words,
        merge_chunks,
        merge_into_presentation,
    )
    from frameweave.types import Usage

    video = _video(tmp_path)
    cfg = _cfg(tmp_path, vision_lane="none")

    def fake_chunk(audio: Path, dest_dir: Path, **kwargs: object) -> list[Chunk]:
        del kwargs
        return [
            Chunk(audio, offset_s=0.0, duration_s=10.0),
            Chunk(audio, offset_s=8.0, duration_s=12.0),
        ]

    monkeypatch.setattr("frameweave.pipeline.stt_audio.chunk", fake_chunk)

    @dataclass
    class TwoChunkFake:
        name: str = "stt-fake"
        calls: int = 0
        paths: list[Path] = field(default_factory=list)

        def transcribe(self, audio: Path, config: object) -> SttResult:
            del config
            self.paths.append(Path(audio))
            self.calls += 1
            if self.calls == 1:
                segs = [
                    Segment(float(i * 4), float(i * 4 + 4), "keep going", self.name)
                    for i in range(2)
                ] + [Segment(8.0, 9.5, "end of chunk one.", self.name)]
            else:
                segs = [
                    Segment(0.5, 1.0, "overlap dup", self.name),
                    Segment(1.5, 5.5, "kept after boundary", self.name),
                    Segment(5.5, 9.5, "more kept text", self.name),
                    Segment(9.5, 13.5, "still going", self.name),
                    Segment(13.5, 17.5, "Second chunk close.", self.name),
                ]
            return SttResult(
                segments=segs,
                source=self.name,
                usage=Usage(calls=1, seconds=0.01),
                audio_seconds=12.0,
            )

        def merge_and_diarize(
            self, audio: Path, chunk_results: list[tuple[Chunk, SttResult]]
        ) -> list[Segment]:
            del audio
            return merge_into_presentation(clamp_to_words(merge_chunks(chunk_results)))

    source = FakeSource(video=video)
    stt = TwoChunkFake()
    outcome = run(str(video), cfg, source=source, stt=stt, vision=FakeVision())
    assert stt.calls == 2

    transcript = json.loads(
        next((cfg.cache_dir / "runs").rglob("transcript.json")).read_text(encoding="utf-8")
    )
    texts = " ".join(item["text"] for item in transcript["segments"])
    assert "kept after boundary" in texts
    assert "Second chunk close." in texts
    assert "overlap dup" not in texts
    assert transcript["segments"], "expected presentation spans"
    # Presentation merge of ~17 s of unique speech → one span under 40 s.
    for item in transcript["segments"]:
        assert item["end"] - item["start"] <= 40.0
    assert (outcome.output_path / "transcript.fwv").is_file()


def test_pipeline_speakers_labels_via_merge_and_diarize(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Finding 5: --speakers on the pipeline path produces S1/S2 with a fake diarizer."""
    from dataclasses import replace

    from frameweave.stt.audio import Chunk
    from frameweave.stt.base import SttResult
    from frameweave.stt.local import WhisperXBackend
    from frameweave.types import Usage

    video = _video(tmp_path)
    cfg = _cfg(tmp_path, speakers=True, vision_lane="none")

    def fake_diarize(audio: Path, segments: list[Segment]) -> list[Segment]:
        del audio
        return [
            replace(seg, speaker="S1" if seg.start < 8.0 else "S2") for seg in segments
        ]

    backend = WhisperXBackend(diarize=fake_diarize)
    scripted = [
        Segment(0.0, 4.0, "speaker one talking now", "stt-whisperx"),
        Segment(4.0, 8.0, "still speaker one here.", "stt-whisperx"),
        Segment(8.0, 12.0, "speaker two takes over", "stt-whisperx"),
        Segment(12.0, 16.0, "and finishes the thought.", "stt-whisperx"),
    ]

    def fake_transcribe(self: WhisperXBackend, audio: Path, config: object) -> SttResult:
        del self, config
        return SttResult(
            segments=list(scripted),
            source="stt-whisperx",
            usage=Usage(calls=1, seconds=0.01),
            audio_seconds=20.0,
        )

    monkeypatch.setattr(WhisperXBackend, "transcribe", fake_transcribe)

    # Avoid real chunking variance: one chunk covering the clip.
    def fake_chunk(audio: Path, dest_dir: Path, **kwargs: object) -> list[Chunk]:
        del dest_dir, kwargs
        return [Chunk(audio, offset_s=0.0, duration_s=20.0)]

    monkeypatch.setattr("frameweave.pipeline.stt_audio.chunk", fake_chunk)

    source = FakeSource(video=video)
    outcome = run(str(video), cfg, source=source, stt=backend, vision=FakeVision())
    transcript = json.loads(
        next((cfg.cache_dir / "runs").rglob("transcript.json")).read_text(encoding="utf-8")
    )
    speakers = {item.get("speaker") for item in transcript["segments"]}
    assert speakers == {"S1", "S2"}
    fwv = (outcome.output_path / "transcript.fwv").read_text(encoding="utf-8")
    assert "S1:" in fwv
    assert "S2:" in fwv

def test_ledger_persists_before_descriptions_on_failure(tmp_path: Path) -> None:
    video = _video(tmp_path)
    cfg = _cfg(tmp_path, frames_per_call=1, frame_interval_s=2.0)
    source = FakeSource(video=video)
    stt = FakeStt()
    vision = FakeVision(fail_after=1)
    with pytest.raises(RuntimeError, match="fake vision failed"):
        run(str(video), cfg, source=source, stt=stt, vision=vision)
    run_dirs = list((cfg.cache_dir / "runs").rglob("ledger.jsonl"))
    assert run_dirs
    ledger_path = run_dirs[0]
    run_dir = ledger_path.parent
    assert ledger_path.is_file()
    assert ledger_path.stat().st_size > 0
    assert not (run_dir / "descriptions.json").is_file()


def test_sigkill_leaves_obligations_for_reconcile(tmp_path: Path) -> None:
    run_dir = tmp_path / "runs" / "vid" / "rk"
    run_dir.mkdir(parents=True)
    code = textwrap.dedent(
        f"""
        import os, signal, sys
        from pathlib import Path
        sys.path.insert(0, {str(Path.cwd() / "src")!r})
        from frameweave.ledger import Ledger
        led = Ledger(Path({str(run_dir)!r}))
        led.register("fake", "kill-1")
        led.register("fake", "kill-2")
        os.kill(os.getpid(), signal.SIGKILL)
        """
    )
    proc = subprocess.run([sys.executable, "-c", code], check=False)
    assert proc.returncode != 0
    obligations = json.loads((run_dir / "obligations.json").read_text())
    assert {item["id"] for item in obligations} == {"kill-1", "kill-2"}

    deleted: list[str] = []
    DELETERS["fake"] = deleted.append
    try:
        assert reconcile(tmp_path) == 2
        assert sorted(deleted) == ["kill-1", "kill-2"]
    finally:
        DELETERS.pop("fake", None)


def test_sigint_subprocess_reconciles_and_clears_temps(tmp_path: Path) -> None:
    """Real SIGINT to a subprocess that installed the pipeline handlers."""
    import signal
    import time

    run_dir = tmp_path / "runs" / "vid" / "rk"
    source_dir = tmp_path / "sources" / "vid"
    out_dir = tmp_path / "out"
    run_dir.mkdir(parents=True)
    source_dir.mkdir(parents=True)
    out_dir.mkdir(parents=True)
    ready = tmp_path / "ready"
    code = textwrap.dedent(
        f"""
        import os, signal, sys, time
        from pathlib import Path
        sys.path.insert(0, {str(Path.cwd() / "src")!r})
        from frameweave.config import load
        from frameweave.ledger import DELETERS, Ledger
        from frameweave.pipeline import RunContext, _install_handlers
        from frameweave.range import RangeSpec
        from frameweave.types import Resolved

        run_dir = Path({str(run_dir)!r})
        source_dir = Path({str(source_dir)!r})
        (run_dir / "frame.tmp.jpg").write_bytes(b"x")
        (run_dir / "media.partial").write_bytes(b"y")
        (run_dir / ".assembled.json.1.tmp").write_bytes(b"z")
        led = Ledger(run_dir)
        led.register("fake", "int-1")

        def _delete(uid: str) -> None:
            (run_dir / f"deleted-{{uid}}").write_text("ok", encoding="utf-8")

        DELETERS["fake"] = _delete
        cfg = load(
            flags={{
                "out": Path({str(out_dir)!r}),
                "cache_dir": Path({str(tmp_path)!r}),
                "vision_lane": "claude",
            }},
            env={{}},
            toml_path=Path("/nonexistent/frameweave-test/config.toml"),
            dotenv_paths=[],
        )
        ctx = RunContext(
            config=cfg,
            raw_input="x",
            source=object(),
            resolved=Resolved(
                video_id="vid",
                title="t",
                channel="c",
                source="x",
                duration=1.0,
            ),
            source_dir=source_dir,
            run_dir=run_dir,
            run_key="rk",
            range_spec=RangeSpec(0.0, 1.0, "full", "", "full"),
            ledger=led,
            progress=lambda _m: None,
            redo=set(),
        )
        _install_handlers(ctx)
        Path({str(ready)!r}).write_text("1", encoding="utf-8")
        while True:
            time.sleep(0.05)
        """
    )
    proc = subprocess.Popen(
        [sys.executable, "-c", code], env=_child_vision_env(tmp_path)
    )
    try:
        for _ in range(200):
            if ready.is_file():
                break
            if proc.poll() is not None:
                raise AssertionError(f"subprocess exited early: {proc.returncode}")
            time.sleep(0.05)
        else:
            proc.kill()
            raise AssertionError("subprocess never became ready")
        proc.send_signal(signal.SIGINT)
        proc.wait(timeout=10)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)

    assert not (run_dir / "frame.tmp.jpg").exists()
    assert not (run_dir / "media.partial").exists()
    assert not (run_dir / ".assembled.json.1.tmp").exists()
    assert (run_dir / "deleted-int-1").is_file()


@pytest.mark.parametrize(
    ("sig", "expected_code"),
    [
        ("SIGINT", 130),
        ("SIGTERM", 143),
    ],
)
def test_signal_during_describe_stops_within_one_batch(
    tmp_path: Path, sig: str, expected_code: int
) -> None:
    """SIGINT/SIGTERM mid-describe: exit 130/143, no temps, next run resumes.

    Before the #68 fix, SIGINT during describe re-delivered via os.kill and the
    worker exited -2 (signal death) rather than 130 through the CLI; a vision
    child killed by the same signal could also be swallowed as a retryable
    failure so batches continued. Observed on main: exit -2, not 130.
    """
    import os
    import signal
    import time

    video = _video(tmp_path)
    out = tmp_path / "out"
    cache = tmp_path / "cache"
    out.mkdir()
    cache.mkdir()
    ready = tmp_path / "ready"
    calls_path = tmp_path / "calls.txt"

    code = textwrap.dedent(
        f"""
        import sys, time
        from pathlib import Path
        sys.path.insert(0, {str(Path.cwd() / "src")!r})
        sys.path.insert(0, {str(Path.cwd())!r})

        from frameweave.cli import main
        from frameweave.types import Description, Usage
        from tests.fakes.pipeline import FakeSource, FakeStt

        ready = Path({str(ready)!r})
        calls_path = Path({str(calls_path)!r})
        video = Path({str(video)!r})
        out = Path({str(out)!r})
        cache = Path({str(cache)!r})

        class SlowVision:
            name = "fake-vision"
            model = "fake-model"
            calls = 0

            def describe(self, batch, context, frames_dir, config):
                self.calls += 1
                calls_path.write_text(str(self.calls), encoding="utf-8")
                # Leave a temp the handler must clear.
                (frames_dir.parent / "describe.partial").write_bytes(b"x")
                ready.write_text(str(self.calls), encoding="utf-8")
                time.sleep(5)
                return [
                    Description(
                        frame_id=frame.id,
                        source="fake:fake",
                        summary="s",
                        strings=[],
                    )
                    for frame in batch
                ], Usage(calls=1)

        code = main(
            [
                "run",
                str(video),
                "--out",
                str(out / "exact"),
                "--vision",
                "claude",
                "--captions",
                "none",
                "--frames-per-call",
                "2",
                "--frame-interval",
                "5",
                "--quiet",
            ],
            backends={{
                "source": FakeSource(video=video),
                "stt": FakeStt(),
                "vision": SlowVision(),
            }},
            env={{
                "FRAMEWEAVE_OUT": str(out),
                "FRAMEWEAVE_CACHE_DIR": str(cache),
            }},
            toml_path=Path("/nonexistent/frameweave-test/config.toml"),
            dotenv_paths=[],
        )
        raise SystemExit(code)
        """
    )
    proc = subprocess.Popen(
        [sys.executable, "-c", code],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=_child_vision_env(tmp_path),
    )
    stderr_text = ""
    try:
        for _ in range(400):
            if ready.is_file() and ready.read_text(encoding="utf-8").strip() == "1":
                break
            if proc.poll() is not None:
                out_t, err_t = proc.communicate()
                raise AssertionError(
                    f"worker exited early rc={proc.returncode}\n"
                    f"stdout={out_t}\nstderr={err_t}"
                )
            time.sleep(0.05)
        else:
            proc.kill()
            raise AssertionError("describe never started")

        signum = getattr(signal, sig)
        os.kill(proc.pid, signum)
        try:
            _, stderr_text = proc.communicate(timeout=8)
        except subprocess.TimeoutExpired:
            proc.kill()
            _, stderr_text = proc.communicate(timeout=5)
            raise AssertionError(
                f"worker did not exit within one batch after {sig}"
            ) from None
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)

    assert proc.returncode == expected_code, (
        f"expected {expected_code}, got {proc.returncode}; stderr={stderr_text}"
    )
    assert calls_path.read_text(encoding="utf-8").strip() == "1"

    run_dirs = list((cache / "runs").rglob("frames.json"))
    assert run_dirs, "expected a run dir with frames.json from earlier stages"
    run_dir = run_dirs[0].parent
    leftovers = [
        p
        for p in run_dir.rglob("*")
        if p.is_file()
        and (
            p.name.endswith(".partial")
            or p.name.endswith(".tmp")
            or p.name.endswith(".tmp.jpg")
            or ".tmp." in p.name
        )
    ]
    assert leftovers == []

    # Following run reuses resolve/fetch/transcript/frames; only describe (+assemble).
    cfg = _cfg(
        tmp_path,
        out=out,
        cache_dir=cache,
        frames_per_call=2,
        frame_interval_s=5.0,
    )
    source = FakeSource(video=video)
    stt = FakeStt()
    vision = FakeVision()
    run(str(video), cfg, source=source, stt=stt, vision=vision)
    assert source.fetch_calls == 0
    assert stt.calls == 0
    assert vision.calls >= 1


def test_stage_registry_order() -> None:
    assert [s.name for s in STAGES] == [
        "resolve",
        "fetch_media",
        "fetch_captions",
        "transcript",
        "frames",
        "dedupe",
        "describe",
        "assemble",
    ]


def test_two_chapter_runs_reuse_fetch_distinct_keys(tmp_path: Path) -> None:
    """Second chapter: zero fetch calls, distinct run key and output folder."""
    video = _video(tmp_path)
    cfg = _cfg(tmp_path, vision_lane="none")
    source = FakeSource(video=video)
    stt = FakeStt(
        segments=[
            Segment(0.0, 5.0, "intro line", "stt-fake", id="s0001"),
            Segment(8.0, 12.0, "middle line", "stt-fake", id="s0002"),
            Segment(15.0, 18.0, "outro line", "stt-fake", id="s0003"),
        ]
    )
    vision = FakeVision()
    resolved = Resolved(
        video_id="chapters-vid",
        title="Synthetic twenty",
        channel="Test Channel",
        source=str(video.resolve()),
        duration=20.0,
        chapters=[
            Chapter(0.0, "Intro"),
            Chapter(8.0, "Middle"),
            Chapter(15.0, "Outro"),
        ],
        has_captions=False,
    )

    first = run(
        str(video),
        cfg,
        source=source,
        stt=stt,
        vision=vision,
        resolved=resolved,
        chapter="Intro",
    )
    fetch_after_first = source.fetch_calls
    assert fetch_after_first >= 1

    second = run(
        str(video),
        cfg,
        source=source,
        stt=stt,
        vision=vision,
        resolved=resolved,
        chapter="Middle",
    )
    assert source.fetch_calls == fetch_after_first
    assert first.run_key != second.run_key
    assert first.output_path != second.output_path
    assert first.output_path.name == "intro"
    assert second.output_path.name == "middle"

    first_text = (first.output_path / "transcript.fwv").read_text(encoding="utf-8")
    second_text = (second.output_path / "transcript.fwv").read_text(encoding="utf-8")
    assert "range: chapter: Intro" in first_text
    assert "range: chapter: Middle" in second_text
    assert "chapters: 00:00:00 Intro; 00:00:08 Middle; 00:00:15 Outro" in first_text
    assert "chapters: 00:00:00 Intro; 00:00:08 Middle; 00:00:15 Outro" in second_text
    assert "## [00:00:00] Intro" in first_text
    assert "## [00:00:08] Middle" not in first_text
    assert "## [00:00:08] Middle" in second_text
    assert "## [00:00:00] Intro" not in second_text


def test_auto_run_writes_lane_choice_and_lane_actual(tmp_path: Path) -> None:
    """Pipeline wiring: auto → meta.lane_choice and cost.lane_actual (issue #26 fix)."""
    from datetime import UTC, datetime

    snap_path = tmp_path / "usage-snapshot.json"
    snap_path.write_text(
        json.dumps(
            {
                "generatedAt": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "providers": {
                    "claude": {
                        "metrics": [
                            {"id": "five_hour", "percentUsed": 10},
                            {"id": "seven_day", "percentUsed": 20},
                        ]
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    video = _video(tmp_path)
    cfg = _cfg(
        tmp_path,
        vision_lane="auto",
        usage_snapshot=snap_path,
        usage_refresh_script=tmp_path / "missing-refresh.sh",
    )
    outcome = run(
        str(video),
        cfg,
        source=FakeSource(video=video),
        stt=FakeStt(),
        vision=FakeVision(),
    )
    assert outcome.vision_lane == "claude"
    meta = json.loads((outcome.output_path / "meta.json").read_text(encoding="utf-8"))
    assert "lane_choice" in meta
    assert meta["lane_choice"]["lane"] == "claude"
    cost = json.loads((outcome.output_path / "cost.json").read_text(encoding="utf-8"))
    assert "lane_actual" in cost
    assert cost["lane_actual"]["lane"] == "claude"
    assert "tokens_per_frame" in cost["lane_actual"]
    # Chooser before-reading is present, so quota_delta is populated (not {}).
    assert "five_hour" in cost["lane_actual"]["quota_delta"]


def test_local_file_captions_are_note_not_warning(tmp_path: Path) -> None:
    """Non-YouTube sources: no caption tracks is a stats note, not a warning."""
    video = _video(tmp_path)
    cfg = _cfg(tmp_path)
    outcome = run(
        str(video),
        cfg,
        source=FakeSource(video=video, name="local"),
        stt=FakeStt(),
        vision=FakeVision(),
    )
    assert outcome.completion == "complete"
    assert not any("caption" in w.lower() for w in outcome.warnings)
    meta = json.loads((outcome.output_path / "meta.json").read_text(encoding="utf-8"))
    assert meta["completion"] == "complete"
    assert meta["warnings"] == []
    assert meta["stats"]["captions"] == "not applicable (local file)"
    captions = json.loads(
        next((cfg.cache_dir / "sources").rglob("captions.json")).read_text(encoding="utf-8")
    )
    assert captions["reason"] == "not applicable (local file)"


def test_direct_url_captions_are_note_not_warning(tmp_path: Path) -> None:
    """Direct URL sources: no caption tracks is a stats note, not a warning."""
    video = _video(tmp_path)
    cfg = _cfg(tmp_path)
    outcome = run(
        str(video),
        cfg,
        source=FakeSource(video=video, name="http"),
        stt=FakeStt(),
        vision=FakeVision(),
    )
    assert outcome.completion == "complete"
    meta = json.loads((outcome.output_path / "meta.json").read_text(encoding="utf-8"))
    assert meta["stats"]["captions"] == "not applicable (direct URL)"
    assert meta["warnings"] == []


def test_explicit_gemini_without_key_fails_before_fetch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Issue #66: an unavailable explicit lane raises before stage 1 downloads anything."""
    from frameweave.vision.choose import NoVisionLane

    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    video = _video(tmp_path)
    cfg = _cfg(tmp_path, vision_lane="gemini")
    source = FakeSource(video=video)
    with pytest.raises(NoVisionLane) as excinfo:
        run(
            str(video),
            cfg,
            source=source,
            stt=FakeStt(),
            vision=FakeVision(),
        )
    assert "GEMINI_API_KEY not set" in str(excinfo.value)
    assert source.fetch_calls == 0  # nothing downloaded


def test_auto_raises_before_fetch_when_no_lane_available(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Issue #66: auto with every candidate unavailable fails before stage 1."""
    from frameweave.vision.choose import NoVisionLane

    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.setattr("shutil.which", lambda _name: None)
    video = _video(tmp_path)
    cfg = _cfg(
        tmp_path,
        vision_lane="auto",
        usage_snapshot=tmp_path / "missing-snapshot.json",
        usage_refresh_script=tmp_path / "missing-refresh.sh",
    )
    source = FakeSource(video=video)
    with pytest.raises(NoVisionLane):
        run(
            str(video),
            cfg,
            source=source,
            stt=FakeStt(),
            vision=FakeVision(),
        )
    assert source.fetch_calls == 0


def test_explicit_lane_with_injected_backend_runs_without_real_cli(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Issue #66 fix: an injected backend makes the real claude CLI's absence moot."""
    monkeypatch.setattr("shutil.which", lambda _name: None)
    video = _video(tmp_path)
    cfg = _cfg(tmp_path, vision_lane="claude")
    outcome = run(
        str(video),
        cfg,
        source=FakeSource(video=video),
        stt=FakeStt(),
        vision=FakeVision(),
    )
    assert outcome.completion == "complete"
    assert outcome.vision_lane == "claude"
