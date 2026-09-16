"""Provider-agnostic retries. The caller classifies each result or exception."""

from __future__ import annotations

import random
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

Status = Literal["ok", "retry", "fail", "timeout"]

_BASE = 1.0
_CAP = 30.0


@dataclass(frozen=True)
class Outcome:
    """Result of ``classify(exc_or_response)``. ``retry_after`` is seconds, if any."""

    status: Status
    retry_after: float | None = None


class RetryExhausted(Exception):
    """Raised after ``attempts`` retries, all classified ``retry``."""

    def __init__(self, outcome: Outcome) -> None:
        super().__init__("retries exhausted")
        self.outcome = outcome


class RequestFailed(Exception):
    """Raised on a ``fail`` classification; no retry."""

    def __init__(self, outcome: Outcome) -> None:
        super().__init__("request failed")
        self.outcome = outcome


class RequestTimeout(Exception):
    """Raised on a ``timeout`` classification. Message names the three remedies."""

    def __init__(self, message: str, outcome: Outcome) -> None:
        super().__init__(message)
        self.outcome = outcome


def call[T](
    fn: Callable[[], T],
    *,
    attempts: int = 3,
    timeout_s: float,
    classify: Callable[[object], Outcome],
    sleep: Callable[[float], None] = time.sleep,
    rand: Callable[[], float] = random.random,
    frames_per_call: int = 8,
) -> T:
    """Call ``fn`` until ``classify`` says ``ok``, or raise.

    ``attempts`` is the number of retries after the first try. Backoff is
    ``base * 2**n`` with ``base=1.0``, full jitter via ``rand``, capped at 30 s.
    A ``retry_after`` on the outcome replaces the computed delay.
    """
    retries = 0
    last: Outcome | None = None
    while True:
        try:
            result = fn()
        except Exception as exc:
            outcome = classify(exc)
            _raise_if_terminal(outcome, timeout_s, frames_per_call)
            last = outcome
            if retries >= attempts:
                raise RetryExhausted(outcome) from exc
            sleep(_delay(retries, outcome.retry_after, rand))
            retries += 1
            continue
        outcome = classify(result)
        if outcome.status == "ok":
            return result
        _raise_if_terminal(outcome, timeout_s, frames_per_call)
        last = outcome
        if retries >= attempts:
            raise RetryExhausted(outcome)
        sleep(_delay(retries, outcome.retry_after, rand))
        retries += 1
    raise RetryExhausted(last or Outcome(status="retry"))  # pragma: no cover


def _raise_if_terminal(outcome: Outcome, timeout_s: float, frames_per_call: int) -> None:
    if outcome.status == "fail":
        raise RequestFailed(outcome)
    if outcome.status == "timeout":
        raise RequestTimeout(_timeout_message(timeout_s, frames_per_call), outcome)


def _delay(n: int, retry_after: float | None, rand: Callable[[], float]) -> float:
    if retry_after is not None:
        return float(retry_after)
    computed = _BASE * (2**n)
    return rand() * min(_CAP, computed)


def _timeout_message(timeout_s: float, frames_per_call: int) -> str:
    timeout_shown = _fmt_number(timeout_s)
    frames_shown = _fmt_number(frames_per_call)
    return (
        f"--timeout <seconds> (raise the per-call timeout, currently {timeout_shown})\n"
        "--vision-quality low (send smaller frames)\n"
        f"--frames-per-call <n> (send fewer frames per request, currently {frames_shown})"
    )


def _fmt_number(value: float | int) -> str:
    if isinstance(value, int) or value == int(value):
        return str(int(value))
    return str(value)
