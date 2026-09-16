"""Launcher: --version today; missing cli.py exits 2."""

from __future__ import annotations

import subprocess
import sys


def test_module_version() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "frameweave", "--version"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0
    assert result.stdout == "frameweave 0.1.0\n"


def test_missing_cli_exits_2() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "frameweave"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 2
    assert result.stderr == (
        "frameweave: commands arrive with issue #15; only --version works today\n"
    )
