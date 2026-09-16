"""Launcher: --version and dispatch to cli.main."""

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


def test_no_command_exits_nonzero() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "frameweave"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 1
