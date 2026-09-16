"""Preflight / doctor: stubbed PATH and env, collector seams, exit-code regression."""

from __future__ import annotations

import json
import sys
import types
from io import StringIO
from pathlib import Path
from subprocess import CompletedProcess

import pytest

from frameweave import preflight
from frameweave.config import Check, ConfigError, load, require_out

MISSING_TOML = Path("/nonexistent/frameweave-test/config.toml")


def missing_out_message(config) -> str:
    try:
        require_out(config)
    except ConfigError as exc:
        return str(exc)
    raise AssertionError("require_out must raise when config.out is None")


def load_cfg(flags: dict | None = None, *, env: dict[str, str] | None = None):
    return load(
        flags,
        env={} if env is None else env,
        toml_path=MISSING_TOML,
        dotenv_paths=[],
    )


def which_map(mapping: dict[str, str | None]):
    def which(name: str) -> str | None:
        return mapping.get(name)

    return which


def run_versions(versions: dict[str, str]):
    def run(argv, **_kwargs):
        name = Path(argv[0]).name
        text = versions.get(name, "")
        return CompletedProcess(argv, 0, stdout=text, stderr="")

    return run


@pytest.fixture
def happy_which():
    return which_map(
        {
            "ffmpeg": "/bin/ffmpeg",
            "ffprobe": "/bin/ffprobe",
            "claude": "/bin/claude",
            "codex": "/bin/codex",
        }
    )


@pytest.fixture
def happy_run():
    return run_versions(
        {
            "ffmpeg": "ffmpeg version 8.0 Copyright (c) 2000-2025\n",
            "ffprobe": "ffprobe version 8.0 Copyright (c) 2000-2025\n",
            "claude": "claude 1.0.0\n",
        }
    )


def test_cli_flags() -> None:
    flags = preflight.cli_flags()
    assert len(flags) == 1
    assert flags[0].name == "--json"
    assert flags[0].dest == "doctor_json"
    assert flags[0].type is bool


def test_collect_skips_missing_modules(happy_which, happy_run, tmp_path: Path) -> None:
    out = tmp_path / "out"
    out.mkdir()
    cache = tmp_path / "cache"
    cfg = load_cfg(flags={"out": out, "cache_dir": cache, "vision_lane": "none"})
    rows = {
        row.name: row
        for row in preflight.collect(cfg, env={}, which=happy_which, run=happy_run)
    }
    assert "ffmpeg" in rows
    assert rows["ffmpeg"].ok is True
    assert rows["ffmpeg"].detail == "8.0"
    assert "frameweave.sources.youtube" not in rows
    assert "output root" in rows
    assert rows["output root"].ok is True
    assert "cache dir" in rows


def test_fake_module_injected(
    monkeypatch: pytest.MonkeyPatch, happy_which, happy_run, tmp_path: Path
) -> None:
    out = tmp_path / "out"
    out.mkdir()
    cache = tmp_path / "cache"
    cfg = load_cfg(flags={"out": out, "cache_dir": cache, "vision_lane": "none"})

    fake = types.ModuleType("frameweave.frames")

    def checks(_config):
        return [Check("frames-extra", True, "from fake", "n/a")]

    fake.preflight_checks = checks  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "frameweave.frames", fake)

    rows = {
        row.name: row
        for row in preflight.collect(cfg, env={}, which=happy_which, run=happy_run)
    }
    assert rows["frames-extra"].detail == "from fake"


def test_module_that_raises(
    monkeypatch: pytest.MonkeyPatch, happy_which, happy_run, tmp_path: Path
) -> None:
    out = tmp_path / "out"
    out.mkdir()
    cache = tmp_path / "cache"
    cfg = load_cfg(flags={"out": out, "cache_dir": cache, "vision_lane": "none"})

    boom = types.ModuleType("frameweave.captions")

    def checks(_config):
        raise RuntimeError("captions exploded")

    boom.preflight_checks = checks  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "frameweave.captions", boom)

    rows = {
        row.name: row
        for row in preflight.collect(cfg, env={}, which=happy_which, run=happy_run)
    }
    assert rows["frameweave.captions"].ok is False
    assert "captions exploded" in rows["frameweave.captions"].detail


def test_nested_missing_import_is_failed_row(
    monkeypatch: pytest.MonkeyPatch, happy_which, happy_run, tmp_path: Path
) -> None:
    """ModuleNotFoundError for a dependency (not the module itself) fails the row."""
    out = tmp_path / "out"
    out.mkdir()
    cache = tmp_path / "cache"
    cfg = load_cfg(flags={"out": out, "cache_dir": cache, "vision_lane": "none"})

    fake = types.ModuleType("frameweave.stt.local")

    def checks(_config):
        raise ModuleNotFoundError("No module named 'whisperx'", name="whisperx")

    fake.preflight_checks = checks  # type: ignore[attr-defined]
    stt = types.ModuleType("frameweave.stt")
    stt.local = fake  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "frameweave.stt", stt)
    monkeypatch.setitem(sys.modules, "frameweave.stt.local", fake)

    rows = {
        row.name: row
        for row in preflight.collect(cfg, env={}, which=happy_which, run=happy_run)
    }
    assert rows["frameweave.stt.local"].ok is False
    assert "whisperx" in rows["frameweave.stt.local"].detail


def test_dedupe_module_wins_over_builtin(
    monkeypatch: pytest.MonkeyPatch, happy_which, happy_run, tmp_path: Path
) -> None:
    out = tmp_path / "out"
    out.mkdir()
    cache = tmp_path / "cache"
    cfg = load_cfg(flags={"out": out, "cache_dir": cache, "vision_lane": "none"})

    fake = types.ModuleType("frameweave.sources.local")

    def checks(_config):
        return [Check("ffmpeg", True, "module ffmpeg", "from module")]

    fake.preflight_checks = checks  # type: ignore[attr-defined]
    sources = types.ModuleType("frameweave.sources")
    sources.local = fake  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "frameweave.sources", sources)
    monkeypatch.setitem(sys.modules, "frameweave.sources.local", fake)

    rows = {
        row.name: row
        for row in preflight.collect(cfg, env={}, which=happy_which, run=happy_run)
    }
    assert rows["ffmpeg"].detail == "module ffmpeg"
    assert rows["ffmpeg"].remedy == "from module"


def test_missing_out_still_runs_other_rows(happy_which, happy_run, tmp_path: Path) -> None:
    cache = tmp_path / "cache"
    cfg = load_cfg(flags={"cache_dir": cache, "vision_lane": "none"})
    expected = missing_out_message(cfg)
    rows = list(preflight.collect(cfg, env={}, which=happy_which, run=happy_run))
    by_name = {row.name: row for row in rows}
    assert by_name["output root"].ok is False
    assert by_name["output root"].detail == expected
    assert by_name["output root"].remedy == expected
    assert "ffmpeg" in by_name
    assert "python" in by_name
    assert "yt-dlp" in by_name
    assert "disk" in by_name
    assert "cache dir" in by_name


def test_exit_code_from_rows() -> None:
    ok_rows = [Check("a", True, "ok", "n/a"), Check("b", False, "warn", "n/a", required=False)]
    assert preflight.exit_code(ok_rows) == 0
    fail_rows = [Check("a", False, "bad", "fix", required=True)]
    assert preflight.exit_code(fail_rows) == 1


def test_exit_code_matches_summary_not_print_text(happy_which, happy_run, tmp_path: Path) -> None:
    """Regression: exit 1 while printing a happy summary must not happen."""
    out = tmp_path / "out"
    out.mkdir()
    cache = tmp_path / "cache"
    cfg = load_cfg(flags={"out": out, "cache_dir": cache, "vision_lane": "none"})
    buf = StringIO()
    code = preflight.run_doctor(
        cfg,
        env={},
        which=which_map({"ffmpeg": None, "ffprobe": "/bin/ffprobe"}),
        run=happy_run,
        out=buf,
    )
    text = buf.getvalue()
    assert code == 1
    assert "1 failed" in text
    assert "FAIL ffmpeg" in text
    assert "All checks passed" not in text


def test_as_json_round_trip(happy_which, happy_run, tmp_path: Path) -> None:
    out = tmp_path / "out"
    out.mkdir()
    cache = tmp_path / "cache"
    cfg = load_cfg(flags={"out": out, "cache_dir": cache, "vision_lane": "none"})
    rows = preflight.collect(cfg, env={"GEMINI_API_KEY": "x"}, which=happy_which, run=happy_run)
    payload = json.loads(preflight.as_json(rows))
    assert isinstance(payload, list)
    assert {item["name"] for item in payload} == {row.name for row in rows}
    for item, row in zip(payload, rows, strict=True):
        assert item["ok"] is row.ok
        assert item["detail"] == row.detail
        assert item["remedy"] == row.remedy
        assert item["required"] is row.required
    assert preflight.exit_code(rows) in {0, 1}


def test_disk_warning_never_flips_exit(
    monkeypatch: pytest.MonkeyPatch, happy_which, happy_run, tmp_path: Path
) -> None:
    out = tmp_path / "out"
    out.mkdir()
    cache = tmp_path / "cache"
    cache.mkdir()
    cfg = load_cfg(
        flags={"out": out, "cache_dir": cache, "vision_lane": "none", "disk_warn_gb": 10}
    )

    class Usage:
        free = 1 * 1024**3
        total = 100 * 1024**3
        used = 99 * 1024**3

    monkeypatch.setattr(preflight.shutil, "disk_usage", lambda _path: Usage())

    rows = list(
        preflight.collect(
            cfg, env={"GEMINI_API_KEY": "x"}, which=happy_which, run=happy_run
        )
    )
    by_name = {row.name: row for row in rows}
    assert by_name["disk"].ok is False
    assert by_name["disk"].required is False
    assert "frameweave cache prune" in by_name["disk"].remedy

    required_failures = [row for row in rows if (not row.ok) and row.required]
    assert required_failures == []

    buf = StringIO()
    code = preflight.run_doctor(
        cfg,
        env={"GEMINI_API_KEY": "x"},
        which=happy_which,
        run=happy_run,
        out=buf,
    )
    text = buf.getvalue()
    assert "warn disk" in text
    assert "0 failed" in text
    assert code == 0


def test_gemini_key_required_only_for_gemini_lane(happy_which, happy_run, tmp_path: Path) -> None:
    out = tmp_path / "out"
    out.mkdir()
    cache = tmp_path / "cache"
    cfg = load_cfg(flags={"out": out, "cache_dir": cache, "vision_lane": "gemini"})
    rows = {
        row.name: row
        for row in preflight.collect(cfg, env={}, which=happy_which, run=happy_run)
    }
    assert rows["GEMINI_API_KEY"].required is True
    assert rows["GEMINI_API_KEY"].ok is False

    cfg2 = load_cfg(flags={"out": out, "cache_dir": cache, "vision_lane": "none"})
    rows2 = {
        row.name: row
        for row in preflight.collect(cfg2, env={}, which=happy_which, run=happy_run)
    }
    assert rows2["GEMINI_API_KEY"].required is False


def test_hf_token_required_when_speakers(happy_which, happy_run, tmp_path: Path) -> None:
    out = tmp_path / "out"
    out.mkdir()
    cache = tmp_path / "cache"
    cfg = load_cfg(flags={"out": out, "cache_dir": cache, "vision_lane": "none", "speakers": True})
    rows = {
        row.name: row
        for row in preflight.collect(cfg, env={}, which=happy_which, run=happy_run)
    }
    assert rows["HF_TOKEN"].required is True
    assert rows["HF_TOKEN"].ok is False


def test_claude_and_codex_lane_gates(happy_which, happy_run, tmp_path: Path) -> None:
    """Built-ins gate on lane; vision.base may also emit both CLI rows (dedupe wins)."""
    out = tmp_path / "out"
    out.mkdir()
    cache = tmp_path / "cache"
    auto_builtins = {
        row.name: row
        for row in preflight._builtin_checks(
            load_cfg(flags={"out": out, "cache_dir": cache, "vision_lane": "auto"}),
            env={},
            which=happy_which,
            run=happy_run,
        )
    }
    assert "claude" in auto_builtins
    assert "codex" not in auto_builtins

    codex_builtins = {
        row.name: row
        for row in preflight._builtin_checks(
            load_cfg(flags={"out": out, "cache_dir": cache, "vision_lane": "codex"}),
            env={},
            which=happy_which,
            run=happy_run,
        )
    }
    assert "codex" in codex_builtins
    assert "claude" not in codex_builtins


def test_yt_dlp_pin_from_fabricated_lock(tmp_path: Path) -> None:
    lock = tmp_path / "uv.lock"
    lock.write_text(
        '[[package]]\nname = "yt-dlp"\nversion = "2025.1.2"\n',
        encoding="utf-8",
    )
    assert preflight._yt_dlp_pin(lock) == "2025.1.2"
    row = preflight._yt_dlp_check(lock_path=lock)
    assert "lock pin 2025.1.2" in row.detail


def test_yt_dlp_pin_unavailable_when_lock_missing(tmp_path: Path) -> None:
    missing = tmp_path / "no-such-uv.lock"
    row = preflight._yt_dlp_check(lock_path=missing)
    assert row.ok is True
    assert "lock pin unavailable" in row.detail


def test_binary_version_passes_timeout(happy_which) -> None:
    seen: dict[str, object] = {}

    def run(argv, **kwargs):
        seen.update(kwargs)
        return CompletedProcess(argv, 0, stdout="ffmpeg version 8.0\n", stderr="")

    row = preflight._binary_version_check("ffmpeg", happy_which, run)
    assert row.ok is True
    assert seen.get("timeout") == 30
