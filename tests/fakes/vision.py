"""Offline scripted fakes for the vision provider seams."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from typing import Any


class ScriptedRunner:
    """A subprocess.run-compatible callable that records and consumes outcomes."""

    def __init__(self, outcomes: list[subprocess.CompletedProcess[str] | Exception]) -> None:
        self.outcomes = list(outcomes)
        self.calls: list[tuple[list[str], dict[str, Any]]] = []

    def __call__(self, argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        self.calls.append((argv, kwargs))
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


@dataclass
class FakeUsageMetadata:
    prompt_token_count: int = 0
    candidates_token_count: int = 0
    thoughts_token_count: int = 0


@dataclass
class FakeGeminiResponse:
    text: str
    usage_metadata: FakeUsageMetadata


class FakeAPIError(Exception):
    def __init__(self, status_code: int, retry_after: str | None = None) -> None:
        super().__init__(f"API status {status_code}")
        self.status_code = status_code
        self.response = _FakeResponse(retry_after)


class _FakeResponse:
    def __init__(self, retry_after: str | None) -> None:
        self.headers = {} if retry_after is None else {"Retry-After": retry_after}


class FakeModels:
    def __init__(self, outcomes: list[FakeGeminiResponse | Exception]) -> None:
        self.outcomes = list(outcomes)
        self.calls: list[dict[str, Any]] = []

    def generate_content(self, **kwargs: Any) -> FakeGeminiResponse:
        self.calls.append(kwargs)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class FakeGenAIClient:
    def __init__(self, outcomes: list[FakeGeminiResponse | Exception]) -> None:
        self.models = FakeModels(outcomes)
