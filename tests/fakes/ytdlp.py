"""Scripted yt-dlp runner for YouTubeSource tests.

Selectable via ``YouTubeSource(runner=FakeYtDlp(...))``. Scripts common failure
modes offline: 403 on early player clients, leftover ``.part`` files, and an
HTML body instead of media.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path


def _client_from_args(args: list[str]) -> str | None:
    for i, arg in enumerate(args):
        if arg == "--extractor-args" and i + 1 < len(args):
            value = args[i + 1]
            prefix = "youtube:player_client="
            if prefix in value:
                return value.split(prefix, 1)[1].split(",", 1)[0]
    return None


def _output_template(args: list[str]) -> str | None:
    for i, arg in enumerate(args):
        if arg == "-o" and i + 1 < len(args):
            return args[i + 1]
    return None


def _is_resolve(args: list[str]) -> bool:
    return "-J" in args


class FakeYtDlp:
    """Callable runner that records argv and follows a small script.

    ``mode`` values:
    - ``chain_403``: 403 for android and mweb, success on web (writes ``media_path``)
    - ``leave_part``: writes a ``.part`` then succeeds (so cleanup can be asserted)
    - ``html_body``: writes an HTML file as ``media.mp4``
    - ``resolve_only``: resolve from ``resolve_json``; downloads fail
    """

    def __init__(
        self,
        *,
        mode: str = "chain_403",
        resolve_json: dict | Path | None = None,
        media_path: Path | None = None,
    ) -> None:
        self.mode = mode
        self.calls: list[list[str]] = []
        self.resolve_json = resolve_json
        self.media_path = media_path

    def __call__(self, args: list[str]) -> subprocess.CompletedProcess[str]:
        self.calls.append(list(args))
        if _is_resolve(args):
            return self._resolve()
        return self._download(args)

    def _resolve(self) -> subprocess.CompletedProcess[str]:
        if self.resolve_json is None:
            return subprocess.CompletedProcess(
                args=[], returncode=1, stdout="", stderr="no resolve fixture"
            )
        if isinstance(self.resolve_json, Path):
            payload = self.resolve_json.read_text()
        else:
            payload = json.dumps(self.resolve_json)
        return subprocess.CompletedProcess(
            args=[], returncode=0, stdout=payload, stderr=""
        )

    def _download(self, args: list[str]) -> subprocess.CompletedProcess[str]:
        client = _client_from_args(args) or "unknown"
        outtmpl = _output_template(args)
        if outtmpl is None:
            return subprocess.CompletedProcess(
                args=args, returncode=1, stdout="", stderr="missing -o"
            )
        dest = Path(outtmpl.replace("%(ext)s", "mp4"))
        dest.parent.mkdir(parents=True, exist_ok=True)

        if self.mode == "chain_403":
            if client in {"android", "mweb"}:
                return subprocess.CompletedProcess(
                    args=args,
                    returncode=1,
                    stdout="",
                    stderr=f"ERROR: [youtube] HTTP Error 403: Forbidden ({client})",
                )
            return self._write_success(dest, client)

        if self.mode == "leave_part":
            part = dest.with_suffix(dest.suffix + ".part")
            part.write_bytes(b"partial")
            if client in {"android", "mweb"}:
                return subprocess.CompletedProcess(
                    args=args,
                    returncode=1,
                    stdout="",
                    stderr="ERROR: HTTP Error 403: Forbidden",
                )
            result = self._write_success(dest, client)
            # Leave a sibling .part to prove post-attempt cleanup.
            leftover = dest.parent / "stray.part"
            leftover.write_bytes(b"stray")
            return result

        if self.mode == "html_body":
            dest.write_text("<html><body>not media</body></html>\n")
            info = {
                "format_id": "html",
                "ext": "mp4",
                "id": "html",
            }
            return subprocess.CompletedProcess(
                args=args,
                returncode=0,
                stdout=json.dumps(info),
                stderr="",
            )

        return subprocess.CompletedProcess(
            args=args, returncode=1, stdout="", stderr=f"unknown mode {self.mode}"
        )

    def _write_success(self, dest: Path, client: str) -> subprocess.CompletedProcess[str]:
        if self.media_path is None or not self.media_path.exists():
            return subprocess.CompletedProcess(
                args=[], returncode=1, stdout="", stderr="no media fixture"
            )
        shutil.copyfile(self.media_path, dest)
        info = {
            "format_id": f"fake-{client}",
            "ext": "mp4",
            "id": "fake",
            "title": "fake",
        }
        (dest.parent / "media.info.json").write_text(json.dumps(info) + "\n")
        return subprocess.CompletedProcess(
            args=[], returncode=0, stdout=json.dumps(info), stderr=""
        )
