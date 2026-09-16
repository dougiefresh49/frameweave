"""Fake provider client. Other issues extend this; keep the scripted surface small.

``responses`` is consumed in order. Each item is one of:
``("retry", retry_after)``, ``("ok", value)``, ``("fail",)``, ``("timeout",)``.
``retry_after`` is seconds or ``None``.
"""

from __future__ import annotations

from frameweave.util.retry import Outcome


class FakeClient:
    def __init__(self, responses: list[tuple] | None = None) -> None:
        self.responses = list(responses or [])
        self.calls = 0

    def __call__(self) -> tuple:
        if self.calls >= len(self.responses):
            raise RuntimeError("FakeClient: no scripted responses left")
        item = self.responses[self.calls]
        self.calls += 1
        return item

    @staticmethod
    def classify(exc_or_response: object) -> Outcome:
        if not isinstance(exc_or_response, tuple) or not exc_or_response:
            raise TypeError(f"unscripted value: {exc_or_response!r}")
        kind = exc_or_response[0]
        if kind == "ok":
            return Outcome(status="ok")
        if kind == "retry":
            retry_after = exc_or_response[1] if len(exc_or_response) > 1 else None
            return Outcome(status="retry", retry_after=retry_after)
        if kind == "fail":
            return Outcome(status="fail")
        if kind == "timeout":
            return Outcome(status="timeout")
        raise ValueError(f"unknown scripted kind: {kind!r}")
