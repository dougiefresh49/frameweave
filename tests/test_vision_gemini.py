"""Recorded-shape tests for the metered Gemini API vision lane."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from google.genai import types

from frameweave.types import Frame
from frameweave.vision import VisionFailed
from frameweave.vision.gemini import GeminiBackend
from tests.fakes.vision import (
    FakeAPIError,
    FakeGeminiResponse,
    FakeGenAIClient,
    FakeUsageMetadata,
)

RECORDED = Path(__file__).parent / "recorded" / "vision"


def config() -> SimpleNamespace:
    return SimpleNamespace(timeout_s=15.0, frames_per_call=2)


def frame(index: int) -> Frame:
    return Frame(id=f"f{index:04d}", time=float(index), kind="primary", path=f"f{index:04d}.jpg")


def response(text: str | None = None) -> FakeGeminiResponse:
    fixture = json.loads((RECORDED / "gemini-normal.json").read_text())
    raw_usage = fixture["usage_metadata"]
    return FakeGeminiResponse(
        text=fixture["text"] if text is None else text,
        usage_metadata=FakeUsageMetadata(**raw_usage),
    )


def write_jpeg(path: Path, marker: bytes) -> None:
    path.write_bytes(b"\xff\xd8" + marker + b"\xff\xd9")


def test_request_shape_usage_and_dollars(tmp_path: Path) -> None:
    write_jpeg(tmp_path / "f0001.jpg", b"one")
    client = FakeGenAIClient([response()])
    backend = GeminiBackend(
        "gemini-3.5-flash-lite", client=client, clock=iter((2.0, 3.25)).__next__
    )

    descriptions, usage = backend.describe([frame(1)], "queue discussion", tmp_path, config())

    request = client.models.calls[0]
    assert request["model"] == "gemini-3.5-flash-lite"
    assert isinstance(request["config"], types.GenerateContentConfig)
    assert request["config"].response_mime_type == "application/json"
    content = request["contents"][0]
    assert content.role == "user"
    assert content.parts[0].inline_data.mime_type == "image/jpeg"
    assert content.parts[0].inline_data.data == b"\xff\xd8one\xff\xd9"
    assert "queue discussion" in content.parts[-1].text
    assert descriptions[0].source == "gemini:gemini-3.5-flash-lite"
    assert usage.calls == 1
    assert usage.tokens_in == 1000
    assert usage.tokens_out == 100
    assert usage.tokens_reasoning == 20
    assert usage.seconds == 1.25
    assert usage.usd == pytest.approx((1000 * 0.30 + 120 * 2.50) / 1_000_000)
    assert usage.unknown_usd is False


def test_429_retries_and_honors_retry_after(tmp_path: Path) -> None:
    write_jpeg(tmp_path / "f0001.jpg", b"one")
    client = FakeGenAIClient([FakeAPIError(429, "4"), response()])
    sleeps: list[float] = []
    backend = GeminiBackend("gemini-3.6-flash", client=client, sleep=sleeps.append)

    descriptions, usage = backend.describe([frame(1)], "", tmp_path, config())

    assert len(descriptions) == 1
    assert len(client.models.calls) == 2
    assert sleeps == [4.0]
    assert usage.calls == 2


def test_400_fails_without_retry(tmp_path: Path) -> None:
    write_jpeg(tmp_path / "f0001.jpg", b"one")
    client = FakeGenAIClient([FakeAPIError(400)])
    backend = GeminiBackend("gemini-3.5-flash-lite", client=client)

    with pytest.raises(VisionFailed):
        backend.describe([frame(1)], "", tmp_path, config())

    assert len(client.models.calls) == 1


def test_shared_json_validation_retries_once(tmp_path: Path) -> None:
    write_jpeg(tmp_path / "f0001.jpg", b"one")
    write_jpeg(tmp_path / "f0002.jpg", b"two")
    reordered = (
        '[{"frame":2,"description":"two","text":[]},'
        '{"frame":1,"description":"one","text":[]}]'
    )
    client = FakeGenAIClient([response(reordered), response(reordered)])
    backend = GeminiBackend("gemini-3.5-flash-lite", client=client, sleep=lambda _: None)

    with pytest.raises(VisionFailed):
        backend.describe([frame(1), frame(2)], "", tmp_path, config())

    assert len(client.models.calls) == 2


def test_unknown_model_marks_dollars_unknown(tmp_path: Path) -> None:
    write_jpeg(tmp_path / "f0001.jpg", b"one")
    client = FakeGenAIClient([response()])
    backend = GeminiBackend("gemini-future", client=client)

    _, usage = backend.describe([frame(1)], "", tmp_path, config())

    assert usage.usd == 0
    assert usage.unknown_usd is True
