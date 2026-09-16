"""Retry: backoff, Retry-After, fail-fast, exhaustion, timeout remedies, cap."""

from __future__ import annotations

import pytest

from frameweave.util.retry import RequestFailed, RequestTimeout, RetryExhausted, call
from tests.fakes import FakeClient


def test_three_retries_then_success(fake_clock, fixed_rand) -> None:
    client = FakeClient(
        [
            ("retry", None),
            ("retry", None),
            ("retry", None),
            ("ok", "done"),
        ]
    )
    result = call(
        client,
        timeout_s=30,
        classify=FakeClient.classify,
        sleep=fake_clock,
        rand=fixed_rand,
    )
    assert result == ("ok", "done")
    assert client.calls == 4
    assert fake_clock.sleeps == [0.5, 1.0, 2.0]


def test_retry_after_overrides_backoff(fake_clock, fixed_rand) -> None:
    client = FakeClient(
        [
            ("retry", 7),
            ("ok", "done"),
        ]
    )
    call(
        client,
        timeout_s=30,
        classify=FakeClient.classify,
        sleep=fake_clock,
        rand=fixed_rand,
    )
    assert fake_clock.sleeps == [7]


def test_fail_raises_at_once_with_zero_sleeps(fake_clock, fixed_rand) -> None:
    client = FakeClient([("fail",)])
    with pytest.raises(RequestFailed) as exc:
        call(
            client,
            timeout_s=30,
            classify=FakeClient.classify,
            sleep=fake_clock,
            rand=fixed_rand,
        )
    assert exc.value.outcome.status == "fail"
    assert fake_clock.sleeps == []
    assert client.calls == 1


def test_four_consecutive_retries_raise_exhausted(fake_clock, fixed_rand) -> None:
    client = FakeClient([("retry", None)] * 4)
    with pytest.raises(RetryExhausted) as exc:
        call(
            client,
            timeout_s=30,
            classify=FakeClient.classify,
            sleep=fake_clock,
            rand=fixed_rand,
        )
    assert exc.value.outcome.status == "retry"
    assert client.calls == 4
    assert len(fake_clock.sleeps) == 3


def test_timeout_message_names_flags_and_current_values(fake_clock, fixed_rand) -> None:
    client = FakeClient([("timeout",)])
    with pytest.raises(RequestTimeout) as exc:
        call(
            client,
            timeout_s=45,
            frames_per_call=4,
            classify=FakeClient.classify,
            sleep=fake_clock,
            rand=fixed_rand,
        )
    message = str(exc.value)
    assert "--timeout" in message
    assert "--vision-quality" in message
    assert "--frames-per-call" in message
    assert "currently 45" in message
    assert "currently 4" in message
    assert message == (
        "--timeout <seconds> (raise the per-call timeout, currently 45)\n"
        "--vision-quality standard (send smaller frames)\n"
        "--frames-per-call <n> (send fewer frames per request, currently 4)"
    )
    assert fake_clock.sleeps == []


def test_jitter_never_exceeds_cap(fake_clock) -> None:
    def one() -> float:
        return 1.0

    client = FakeClient([("retry", None)] * 6 + [("ok", "done")])
    call(
        client,
        attempts=6,
        timeout_s=30,
        classify=FakeClient.classify,
        sleep=fake_clock,
        rand=one,
    )
    assert fake_clock.sleeps
    assert all(delay <= 30 for delay in fake_clock.sleeps)
    assert 30 in fake_clock.sleeps
