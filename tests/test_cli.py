"""CLI: doctor, inspect, run, cache size, and end-to-end fakes."""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

from frameweave.cli import _collect_flags, _flags_dict, cli_flags, main, preflight_checks
from frameweave.config import load
from frameweave.stt.base import SttResult
from frameweave.types import Usage
from tests.fakes.pipeline import FakeSource, FakeStt, FakeVision
from tests.make_synthetic import make as make_synthetic

MISSING_TOML = Path("/nonexistent/frameweave-test/config.toml")


class EmptyStt:
    """STT that returns no segments (silent / incomplete speech)."""

    name = "stt-empty"
    calls = 0

    def transcribe(self, audio: Path, config: object) -> SttResult:
        del audio, config
        self.calls += 1
        return SttResult(
            segments=[],
            source=self.name,
            usage=Usage(calls=1, seconds=0.01, usd=0.0),
            audio_seconds=20.0,
        )


def _load_kwargs(tmp_path: Path) -> dict:
    out = tmp_path / "out"
    cache = tmp_path / "cache"
    out.mkdir(exist_ok=True)
    cache.mkdir(exist_ok=True)
    return {
        "env": {
            "FRAMEWEAVE_OUT": str(out),
            "FRAMEWEAVE_CACHE_DIR": str(cache),
            "FRAMEWEAVE_VISION_LANE": "claude",
        },
        "toml_path": MISSING_TOML,
        "dotenv_paths": [],
    }


def _video(tmp_path: Path) -> Path:
    path = tmp_path / "synthetic.mp4"
    make_synthetic(path, 20)
    return path


def test_cli_flags_and_preflight() -> None:
    names = {flag.name for flag in cli_flags()}
    assert "--debug" in names
    assert "--dry-run" in names
    assert "--out" in names
    assert preflight_checks(load(flags={}, env={}, toml_path=MISSING_TOML, dotenv_paths=[])) == []


def test_collect_flags_dedupes_and_includes_pipeline() -> None:
    flags = _collect_flags()
    by_name = {}
    for spec in flags:
        assert spec.name not in by_name
        by_name[spec.name] = spec
    assert "--out" in by_name
    assert "--redo" in by_name
    assert "--frames-only" in by_name
    assert "--vision" in by_name


def test_doctor_json(tmp_path: Path) -> None:
    buf = io.StringIO()
    code = main(
        ["doctor", "--json"],
        stdout=buf,
        **_load_kwargs(tmp_path),
    )
    payload = json.loads(buf.getvalue())
    assert isinstance(payload, list)
    assert payload
    expected = 1 if any((not row["ok"]) and row.get("required", True) for row in payload) else 0
    assert code == expected


def test_inspect_prints_block_and_only_resolved(
    tmp_path: Path,
) -> None:
    video = _video(tmp_path)
    kwargs = _load_kwargs(tmp_path)
    cache = Path(kwargs["env"]["FRAMEWEAVE_CACHE_DIR"])
    buf = io.StringIO()
    source = FakeSource(video=video)
    code = main(
        ["inspect", str(video)],
        backends={"source": source},
        stdout=buf,
        **kwargs,
    )
    assert code == 0
    text = buf.getvalue()
    assert "title:" in text
    assert "frames upper bound:" in text
    assert "tokens (est):" in text
    assert source.fetch_calls == 0
    # Only resolved.json under the cache (plus empty dirs).
    files = [p for p in cache.rglob("*") if p.is_file()]
    assert len(files) == 1
    assert files[0].name == "resolved.json"


def test_run_e2e_writes_section2_files(tmp_path: Path) -> None:
    video = _video(tmp_path)
    kwargs = _load_kwargs(tmp_path)
    out_root = Path(kwargs["env"]["FRAMEWEAVE_OUT"])
    buf = io.StringIO()
    err = io.StringIO()
    source = FakeSource(video=video)
    stt = FakeStt()
    vision = FakeVision()
    code = main(
        ["run", str(video), "--out", str(out_root / "exact")],
        backends={"source": source, "stt": stt, "vision": vision},
        stdout=buf,
        stderr=err,
        **kwargs,
    )
    assert code == 0, err.getvalue()
    lines = [line for line in buf.getvalue().splitlines() if line.strip()]
    assert lines[-1].startswith("cost: $")
    out_path = Path(lines[-2])
    assert out_path.is_dir()
    for name in ("transcript.fwv", "meta.json", "cost.json", "README.md"):
        assert (out_path / name).is_file(), name
    assert (out_path / "frames").is_dir()
    assert any((out_path / "frames").glob("*.jpg"))
    assert "frames:" in buf.getvalue()
    assert stt.calls >= 1
    assert vision.calls >= 1


def test_out_uses_exact_path(tmp_path: Path) -> None:
    """Finding 1: --out is the output folder exactly, no channel/title nest."""
    video = _video(tmp_path)
    kwargs = _load_kwargs(tmp_path)
    exact = tmp_path / "my-exact-folder"
    buf = io.StringIO()
    code = main(
        ["run", str(video), "--out", str(exact)],
        backends={
            "source": FakeSource(video=video),
            "stt": FakeStt(),
            "vision": FakeVision(),
        },
        stdout=buf,
        **kwargs,
    )
    assert code == 0
    lines = [line for line in buf.getvalue().splitlines() if line.strip()]
    assert Path(lines[-2]) == exact.resolve()
    assert (exact / "transcript.fwv").is_file()
    assert not (exact / "test-channel").exists()


def test_run_resolves_once(tmp_path: Path) -> None:
    """Finding 4: CLI resolve is passed through; pipeline does not resolve again."""
    video = _video(tmp_path)
    kwargs = _load_kwargs(tmp_path)
    source = FakeSource(video=video)
    buf = io.StringIO()
    code = main(
        ["run", str(video), "--out", str(tmp_path / "once")],
        backends={"source": source, "stt": FakeStt(), "vision": FakeVision()},
        stdout=buf,
        **kwargs,
    )
    assert code == 0
    assert source.resolve_calls == 1


def test_run_empty_stt_exits_2(tmp_path: Path) -> None:
    video = _video(tmp_path)
    kwargs = _load_kwargs(tmp_path)
    buf = io.StringIO()
    err = io.StringIO()
    code = main(
        ["run", str(video), "--out", str(tmp_path / "out2")],
        backends={
            "source": FakeSource(video=video),
            "stt": EmptyStt(),
            "vision": FakeVision(),
        },
        stdout=buf,
        stderr=err,
        **kwargs,
    )
    assert code == 2, (buf.getvalue(), err.getvalue())


def test_run_frames_only_exits_0(tmp_path: Path) -> None:
    video = _video(tmp_path)
    kwargs = _load_kwargs(tmp_path)
    cache = Path(kwargs["env"]["FRAMEWEAVE_CACHE_DIR"])
    buf = io.StringIO()
    code = main(
        ["run", str(video), "--frames-only", "--out", str(tmp_path / "fo")],
        backends={
            "source": FakeSource(video=video),
            "stt": FakeStt(),
            "vision": FakeVision(),
        },
        stdout=buf,
        **kwargs,
    )
    assert code == 0
    text = buf.getvalue()
    lines = [line for line in text.splitlines() if line.strip()]
    assert lines[-1].startswith("cost:")
    assembled = list(cache.rglob("assembled.json"))
    assert assembled
    marker = json.loads(assembled[0].read_text(encoding="utf-8"))
    assert marker["completion"] == "complete (speech not requested)"


def test_run_dry_run_exits_0_no_fetch(tmp_path: Path) -> None:
    video = _video(tmp_path)
    kwargs = _load_kwargs(tmp_path)
    source = FakeSource(video=video)
    buf = io.StringIO()
    code = main(
        ["run", str(video), "--dry-run"],
        backends={"source": source},
        stdout=buf,
        **kwargs,
    )
    assert code == 0
    assert "frames upper bound:" in buf.getvalue()
    assert source.fetch_calls == 0


def test_quiet_keeps_closing_lines(tmp_path: Path) -> None:
    video = _video(tmp_path)
    kwargs = _load_kwargs(tmp_path)
    buf = io.StringIO()
    code = main(
        ["run", str(video), "--quiet", "--out", str(tmp_path / "q")],
        backends={
            "source": FakeSource(video=video),
            "stt": FakeStt(),
            "vision": FakeVision(),
        },
        stdout=buf,
        **kwargs,
    )
    assert code == 0
    lines = buf.getvalue().splitlines()
    assert not any(line.startswith("stage ") for line in lines)
    assert lines[-1].startswith("cost:")
    assert Path(lines[-2]).is_dir()


def test_quiet_before_and_after_subcommand(tmp_path: Path) -> None:
    """Finding 3: global --quiet before the subcommand is not lost."""
    video = _video(tmp_path)
    kwargs = _load_kwargs(tmp_path)
    backends = {
        "source": FakeSource(video=video),
        "stt": FakeStt(),
        "vision": FakeVision(),
    }

    before = io.StringIO()
    code_before = main(
        ["--quiet", "run", str(video), "--out", str(tmp_path / "qb")],
        backends=backends,
        stdout=before,
        **kwargs,
    )
    assert code_before == 0
    assert not any(line.startswith("stage ") for line in before.getvalue().splitlines())

    after = io.StringIO()
    code_after = main(
        ["run", str(video), "--quiet", "--out", str(tmp_path / "qa")],
        backends=backends,
        stdout=after,
        **kwargs,
    )
    assert code_after == 0
    assert not any(line.startswith("stage ") for line in after.getvalue().splitlines())


def test_usage_error_exits_1() -> None:
    """Finding 2: argparse usage errors exit 1, not 2."""
    err = io.StringIO()
    code = main(["run"], stdout=io.StringIO(), stderr=err)
    assert code == 1


def test_bare_cache_exits_1(tmp_path: Path) -> None:
    """Finding 2: bare `cache` (no size) exits 1."""
    buf = io.StringIO()
    code = main(["cache"], stdout=buf, **_load_kwargs(tmp_path))
    assert code == 1


def test_frames_line_uses_current_run_key(tmp_path: Path) -> None:
    """Finding 5: frames line reads this run key's frames.json, not newest mtime."""
    import os
    import time

    video = _video(tmp_path)
    kwargs = _load_kwargs(tmp_path)
    cache = Path(kwargs["env"]["FRAMEWEAVE_CACHE_DIR"])
    source = FakeSource(video=video)
    resolved = source.resolve(str(video))
    # Sibling run key under the same video_id with a newer mtime and a loud count.
    decoy_root = cache / "runs" / resolved.video_id / "other-run-key"
    decoy_root.mkdir(parents=True)
    decoy = decoy_root / "frames.json"
    decoy.write_text(
        json.dumps(
            {
                "frames": [
                    {"id": "f0001", "kind": "primary"},
                    {"id": "f0002", "kind": "primary"},
                    {"id": "f0003", "kind": "extra"},
                    {"id": "f0004", "kind": "extra"},
                    {"id": "f0005", "kind": "extra"},
                ]
            }
        ),
        encoding="utf-8",
    )
    future = time.time() + 10_000
    os.utime(decoy, (future, future))

    buf = io.StringIO()
    code = main(
        ["run", str(video), "--out", str(tmp_path / "fk")],
        backends={"source": source, "stt": FakeStt(), "vision": FakeVision()},
        stdout=buf,
        **kwargs,
    )
    assert code == 0
    text = buf.getvalue()
    assert "frames:" in text
    assert "frames: 5 primary 2 extra 3" not in text
    # Confirm the current run key's file is what we would read.
    from dataclasses import replace

    from frameweave.config import load
    from frameweave.config import run_key as make_run_key

    cfg = load(flags={}, **kwargs)
    keyed = cfg if cfg.vision_lane != "auto" else replace(cfg, vision_lane="claude")
    key = make_run_key(keyed, "full")
    real = json.loads(
        (cache / "runs" / resolved.video_id / key / "frames.json").read_text(encoding="utf-8")
    )
    frames = real["frames"]
    primary = sum(1 for f in frames if f["kind"] == "primary")
    extra = sum(1 for f in frames if f["kind"] == "extra")
    assert f"frames: {len(frames)} primary {primary} extra {extra}" in text


def test_frames_line_uses_range_label(tmp_path: Path) -> None:
    """Range runs key frames.json by the range label, not hardcoded full."""
    video = _video(tmp_path)
    kwargs = _load_kwargs(tmp_path)
    buf = io.StringIO()
    code = main(
        [
            "run",
            str(video),
            "--start",
            "5",
            "--end",
            "10",
            "--out",
            str(tmp_path / "range-frames"),
        ],
        backends={
            "source": FakeSource(video=video),
            "stt": FakeStt(),
            "vision": FakeVision(),
        },
        stdout=buf,
        **kwargs,
    )
    assert code == 0
    text = buf.getvalue()
    assert "frames:" in text

    from dataclasses import replace

    from frameweave.config import load
    from frameweave.config import run_key as make_run_key

    cfg = load(flags={}, **kwargs)
    keyed = cfg if cfg.vision_lane != "auto" else replace(cfg, vision_lane="claude")
    cache = Path(kwargs["env"]["FRAMEWEAVE_CACHE_DIR"])
    source = FakeSource(video=video)
    video_id = source.resolve(str(video)).video_id
    key = make_run_key(keyed, "00:00:05-00:00:10")
    assert (cache / "runs" / video_id / key / "frames.json").is_file()
    # Hardcoded "full" would miss this file and omit the frames line.
    full_key = make_run_key(keyed, "full")
    assert full_key != key
    assert not (cache / "runs" / video_id / full_key / "frames.json").is_file()


def test_flags_dict_passes_config_dests() -> None:
    """Finding 6: every non-CLI dest is passed; load rejects unknowns."""
    import argparse

    args = argparse.Namespace(vision_lane="claude", frame_width=640, youtube_client=None)
    flags = _flags_dict(args)
    assert "vision_lane" in flags
    assert "out" not in flags
    assert "debug" not in flags
    # None values are fine; a set unknown would raise in load.
    load(
        flags={k: v for k, v in flags.items() if v is not None},
        env={},
        toml_path=MISSING_TOML,
        dotenv_paths=[],
    )


def test_inspect_prints_lane_projection_table(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Issue #26: inspect prints one projection row per candidate lane."""
    from datetime import UTC, datetime

    from frameweave.vision.choose import Snapshot

    snap = Snapshot(
        generated_at=datetime(2026, 9, 16, 18, 0, 0, tzinfo=UTC),
        metrics={
            "claude": {"five_hour": 10.0, "seven_day": 20.0},
            "openai": {"primary": 5.0, "secondary": 15.0},
        },
    )
    monkeypatch.setattr("frameweave.cli.load_snapshot", lambda *a, **k: snap)

    video = _video(tmp_path)
    kwargs = _load_kwargs(tmp_path)
    kwargs["env"] = {**kwargs["env"], "FRAMEWEAVE_VISION_LANE": "auto"}
    buf = io.StringIO()
    code = main(
        ["inspect", str(video)],
        backends={"source": FakeSource(video=video)},
        stdout=buf,
        **kwargs,
    )
    assert code == 0
    text = buf.getvalue()
    assert "lane projection:" in text
    assert "claude" in text
    assert "gemini" in text
    assert "chosen:" in text


def test_no_dollars_line_for_lane_none(tmp_path: Path) -> None:
    """Finding 7: lane none omits the dollars estimate line."""
    video = _video(tmp_path)
    kwargs = _load_kwargs(tmp_path)
    kwargs["env"] = {**kwargs["env"], "FRAMEWEAVE_VISION_LANE": "none"}
    buf = io.StringIO()
    code = main(
        ["inspect", str(video)],
        backends={"source": FakeSource(video=video)},
        stdout=buf,
        **kwargs,
    )
    assert code == 0
    assert "dollars (est):" not in buf.getvalue()
    assert "vision lane: none" in buf.getvalue()


def test_injected_empty_env_is_honoured(tmp_path: Path) -> None:
    """Finding 7: env={} must not fall back to os.environ."""
    buf = io.StringIO()
    code = main(
        ["doctor", "--json"],
        stdout=buf,
        env={},
        toml_path=MISSING_TOML,
        dotenv_paths=[],
    )
    payload = json.loads(buf.getvalue())
    out_row = next(row for row in payload if row["name"] == "output root")
    assert out_row["ok"] is False
    expected = 1 if any((not row["ok"]) and row.get("required", True) for row in payload) else 0
    assert code == expected
    assert code == 1


def test_cache_size(tmp_path: Path) -> None:
    kwargs = _load_kwargs(tmp_path)
    cache = Path(kwargs["env"]["FRAMEWEAVE_CACHE_DIR"])
    (cache / "marker.txt").write_text("hi\n", encoding="utf-8")
    buf = io.StringIO()
    code = main(["cache", "size"], stdout=buf, **kwargs)
    assert code == 0
    assert "cache:" in buf.getvalue()
