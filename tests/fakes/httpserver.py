"""Tiny threaded HTTP server for source tests. Serves a generated 1-second mp4 and HTML.

Never commit media; callers pass a tmp_path mp4 from ``make_tiny_mp4``.
"""

from __future__ import annotations

import subprocess
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit


def make_tiny_mp4(dest: Path) -> Path:
    """1-second 64x64 testsrc mp4. Generated on demand, never committed."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "testsrc=duration=1:size=64x64:rate=5",
            "-pix_fmt",
            "yuv420p",
            str(dest),
        ],
        check=True,
    )
    return dest


@dataclass
class FakeHttpServer:
    url: str
    requests: list[str] = field(default_factory=list)
    mp4: bytes = b""
    html: bytes = b"<html><body>challenge</body></html>"


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt: str, *args: object) -> None:
        return

    def do_HEAD(self) -> None:
        self.server.recorder.requests.append(self.requestline)  # type: ignore[attr-defined]
        self._send(write_body=False)

    def do_GET(self) -> None:
        self.server.recorder.requests.append(self.requestline)  # type: ignore[attr-defined]
        self._send(write_body=True)

    def _send(self, *, write_body: bool) -> None:
        recorder: FakeHttpServer = self.server.recorder  # type: ignore[attr-defined]
        path = urlsplit(self.path).path
        if path == "/video.mp4":
            body = recorder.mp4
            content_type = "video/mp4"
            status = 200
        elif path == "/page.html":
            body = recorder.html
            content_type = "text/html; charset=utf-8"
            status = 200
        else:
            body = b"not found"
            content_type = "text/plain"
            status = 404
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if write_body and status != 404:
            self.wfile.write(body)
        elif write_body:
            self.wfile.write(body)


@contextmanager
def serve_media(mp4_path: Path) -> Iterator[FakeHttpServer]:
    recorder = FakeHttpServer(url="", mp4=mp4_path.read_bytes())
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    httpd.recorder = recorder  # type: ignore[attr-defined]
    host, port = httpd.server_address[:2]
    recorder.url = f"http://{host}:{port}"
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield recorder
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=2)
