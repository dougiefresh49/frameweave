"""CLI: doctor, inspect, run, cache size, and end-to-end fakes."""

from __future__ import annotations

import io
import json
from pathlib import Path

from frameweave.cli import _collect_flags, cli_flags, main, preflight_checks
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
    assert code in (0, 1)


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
    assert text.strip().splitlines()[-1].startswith("cost:")


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


def test_cache_size(tmp_path: Path) -> None:
    kwargs = _load_kwargs(tmp_path)
    cache = Path(kwargs["env"]["FRAMEWEAVE_CACHE_DIR"])
    (cache / "marker.txt").write_text("hi\n", encoding="utf-8")
    buf = io.StringIO()
    code = main(["cache", "size"], stdout=buf, **kwargs)
    assert code == 0
    assert "cache:" in buf.getvalue()
