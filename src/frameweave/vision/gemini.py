"""Metered Gemini API vision backend."""

from __future__ import annotations

import os
import time
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from google import genai
from google.genai import types

from frameweave.types import Description, Frame, Usage
from frameweave.util.retry import (
    Outcome,
    RequestFailed,
    RequestTimeout,
    RetryExhausted,
    call,
)
from frameweave.vision.base import (
    BadReply,
    VisionFailed,
    failure_message,
    parse_json_list,
    render_prompt,
    validate,
)

if TYPE_CHECKING:
    from frameweave.config import Config

RATES_USD_PER_MILLION: dict[str, tuple[float, float]] = {
    "gemini-3.5-flash-lite": (0.30, 2.50),
    "gemini-3.6-flash": (0.75, 3.75),
}


class GeminiBackend:
    name = "gemini"

    def __init__(
        self,
        model: str,
        *,
        client: Any | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.model = model
        self._client = client
        self._sleep = sleep
        self._clock = clock

    def describe(
        self,
        batch: list[Frame],
        context: str,
        frames_dir: Path,
        config: Config,
    ) -> tuple[list[Description], Usage]:
        if not batch:
            return [], Usage()
        client = self._client or self._make_client(config)
        parts = [
            types.Part.from_bytes(
                data=_frame_path(frame, frames_dir).read_bytes(), mime_type="image/jpeg"
            )
            for frame in batch
        ]
        parts.append(types.Part.from_text(text=render_prompt(len(batch), context)))
        request_config = types.GenerateContentConfig(response_mime_type="application/json")
        bad_replies = 0
        calls = 0
        spent = Usage()
        last_detail = "request failed"
        started = self._clock()

        def invoke() -> tuple[list[Description], Usage]:
            nonlocal calls, spent
            calls += 1
            response = client.models.generate_content(
                model=self.model,
                contents=[types.Content(role="user", parts=parts)],
                config=request_config,
            )
            usage = _usage(response, self.model)
            spent = spent + usage
            parsed = parse_json_list(response.text or "")
            descriptions = validate(batch, parsed, source=f"{self.name}:{self.model}")
            return descriptions, spent

        def classify(value: object) -> Outcome:
            nonlocal bad_replies, last_detail
            if isinstance(value, BadReply):
                last_detail = str(value) or "bad reply"
                bad_replies += 1
                return Outcome("retry" if bad_replies == 1 else "fail")
            if isinstance(value, Exception):
                last_detail = str(value) or value.__class__.__name__
                status = _status_code(value)
                if status == 429 or status is not None and 500 <= status <= 599:
                    return Outcome("retry", retry_after=_exception_retry_after(value))
                return Outcome("fail")
            return Outcome("ok")

        try:
            descriptions, usage = call(
                invoke,
                attempts=2,
                timeout_s=config.timeout_s,
                classify=classify,
                sleep=self._sleep,
                frames_per_call=config.frames_per_call,
            )
        except (RequestFailed, RequestTimeout, RetryExhausted) as exc:
            raise VisionFailed(failure_message(self.name, last_detail, config)) from exc

        return descriptions, Usage(
            calls=calls,
            tokens_in=usage.tokens_in,
            tokens_out=usage.tokens_out,
            tokens_reasoning=usage.tokens_reasoning,
            seconds=self._clock() - started,
            usd=usage.usd,
            unknown_usd=usage.unknown_usd,
        )

    def _make_client(self, config: Config) -> genai.Client:
        api_key = os.environ.get("GEMINI_API_KEY")
        if not api_key:
            raise VisionFailed("GEMINI_API_KEY is required for --vision gemini")
        timeout_ms = int(config.timeout_s * 1000)
        return genai.Client(api_key=api_key, http_options=types.HttpOptions(timeout=timeout_ms))


def _usage(response: Any, model: str) -> Usage:
    metadata = response.usage_metadata
    tokens_in = _integer(getattr(metadata, "prompt_token_count", 0))
    tokens_out = _integer(getattr(metadata, "candidates_token_count", 0))
    reasoning = _integer(getattr(metadata, "thoughts_token_count", 0))
    rate = RATES_USD_PER_MILLION.get(model)
    if rate is None:
        return Usage(
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            tokens_reasoning=reasoning,
            unknown_usd=True,
        )
    usd = (tokens_in * rate[0] + (tokens_out + reasoning) * rate[1]) / 1_000_000
    return Usage(
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        tokens_reasoning=reasoning,
        usd=usd,
    )


def _frame_path(frame: Frame, frames_dir: Path) -> Path:
    path = Path(frame.path)
    return path if path.is_absolute() else frames_dir / path.name


def _status_code(exc: Exception) -> int | None:
    for name in ("status_code", "code"):
        value = getattr(exc, name, None)
        if isinstance(value, int) and not isinstance(value, bool):
            return value
        if callable(value):
            called = value()
            if isinstance(called, int) and not isinstance(called, bool):
                return called
    return None


def _exception_retry_after(exc: Exception) -> float | None:
    response = getattr(exc, "response", None)
    headers = getattr(response, "headers", None)
    if headers is None:
        return None
    value = headers.get("Retry-After")
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _integer(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0
