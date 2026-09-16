"""Recorded-shape tests for the Claude Code and Codex subscription lanes."""

from __future__ import annotations

import argparse
import json
import subprocess
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from frameweave.config import load
from frameweave.types import Frame
from frameweave.vision import VisionFailed, make_backend
from frameweave.vision import base as vision_base
from frameweave.vision.cli import CliBackend
from frameweave.vision.gemini import GeminiBackend
from tests.fakes.vision import ScriptedRunner

RECORDED = Path(__file__).parent / "recorded" / "vision"
MISSING_TOML = Path("/nonexistent/frameweave-test/config.toml")


def config(**overrides: object) -> SimpleNamespace:
    values: dict[str, object] = {
        "timeout_s": 12.0,
        "frames_per_call": 3,
        "vision_effort": "low",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def frames(n: int) -> list[Frame]:
    return [
        Frame(id=f"f{index:04d}", time=float(index), kind="primary", path=f"f{index:04d}.jpg")
        for index in range(1, n + 1)
    ]


def completed(stdout: str = "", stderr: str = "", returncode: int = 0):
    return subprocess.CompletedProcess([], returncode, stdout=stdout, stderr=stderr)


def test_claude_exact_argv_normal_batch_and_usage(tmp_path: Path) -> None:
    envelope = (RECORDED / "claude-normal.json").read_text()
    runner = ScriptedRunner([completed(stdout=envelope)])
    backend = CliBackend("claude", "sonnet", runner=runner, clock=iter((10.0, 12.5)).__next__)

    descriptions, usage = backend.describe(frames(3), "the queue is draining", tmp_path, config())

    argv, kwargs = runner.calls[0]
    assert argv[:12] == [
        "claude",
        "-p",
        "--model",
        "sonnet",
        "--allowedTools",
        "Read",
        "--tools",
        "Read",
        "--output-format",
        "json",
        "--strict-mcp-config",
        "--mcp-config",
    ]
    mcp_path = Path(argv[12])
    assert argv[13:20] == [
        "--disable-slash-commands",
        "--setting-sources",
        "",
        "--add-dir",
        str(tmp_path.resolve()),
        "--system-prompt",
        "You read image files with the Read tool and answer with JSON only.",
    ]
    assert all(str((tmp_path / f"f{i:04d}.jpg").resolve()) in argv[-1] for i in range(1, 4))
    assert "for disambiguation only, never a source of on-screen text" in argv[-1]
    assert kwargs["cwd"] == tmp_path
    assert kwargs["stdin"] is subprocess.DEVNULL
    assert kwargs["timeout"] == 12.0
    assert mcp_path.parent != tmp_path.resolve()
    assert str(tempfile.gettempdir()) in str(mcp_path.resolve())
    assert json.loads(mcp_path.read_text()) == {"mcpServers": {}}
    assert not (tmp_path / ".frameweave-empty-mcp.json").exists()
    assert [item.frame_id for item in descriptions] == ["f0001", "f0002", "f0003"]
    assert descriptions[2].strings == ["12 pending"]
    assert descriptions[2].source == "claude:sonnet"
    assert usage.calls == 1
    assert usage.tokens_in == 125
    assert usage.tokens_out == 30
    assert usage.seconds == 2.5
    assert usage.usd == 0


def test_codex_exact_argv_closed_stdin_and_token_tail(tmp_path: Path) -> None:
    fixture = json.loads((RECORDED / "codex-normal.json").read_text())
    runner = ScriptedRunner([completed(stdout=fixture["stdout"], stderr=fixture["stderr"])])
    backend = CliBackend("codex", "gpt-5.6-sol", runner=runner)

    descriptions, usage = backend.describe(frames(1), "", tmp_path, config())

    argv, kwargs = runner.calls[0]
    assert argv[:9] == [
        "codex",
        "exec",
        "--skip-git-repo-check",
        "--sandbox",
        "read-only",
        "-m",
        "gpt-5.6-sol",
        "-c",
        'model_reasoning_effort="low"',
    ]
    assert argv[9:11] == ["-i", str((tmp_path / "f0001.jpg").resolve())]
    assert argv[11] == "--"
    assert kwargs["stdin"] is subprocess.DEVNULL
    assert descriptions[0].source == "codex:gpt-5.6-sol"
    assert usage.tokens_in == 1234
    assert usage.tokens_out == 0
    assert usage.calls == 1


def test_quota_retries_once_and_honors_retry_after(tmp_path: Path) -> None:
    fixture = json.loads((RECORDED / "codex-normal.json").read_text())
    runner = ScriptedRunner(
        [
            completed(stderr="429 rate limit\nRetry-After: 7", returncode=1),
            completed(stdout=fixture["stdout"], stderr=fixture["stderr"]),
        ]
    )
    sleeps: list[float] = []
    backend = CliBackend("codex", "gpt-5.6-sol", runner=runner, sleep=sleeps.append)

    _, usage = backend.describe(frames(1), "", tmp_path, config())

    assert len(runner.calls) == 2
    assert sleeps == [7.0]
    assert usage.calls == 2


def test_three_quota_failures_raise_vision_failed_with_remedies(tmp_path: Path) -> None:
    runner = ScriptedRunner(
        [completed(stderr="quota exceeded for project", returncode=1) for _ in range(3)]
    )
    backend = CliBackend("claude", "sonnet", runner=runner, sleep=lambda _: None)

    with pytest.raises(VisionFailed) as raised:
        backend.describe(frames(1), "", tmp_path, config())

    message = str(raised.value)
    assert len(runner.calls) == 3
    assert "quota exceeded for project" in message
    assert "--timeout" in message
    assert "--vision-quality standard" in message
    assert "--frames-per-call" in message


def test_vision_failed_carries_stderr_detail(tmp_path: Path) -> None:
    runner = ScriptedRunner([completed(stderr="unknown option --bogus", returncode=2)])
    backend = CliBackend("claude", "sonnet", runner=runner)

    with pytest.raises(VisionFailed) as raised:
        backend.describe(frames(1), "", tmp_path, config())

    assert "unknown option --bogus" in str(raised.value)


def test_vision_failed_carries_bad_reply_reason(tmp_path: Path) -> None:
    envelope = json.dumps({"result": "I cannot read that image", "usage": {}})
    runner = ScriptedRunner([completed(stdout=envelope), completed(stdout=envelope)])
    backend = CliBackend("claude", "sonnet", runner=runner, sleep=lambda _: None)

    with pytest.raises(VisionFailed) as raised:
        backend.describe(frames(1), "", tmp_path, config())

    assert "reply did not contain a JSON list" in str(raised.value)


def test_bad_reply_retries_accumulate_tokens(tmp_path: Path) -> None:
    first = json.dumps(
        {
            "result": "not json",
            "usage": {
                "input_tokens": 40,
                "cache_read_input_tokens": 10,
                "cache_creation_input_tokens": 0,
                "output_tokens": 5,
            },
        }
    )
    second = (RECORDED / "claude-normal.json").read_text()
    runner = ScriptedRunner([completed(stdout=first), completed(stdout=second)])
    backend = CliBackend("claude", "sonnet", runner=runner, sleep=lambda _: None)

    _, usage = backend.describe(frames(3), "", tmp_path, config())

    assert usage.calls == 2
    assert usage.tokens_in == 50 + 125
    assert usage.tokens_out == 5 + 30


def test_timeout_is_terminal_and_names_remedies(tmp_path: Path) -> None:
    timeout = subprocess.TimeoutExpired(["codex"], 12)
    runner = ScriptedRunner([timeout])
    backend = CliBackend("codex", "gpt-5.6-sol", runner=runner)

    with pytest.raises(VisionFailed, match="--timeout"):
        backend.describe(frames(1), "", tmp_path, config())

    assert len(runner.calls) == 1


@pytest.mark.parametrize(
    "reply",
    [
        '[{"frame":1,"description":"one","text":[]}]',
        '[{"frame":1,"description":"one","text":[]},{"frame":1,"description":"two","text":[]}]',
        '[{"frame":2,"description":"two","text":[]},{"frame":1,"description":"one","text":[]}]',
    ],
)
def test_bad_indices_retry_once_then_fail(reply: str, tmp_path: Path) -> None:
    envelope = json.dumps({"result": reply, "usage": {}})
    runner = ScriptedRunner([completed(stdout=envelope), completed(stdout=envelope)])
    backend = CliBackend("claude", "sonnet", runner=runner, sleep=lambda _: None)

    with pytest.raises(VisionFailed):
        backend.describe(frames(2), "", tmp_path, config())

    assert len(runner.calls) == 2


def test_zero_exit_without_json_retries_once_then_fails(tmp_path: Path) -> None:
    envelope = json.dumps({"result": "I cannot read that image", "usage": {}})
    runner = ScriptedRunner([completed(stdout=envelope), completed(stdout=envelope)])
    backend = CliBackend("claude", "sonnet", runner=runner, sleep=lambda _: None)

    with pytest.raises(VisionFailed):
        backend.describe(frames(1), "", tmp_path, config())

    assert len(runner.calls) == 2


def test_non_quota_nonzero_exit_does_not_retry(tmp_path: Path) -> None:
    runner = ScriptedRunner([completed(stderr="unknown option", returncode=2)])
    backend = CliBackend("claude", "sonnet", runner=runner)

    with pytest.raises(VisionFailed):
        backend.describe(frames(1), "", tmp_path, config())

    assert len(runner.calls) == 1


def test_codex_refuses_empty_model() -> None:
    with pytest.raises(ValueError, match="explicit model"):
        CliBackend("codex", "")


def test_cli_flags_apply_the_specified_choices() -> None:
    flags = {flag.name: flag for flag in vision_base.cli_flags()}

    assert flags["--vision"].type("gemini") == "gemini"
    with pytest.raises(argparse.ArgumentTypeError):
        flags["--vision"].type("other")
    assert flags["--vision-quality"].type("high") == "high"
    with pytest.raises(argparse.ArgumentTypeError):
        flags["--vision-quality"].type("low")
    assert flags["--frames-per-call"].type is int


def test_cli_flags_and_checks_use_keyword_construction() -> None:
    flags = vision_base.cli_flags()
    assert all(hasattr(flag, "name") and hasattr(flag, "dest") for flag in flags)
    assert flags[0].name == "--vision"
    assert flags[0].dest == "vision_lane"

    checks = vision_base.preflight_checks(config(vision_lane="none"))
    assert all(hasattr(check, "name") and hasattr(check, "remedy") for check in checks)


def test_preflight_reports_versions_and_key_requirement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(vision_base.shutil, "which", lambda name: f"/bin/{name}")
    monkeypatch.setattr(
        vision_base.subprocess,
        "run",
        lambda argv, **kwargs: completed(stdout=f"{Path(argv[0]).name} 1.2.3\n"),
    )
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)

    checks = {
        check.name: check
        for check in vision_base.preflight_checks(config(vision_lane="gemini"))
    }

    assert checks["claude"].ok is True
    assert checks["claude"].detail == "claude 1.2.3"
    assert checks["codex"].ok is True
    assert checks["GEMINI_API_KEY"].ok is False
    assert checks["GEMINI_API_KEY"].required is True
    assert "Export GEMINI_API_KEY" in checks["GEMINI_API_KEY"].remedy


def test_make_backend_all_four_lanes() -> None:
    cfg = load(
        {"vision_lane": "none"},
        env={},
        toml_path=MISSING_TOML,
        dotenv_paths=[],
    )
    none = make_backend("none", cfg)
    assert none.name == "none"
    assert isinstance(make_backend("claude", cfg), CliBackend)
    assert isinstance(make_backend("codex", cfg), CliBackend)
    assert isinstance(make_backend("gemini", cfg), GeminiBackend)
    with pytest.raises(ValueError, match="unknown vision lane"):
        make_backend("other", cfg)


def test_failure_message_uses_standard_quality() -> None:
    message = vision_base.failure_message("claude", "boom", config())
    assert "--vision-quality standard" in message
    assert "--vision-quality low" not in message
