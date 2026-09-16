"""Sniff media kind from magic bytes. No ffprobe."""

from __future__ import annotations

from enum import Enum
from pathlib import Path

_READ = 65_536
_FTYP = b"ftyp"
_EBML = b"\x1a\x45\xdf\xa3"
_PNG = b"\x89PNG"
_JPEG = b"\xff\xd8\xff"
_ID3 = b"ID3"
_RIFF = b"RIFF"
_WAVE = b"WAVE"
_M4A_BRANDS = {b"M4A ", b"M4B ", b"M4P "}
_DOCTYPE_ID = 0x4282


class MediaKind(Enum):
    mp4 = "mp4"
    webm = "webm"
    mkv = "mkv"
    mp3 = "mp3"
    m4a = "m4a"
    wav = "wav"
    jpeg = "jpeg"
    png = "png"


class NotMediaError(ValueError):
    """Bytes are not a recognized media container."""


def sniff(path_or_bytes: str | Path | bytes | bytearray | memoryview) -> MediaKind:
    """Return the media kind, or raise ``NotMediaError`` naming the first 8 bytes."""
    data = _head(path_or_bytes)
    kind = _kind(data)
    if kind is None:
        prefix = data[:8].hex()
        raise NotMediaError(f"not media (first 8 bytes: {prefix})")
    return kind


def _head(path_or_bytes: str | Path | bytes | bytearray | memoryview) -> bytes:
    if isinstance(path_or_bytes, (bytes, bytearray, memoryview)):
        return bytes(path_or_bytes[:_READ])
    with Path(path_or_bytes).open("rb") as fh:
        return fh.read(_READ)


def _kind(data: bytes) -> MediaKind | None:
    if not data:
        return None
    if data.startswith(_PNG):
        return MediaKind.png
    if data.startswith(_JPEG):
        return MediaKind.jpeg
    if len(data) >= 12 and data.startswith(_RIFF) and data[8:12] == _WAVE:
        return MediaKind.wav
    if data.startswith(_ID3) or _mpeg_frame_sync(data):
        return MediaKind.mp3
    if len(data) >= 8 and data[4:8] == _FTYP:
        return _mp4_or_m4a(data)
    if data.startswith(_EBML):
        return _webm_or_mkv(data)
    return None


def _mp4_or_m4a(data: bytes) -> MediaKind:
    size = int.from_bytes(data[0:4], "big")
    end = size if 16 <= size <= len(data) else len(data)
    brands: list[bytes] = []
    if len(data) >= 12:
        brands.append(data[8:12])
    for offset in range(16, end - 3, 4):
        brands.append(data[offset : offset + 4])
    if any(brand in _M4A_BRANDS for brand in brands):
        return MediaKind.m4a
    return MediaKind.mp4


def _mpeg_frame_sync(data: bytes) -> bool:
    return len(data) >= 2 and data[0] == 0xFF and (data[1] & 0xE0) == 0xE0


def _webm_or_mkv(data: bytes) -> MediaKind | None:
    doctype = _ebml_doctype(data)
    if doctype == "webm":
        return MediaKind.webm
    if doctype == "matroska":
        return MediaKind.mkv
    return None


def _ebml_doctype(data: bytes) -> str | None:
    if len(data) < 5:
        return None
    offset = 4  # skip EBML ID 1A45DFA3
    size, offset = _read_vint(data, offset)
    if size is None or offset is None:
        return None
    end = min(len(data), offset + size)
    while offset < end:
        element_id, offset = _read_id(data, offset)
        if element_id is None or offset is None:
            return None
        payload_size, offset = _read_vint(data, offset)
        if payload_size is None or offset is None:
            return None
        payload_end = offset + payload_size
        if payload_end > len(data):
            return None
        if element_id == _DOCTYPE_ID:
            return data[offset:payload_end].decode("ascii", errors="replace")
        offset = payload_end
    return None


def _read_id(data: bytes, offset: int) -> tuple[int | None, int | None]:
    if offset >= len(data) or data[offset] == 0:
        return None, None
    length = _vint_length(data[offset])
    if length is None or offset + length > len(data):
        return None, None
    return int.from_bytes(data[offset : offset + length], "big"), offset + length


def _read_vint(data: bytes, offset: int) -> tuple[int | None, int | None]:
    if offset >= len(data) or data[offset] == 0:
        return None, None
    length = _vint_length(data[offset])
    if length is None or offset + length > len(data):
        return None, None
    marker = 0x80 >> (length - 1)
    value = data[offset] & (marker - 1)
    for i in range(1, length):
        value = (value << 8) | data[offset + i]
    return value, offset + length


def _vint_length(first: int) -> int | None:
    for length in range(1, 9):
        if first & (0x80 >> (length - 1)):
            return length
    return None
