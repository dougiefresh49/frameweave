"""Media sniffing from in-test magic-byte headers. No binary fixtures."""

from __future__ import annotations

import pytest

from frameweave.util.media import MediaKind, NotMediaError, sniff


def _ebml(doctype: bytes) -> bytes:
    payload = b"\x42\x82" + bytes([0x80 | len(doctype)]) + doctype
    return b"\x1a\x45\xdf\xa3" + bytes([0x80 | len(payload)]) + payload


def test_sniff_mp4() -> None:
    header = b"\x00\x00\x00\x18ftypisom\x00\x00\x00\x00isomiso2"
    assert sniff(header) is MediaKind.mp4


def test_sniff_m4a() -> None:
    header = b"\x00\x00\x00\x18ftypM4A \x00\x00\x00\x00mp41iso2"
    assert sniff(header) is MediaKind.m4a


def test_sniff_webm() -> None:
    assert sniff(_ebml(b"webm")) is MediaKind.webm


def test_sniff_mkv() -> None:
    assert sniff(_ebml(b"matroska")) is MediaKind.mkv


def test_sniff_mp3_id3() -> None:
    assert sniff(b"ID3\x04\x00\x00\x00\x00\x00\x00") is MediaKind.mp3


def test_sniff_mp3_frame_sync() -> None:
    assert sniff(b"\xff\xfb\x90\x00\x00\x00\x00\x00") is MediaKind.mp3


def test_sniff_wav() -> None:
    header = b"RIFF\x00\x00\x00\x00WAVE"
    assert sniff(header) is MediaKind.wav


def test_sniff_jpeg() -> None:
    assert sniff(b"\xff\xd8\xff\xe0\x00\x10JFIF") is MediaKind.jpeg


def test_sniff_png() -> None:
    assert sniff(b"\x89PNG\r\n\x1a\n") is MediaKind.png


def test_sniff_empty_names_hex_prefix(tmp_path) -> None:
    with pytest.raises(NotMediaError, match="first 8 bytes: ") as exc:
        sniff(b"")
    assert "first 8 bytes: " in str(exc.value)
    empty = tmp_path / "empty.bin"
    empty.write_bytes(b"")
    with pytest.raises(NotMediaError, match="first 8 bytes:"):
        sniff(empty)


def test_sniff_random_bytes_names_hex_prefix() -> None:
    blob = bytes(range(8))
    with pytest.raises(NotMediaError) as exc:
        sniff(blob)
    assert blob[:8].hex() in str(exc.value)
    assert "first 8 bytes:" in str(exc.value)
