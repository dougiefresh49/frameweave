"""Config schema, precedence, slugs, output paths, run key."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from frameweave.config import (
    ConfigError,
    channel_slug,
    cli_flags,
    load,
    output_dir,
    preflight_checks,
    require_out,
    run_key,
    slugify,
)

MISSING_TOML = Path("/nonexistent/frameweave-test/config.toml")
MISSING_DOTENV = Path("/nonexistent/frameweave-test/.env")
MISSING_OUT = (
    "FRAMEWEAVE_OUT is not set. Add this line to .env (or export it): "
    "FRAMEWEAVE_OUT=/path/to/output/folder"
)
AUTO_LANE_MSG = (
    "run_key needs a resolved vision lane; auto is chosen at run time by the lane chooser"
)
THEO = "Theo - t3\u2024gg"


def load_cfg(
    flags: dict | None = None,
    *,
    env: dict[str, str] | None = None,
    toml_path: Path = MISSING_TOML,
    dotenv_paths: list[Path] | None = None,
):
    return load(
        flags,
        env={} if env is None else env,
        toml_path=toml_path,
        dotenv_paths=[] if dotenv_paths is None else dotenv_paths,
    )


def resolved(flags: dict | None = None, **kwargs):
    merged = {"vision_lane": "codex", **(flags or {})}
    return load_cfg(flags=merged, **kwargs)


@pytest.fixture
def fake_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    home = tmp_path / "no-such-home"
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    return home


@pytest.fixture
def tmp_root(tmp_path: Path) -> Path:
    root = tmp_path / "out"
    for name in (
        "theo",
        "network-chuck",
        "ai-search",
        "nicksaraev",
        "todds-garage",
        "fortnite",
    ):
        (root / name).mkdir(parents=True)
    return root


def test_load_defaults(fake_home: Path) -> None:
    cfg = load_cfg()
    assert cfg.out is None
    assert cfg.cache_dir == fake_home / "Library" / "Caches" / "frameweave"
    assert cfg.vision_lane == "auto"
    assert cfg.vision_model == {
        "codex": "gpt-5.6-sol",
        "claude": "sonnet",
        "gemini": "gemini-3.5-flash-lite",
    }
    assert cfg.vision_quality == "standard"
    assert cfg.frames_per_call == 8
    assert cfg.frame_interval_s == 45.0
    assert cfg.frame_width == 1280
    assert cfg.max_frames is None
    assert cfg.stt_backend == "local"
    assert cfg.stt_model == "large-v3-turbo"
    assert cfg.stt_device == "cpu"
    assert cfg.speakers is False
    assert cfg.timeout_s == 120.0
    assert cfg.concurrency == 2
    assert cfg.glossary is None
    assert cfg.lane_skip_percent == 90
    assert cfg.disk_warn_gb == 10
    assert cfg.prompt_revision == "1"
    assert cfg.channels == {}


def test_load_returns_defaults_when_home_is_missing(fake_home: Path) -> None:
    cfg = load(env={}, toml_path=MISSING_TOML, dotenv_paths=[MISSING_DOTENV])
    assert cfg.out is None
    assert cfg.frames_per_call == 8
    assert cfg.cache_dir == fake_home / "Library" / "Caches" / "frameweave"
    assert not fake_home.exists()


def test_require_out_message() -> None:
    with pytest.raises(ConfigError) as exc:
        require_out(load_cfg())
    assert str(exc.value) == MISSING_OUT
    out = Path("/tmp/frameweave-out")
    assert require_out(load_cfg(flags={"out": out})) == out


def test_precedence_frames_per_call(tmp_path: Path) -> None:
    toml_path = tmp_path / "cfg.toml"
    toml_path.write_text("frames_per_call = 4\n")
    assert load_cfg().frames_per_call == 8
    assert load_cfg(toml_path=toml_path).frames_per_call == 4
    assert load_cfg(
        env={"FRAMEWEAVE_FRAMES_PER_CALL": "3"}, toml_path=toml_path
    ).frames_per_call == 3
    assert load_cfg(
        flags={"frames_per_call": 2},
        env={"FRAMEWEAVE_FRAMES_PER_CALL": "3"},
        toml_path=toml_path,
    ).frames_per_call == 2
    assert load_cfg(
        flags={"frames_per_call": None},
        env={"FRAMEWEAVE_FRAMES_PER_CALL": "3"},
        toml_path=toml_path,
    ).frames_per_call == 3


def test_precedence_path(tmp_path: Path, fake_home: Path) -> None:
    toml_dir = tmp_path / "toml-cache"
    env_dir = tmp_path / "env-cache"
    flag_dir = tmp_path / "flag-cache"
    toml_path = tmp_path / "cfg.toml"
    toml_path.write_text(f'cache_dir = "{toml_dir.as_posix()}"\n')
    default = fake_home / "Library" / "Caches" / "frameweave"
    assert load_cfg().cache_dir == default
    assert load_cfg(toml_path=toml_path).cache_dir == toml_dir
    assert load_cfg(
        env={"FRAMEWEAVE_CACHE_DIR": str(env_dir)}, toml_path=toml_path
    ).cache_dir == env_dir
    assert load_cfg(
        flags={"cache_dir": flag_dir},
        env={"FRAMEWEAVE_CACHE_DIR": str(env_dir)},
        toml_path=toml_path,
    ).cache_dir == flag_dir


def test_precedence_bool(tmp_path: Path) -> None:
    toml_path = tmp_path / "cfg.toml"
    toml_path.write_text("speakers = true\n")
    assert load_cfg().speakers is False
    assert load_cfg(toml_path=toml_path).speakers is True
    assert load_cfg(env={"FRAMEWEAVE_SPEAKERS": "no"}, toml_path=toml_path).speakers is False
    assert load_cfg(
        flags={"speakers": True},
        env={"FRAMEWEAVE_SPEAKERS": "no"},
        toml_path=toml_path,
    ).speakers is True


def test_precedence_float(tmp_path: Path) -> None:
    toml_path = tmp_path / "cfg.toml"
    toml_path.write_text("frame_interval_s = 10\n")
    assert load_cfg().frame_interval_s == 45.0
    assert load_cfg(toml_path=toml_path).frame_interval_s == 10.0
    assert load_cfg(
        env={"FRAMEWEAVE_FRAME_INTERVAL_S": "20"}, toml_path=toml_path
    ).frame_interval_s == 20.0
    assert load_cfg(
        flags={"frame_interval_s": 30.0},
        env={"FRAMEWEAVE_FRAME_INTERVAL_S": "20"},
        toml_path=toml_path,
    ).frame_interval_s == 30.0


def test_precedence_dict(tmp_path: Path) -> None:
    toml_path = tmp_path / "cfg.toml"
    toml_path.write_text('[vision_model]\ngemini = "toml-model"\n')
    default = load_cfg()
    assert default.vision_model["gemini"] == "gemini-3.5-flash-lite"
    assert default.vision_model["codex"] == "gpt-5.6-sol"
    toml_cfg = load_cfg(toml_path=toml_path)
    assert toml_cfg.vision_model["gemini"] == "toml-model"
    assert toml_cfg.vision_model["codex"] == "gpt-5.6-sol"
    env_cfg = load_cfg(
        env={"FRAMEWEAVE_VISION_MODEL_GEMINI": "env-model"}, toml_path=toml_path
    )
    assert env_cfg.vision_model["gemini"] == "env-model"
    flag_cfg = load_cfg(
        flags={"vision_model": {"gemini": "flag-model"}},
        env={"FRAMEWEAVE_VISION_MODEL_GEMINI": "env-model"},
        toml_path=toml_path,
    )
    assert flag_cfg.vision_model["gemini"] == "flag-model"
    assert flag_cfg.vision_model["codex"] == "gpt-5.6-sol"


def test_dotenv_earlier_wins_and_env_beats_dotenv(tmp_path: Path) -> None:
    first = tmp_path / "a.env"
    second = tmp_path / "b.env"
    first.write_text("FRAMEWEAVE_FRAMES_PER_CALL=11\n")
    second.write_text("FRAMEWEAVE_FRAMES_PER_CALL=22\n")
    assert load_cfg(dotenv_paths=[first, second]).frames_per_call == 11
    assert load_cfg(
        env={"FRAMEWEAVE_FRAMES_PER_CALL": "33"}, dotenv_paths=[first, second]
    ).frames_per_call == 33


def test_dotenv_does_not_mutate_os_environ(tmp_path: Path) -> None:
    path = tmp_path / ".env"
    path.write_text("FRAMEWEAVE_FRAMES_PER_CALL=11\n")
    snapshot = os.environ.copy()
    load_cfg(dotenv_paths=[path])
    assert os.environ == snapshot


def test_tilde_expansion(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    cfg = load_cfg(env={"FRAMEWEAVE_CACHE_DIR": "~/caches/fw"})
    assert cfg.cache_dir == tmp_path / "caches" / "fw"


def test_bool_strings_case_insensitive() -> None:
    assert load_cfg(env={"FRAMEWEAVE_SPEAKERS": "YES"}).speakers is True
    assert load_cfg(env={"FRAMEWEAVE_SPEAKERS": "False"}).speakers is False
    assert load_cfg(env={"FRAMEWEAVE_SPEAKERS": "1"}).speakers is True
    assert load_cfg(env={"FRAMEWEAVE_SPEAKERS": "0"}).speakers is False


def test_time_fields_accept_timecode() -> None:
    cfg = load_cfg(
        env={"FRAMEWEAVE_TIMEOUT_S": "2:00", "FRAMEWEAVE_FRAME_INTERVAL_S": "1:30"}
    )
    assert cfg.timeout_s == 120.0
    assert cfg.frame_interval_s == 90.0


def test_unknown_toml_key(tmp_path: Path) -> None:
    path = tmp_path / "cfg.toml"
    path.write_text("nope = 1\n")
    with pytest.raises(ConfigError, match="nope") as exc:
        load_cfg(toml_path=path)
    assert "toml" in str(exc.value)


def test_unparseable_value_names_source() -> None:
    with pytest.raises(ConfigError, match="frames_per_call") as exc:
        load_cfg(env={"FRAMEWEAVE_FRAMES_PER_CALL": "nope"})
    assert "env" in str(exc.value)
    with pytest.raises(ConfigError, match="vision_lane") as exc_flag:
        load_cfg(flags={"vision_lane": "fable"})
    assert "flag" in str(exc_flag.value)


def test_empty_env_overrides_lower_layer(tmp_path: Path) -> None:
    toml_path = tmp_path / "cfg.toml"
    toml_path.write_text('out = "/tmp/from-toml"\nframes_per_call = 4\n')
    dotenv = tmp_path / ".env"
    dotenv.write_text("FRAMEWEAVE_OUT=\n")
    assert load_cfg(toml_path=toml_path).out == Path("/tmp/from-toml")
    assert load_cfg(env={"FRAMEWEAVE_OUT": ""}, toml_path=toml_path).out is None
    assert load_cfg(toml_path=toml_path, dotenv_paths=[dotenv]).out is None
    with pytest.raises(ConfigError, match="frames_per_call") as exc:
        load_cfg(env={"FRAMEWEAVE_FRAMES_PER_CALL": ""}, toml_path=toml_path)
    assert "env" in str(exc.value)
    dotenv_required = tmp_path / "required.env"
    dotenv_required.write_text("FRAMEWEAVE_FRAMES_PER_CALL=\n")
    with pytest.raises(ConfigError, match="frames_per_call") as dotenv_exc:
        load_cfg(toml_path=toml_path, dotenv_paths=[dotenv_required])
    assert "env" in str(dotenv_exc.value)


def test_native_toml_time_must_be_finite_nonnegative(tmp_path: Path) -> None:
    path = tmp_path / "cfg.toml"
    for raw in ("timeout_s = -1\n", "timeout_s = nan\n", "timeout_s = inf\n"):
        path.write_text(raw)
        with pytest.raises(ConfigError, match="timeout_s") as exc:
            load_cfg(toml_path=path)
        assert "toml" in str(exc.value)


def test_toml_path_rejects_number(tmp_path: Path) -> None:
    path = tmp_path / "cfg.toml"
    path.write_text("out = 123\n")
    with pytest.raises(ConfigError, match="out") as exc:
        load_cfg(toml_path=path)
    assert "toml" in str(exc.value)


def test_extra_frameweave_env_is_ignored() -> None:
    cfg = load_cfg(
        env={
            "FRAMEWEAVE_SCRUB_TERMS": "/tmp/terms.txt",
            "FRAMEWEAVE_KICKOFF_DIR": "/tmp/kickoff",
        }
    )
    assert cfg.frames_per_call == 8


def test_slugify() -> None:
    assert slugify("") == "untitled"
    assert slugify("!!!") == "untitled"
    assert slugify("AI Search") == "ai-search"
    assert slugify("NetworkChuck") == "networkchuck"
    assert slugify("Nick Saraev") == "nick-saraev"
    assert slugify("a" * 70 + "-" + "b" * 30) == "a" * 70
    assert slugify("a" * 90) == "a" * 80
    assert slugify(THEO) != "theo"


def test_channel_slug_matches_existing_folders(tmp_root: Path) -> None:
    cfg = load_cfg(flags={"channels": {THEO: "theo"}})
    assert channel_slug(THEO, cfg, tmp_root) == "theo"
    assert channel_slug("NetworkChuck", cfg, tmp_root) == "network-chuck"
    assert channel_slug("AI Search", cfg, tmp_root) == "ai-search"
    assert channel_slug("Nick Saraev", cfg, tmp_root) == "nicksaraev"
    assert channel_slug("Todd's Garage", cfg, tmp_root) == "todds-garage"
    assert channel_slug(THEO, load_cfg(), tmp_root) != "theo"


def test_output_dir_layout(tmp_path: Path) -> None:
    root = tmp_path / "root"
    assert output_dir(root, "ai-search", "my-video") == root / "ai-search" / "my-video"
    assert (
        output_dir(root, "ai-search", "my-video", "ch-01")
        == root / "ai-search" / "my-video" / "ch-01"
    )


def test_output_dir_out_override(tmp_path: Path) -> None:
    root = tmp_path / "root"
    override = tmp_path / "exact-out"
    assert output_dir(root, "ai-search", "my-video", out_override=override) == override
    assert output_dir(root, "ai-search", "my-video", "ch-01", override) == override


def test_run_key_identical_inputs_match() -> None:
    key = run_key(resolved(), "full")
    assert run_key(resolved(), "full") == key
    assert len(key) == 12
    assert all(char in "0123456789abcdef" for char in key)


def test_run_key_rejects_auto_lane() -> None:
    with pytest.raises(ConfigError) as exc:
        run_key(load_cfg(), "full")
    assert str(exc.value) == AUTO_LANE_MSG


def test_run_key_codex_model_changes_key() -> None:
    base = run_key(resolved(), "full")
    changed = run_key(resolved({"vision_model": {"codex": "gpt-5.6-luna"}}), "full")
    assert changed != base


def test_run_key_changes_with_listed_fields(tmp_path: Path) -> None:
    base = run_key(resolved(), "full")
    glossary_a = tmp_path / "a.txt"
    glossary_b = tmp_path / "b.txt"
    glossary_a.write_text("one")
    glossary_b.write_text("two")
    assert run_key(resolved({"vision_lane": "claude"}), "full") != base
    assert run_key(resolved({"vision_model": {"codex": "gpt-5.6-luna"}}), "full") != base
    assert run_key(resolved({"vision_quality": "high"}), "full") != base
    assert run_key(resolved({"frames_per_call": 4}), "full") != base
    assert run_key(resolved({"frame_interval_s": 15.0}), "full") != base
    assert run_key(resolved({"frame_width": 640}), "full") != base
    assert run_key(resolved({"max_frames": 40}), "full") != base
    assert run_key(resolved({"stt_backend": "hosted"}), "full") != base
    assert run_key(resolved({"stt_model": "large-v3"}), "full") != base
    assert run_key(resolved({"speakers": True}), "full") != base
    assert run_key(resolved({"prompt_revision": "2"}), "full") != base
    assert run_key(resolved({"glossary": glossary_a}), "full") != base
    assert run_key(resolved({"glossary": glossary_a}), "full") != run_key(
        resolved({"glossary": glossary_b}), "full"
    )
    assert run_key(resolved(), "0:00-1:00") != base


def test_run_key_ignores_non_artifact_fields(tmp_path: Path) -> None:
    base = run_key(resolved(), "full")
    assert run_key(resolved({"timeout_s": 30.0}), "full") == base
    assert run_key(resolved({"concurrency": 8}), "full") == base
    assert run_key(resolved({"out": tmp_path}), "full") == base
    assert run_key(resolved({"cache_dir": tmp_path / "c"}), "full") == base
    assert run_key(resolved({"lane_skip_percent": 50}), "full") == base
    assert run_key(resolved({"disk_warn_gb": 1}), "full") == base


def test_cli_flags() -> None:
    flags = cli_flags()
    by_name = {item.name: item for item in flags}
    assert set(by_name) == {
        "--out",
        "--vision",
        "--vision-quality",
        "--frames-per-call",
        "--frame-interval",
        "--max-frames",
        "--speakers",
        "--timeout",
        "--glossary",
    }
    assert by_name["--vision"].dest == "vision_lane"
    assert by_name["--out"].type is Path
    assert by_name["--speakers"].type is bool
    assert by_name["--timeout"].dest == "timeout_s"
    assert all(item.default is None for item in flags)


def test_preflight_checks(tmp_path: Path) -> None:
    cache = tmp_path / "cache"
    missing = {item.name: item for item in preflight_checks(load_cfg(flags={"cache_dir": cache}))}
    assert missing["output root"].ok is False
    assert missing["output root"].remedy == MISSING_OUT
    assert missing["output root"].required is True
    assert missing["cache dir"].ok is True
    out = tmp_path / "out"
    out.mkdir()
    ok = preflight_checks(load_cfg(flags={"out": out, "cache_dir": cache}))
    assert all(item.ok for item in ok)
    assert all(item.required for item in ok)


def test_env_example_loads_cleanly(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The template must be copyable to .env as-is (Sol review, PR #31)."""
    example = Path(__file__).resolve().parents[1] / ".env.example"
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "nohome")
    cfg = load(flags={}, env={}, toml_path=tmp_path / "none.toml", dotenv_paths=[example])
    assert cfg.out is not None
    assert cfg.frames_per_call == 8
    assert cfg.channels == {}


def test_captions_mode_and_vision_effort_are_fields(tmp_path: Path) -> None:
    cfg = load(
        flags={"captions_mode": "manual"},
        env={"FRAMEWEAVE_VISION_EFFORT": "medium"},
        toml_path=tmp_path / "none.toml",
        dotenv_paths=[],
    )
    assert cfg.captions_mode == "manual"
    assert cfg.vision_effort == "medium"
    base = load(flags={}, env={}, toml_path=tmp_path / "none.toml", dotenv_paths=[])
    assert (base.captions_mode, base.vision_effort) == ("auto", "low")
