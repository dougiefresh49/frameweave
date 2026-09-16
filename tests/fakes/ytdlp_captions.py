"""Offline yt-dlp runner for caption tests."""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class CaptionsRunner:
    manual: Path | None = None
    automatic: Path | None = None
    failure: str | None = None
    commands: list[list[str]] = field(default_factory=list)

    def __call__(self, command: list[str], **kwargs) -> subprocess.CompletedProcess[str]:
        self.commands.append(command)
        if self.failure is not None:
            return subprocess.CompletedProcess(command, 1, "", self.failure)

        output = Path(command[command.index("-o") + 1])
        output.parent.mkdir(parents=True, exist_ok=True)
        if self.manual is not None:
            shutil.copyfile(self.manual, output.with_name("captions.en.manual.vtt"))
        if self.automatic is not None:
            suffix = self.automatic.suffix.lstrip(".")
            shutil.copyfile(self.automatic, output.with_name(f"captions.en.auto.{suffix}"))
        return subprocess.CompletedProcess(command, 0, "", "")
