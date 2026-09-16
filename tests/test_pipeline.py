"""Pipeline registry: reuse, redo, frames-only, ledger timing, cleanup."""

from __future__ import annotations

import json
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from frameweave.config import load
from frameweave.ledger import DELETERS, Ledger, reconcile
from frameweave.pipeline import (
    STAGES,
    RunContext,
    _cleanup_on_signal,
    cli_flags,
    preflight_checks,
    run,
)
from frameweave.types import Resolved, Segment
from tests.fakes.pipeline import FakeSource, FakeStt, FakeVision
from tests.make_synthetic import make as make_synthetic

MISSING_TOML = Path("/nonexistent/frameweave-test/config.toml")


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
    video = _video(tmp_path)
    cfg = _cfg(tmp_path, frames_per_call=8)
    source = FakeSource(video=video)
    stt = FakeStt(
        segments=[
            Segment(0.0, 4.0, "alpha window text", "stt-fake", id="s0001"),
            Segment(8.0, 12.0, "beta window text", "stt-fake", id="s0002"),
        ]
    )
    vision = FakeVision()
    run(str(video), cfg, source=source, stt=stt, vision=vision)
    assert vision.contexts
    joined = " ".join(vision.contexts)
    assert "alpha window text" in joined or "beta window text" in joined


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


def test_sigint_reconciles_and_clears_temps(tmp_path: Path) -> None:
    run_dir = tmp_path / "runs" / "vid" / "rk"
    source_dir = tmp_path / "sources" / "vid"
    run_dir.mkdir(parents=True)
    source_dir.mkdir(parents=True)
    (run_dir / "frame.tmp.jpg").write_bytes(b"x")
    (run_dir / "media.partial").write_bytes(b"y")
    led = Ledger(run_dir)
    led.register("fake", "int-1")

    deleted: list[str] = []
    DELETERS["fake"] = deleted.append
    try:
        cfg = _cfg(tmp_path)
        ctx = RunContext(
            config=cfg,
            raw_input="x",
            source=FakeSource(),
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
            range_spec="full",
            ledger=led,
            progress=lambda _m: None,
            redo=set(),
        )
        _cleanup_on_signal(ctx)
        assert deleted == ["int-1"]
        assert not (run_dir / "frame.tmp.jpg").exists()
        assert not (run_dir / "media.partial").exists()
    finally:
        DELETERS.pop("fake", None)


def test_stage_registry_order() -> None:
    assert [s.name for s in STAGES] == [
        "resolve",
        "fetch_media",
        "fetch_captions",
        "transcript",
        "frames",
        "describe",
        "assemble",
    ]
