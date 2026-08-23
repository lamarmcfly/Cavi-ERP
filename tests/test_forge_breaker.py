"""Tests for the ERP write circuit breaker (W9 / FR9, Story 6.2).

Pure domain, injectable clock — no sleeping. Proves: failures below the
threshold don't trip it; the transition to OPEN happens exactly once (the
escalation moment); while OPEN every check is refused up front; after the
cooldown exactly one half-open probe goes through; the probe's outcome closes
or re-opens it.
"""
from __future__ import annotations

import pytest

from agents.forge.breaker import BreakerOpen, BreakerState, CircuitBreaker


class FakeClock:
    def __init__(self) -> None:
        self.now = 1_000.0

    def __call__(self) -> float:
        return self.now


def _tripped(clock: FakeClock, threshold: int = 3) -> CircuitBreaker:
    breaker = CircuitBreaker(
        failure_threshold=threshold, cooldown_seconds=60.0, clock=clock
    )
    for _ in range(threshold):
        breaker.record_failure()
    return breaker


def test_failures_below_threshold_keep_it_closed():
    breaker = CircuitBreaker(failure_threshold=3, cooldown_seconds=60.0, clock=FakeClock())
    breaker.record_failure()
    breaker.record_failure()
    assert breaker.state is BreakerState.CLOSED
    breaker.check()   # does not raise


def test_a_success_resets_the_consecutive_count():
    breaker = CircuitBreaker(failure_threshold=3, cooldown_seconds=60.0, clock=FakeClock())
    breaker.record_failure()
    breaker.record_failure()
    breaker.record_success()
    breaker.record_failure()
    breaker.record_failure()
    assert breaker.state is BreakerState.CLOSED   # never 3 consecutive


def test_transition_to_open_is_reported_exactly_once():
    breaker = CircuitBreaker(failure_threshold=3, cooldown_seconds=60.0, clock=FakeClock())
    assert breaker.record_failure() is False
    assert breaker.record_failure() is False
    assert breaker.record_failure() is True    # the escalation moment
    assert breaker.state is BreakerState.OPEN


def test_open_refuses_up_front():
    clock = FakeClock()
    breaker = _tripped(clock)
    with pytest.raises(BreakerOpen, match="retry in"):
        breaker.check()


def test_half_open_allows_exactly_one_probe():
    clock = FakeClock()
    breaker = _tripped(clock)
    clock.now += 61.0                          # cooldown elapsed
    assert breaker.state is BreakerState.HALF_OPEN
    breaker.check()                            # the single probe passes the gate
    with pytest.raises(BreakerOpen):
        breaker.check()                        # a second is refused until it reports


def test_probe_success_closes_the_breaker():
    clock = FakeClock()
    breaker = _tripped(clock)
    clock.now += 61.0
    breaker.check()
    breaker.record_success()
    assert breaker.state is BreakerState.CLOSED
    breaker.check()   # fully back to normal


def test_probe_failure_reopens_for_a_fresh_cooldown_without_re_escalating():
    clock = FakeClock()
    breaker = _tripped(clock)
    clock.now += 61.0
    breaker.check()
    # Re-open is not a new OPEN transition — no second page for the same outage.
    assert breaker.record_failure() is False
    assert breaker.state is BreakerState.OPEN
    clock.now += 59.0                          # fresh cooldown, not the old one
    assert breaker.state is BreakerState.OPEN
    clock.now += 2.0
    assert breaker.state is BreakerState.HALF_OPEN


def test_threshold_must_be_positive():
    with pytest.raises(ValueError):
        CircuitBreaker(failure_threshold=0)
