"""Forge — ERP write circuit breaker (W9 / FR9, Story 6.2).

Repeated write failures usually mean something systemic — the ERP is down,
credentials expired, a rate limit tripped. Retrying every approved write into
that condition hammers the ERP (burning NetSuite governance units) and buries
the real problem in noise. The breaker converts "keeps failing" into a single
loud escalation:

    CLOSED     normal operation; failures are counted, successes reset them
    OPEN       `failure_threshold` consecutive failures tripped it — every
               execute is refused up front (`BreakerOpen`) until `cooldown_
               seconds` pass. Writes stay APPROVED and retryable; nothing is
               dropped, and the *transition* to OPEN is the escalation moment.
    HALF_OPEN  cooldown elapsed — exactly one probe write is allowed through;
               success closes the breaker, failure re-opens it for another
               full cooldown.

Pure and injectable (`clock` is monotonic seconds) so tests never sleep. Not
thread-safe by itself; the write agent drives it from its single handler loop.
"""
from __future__ import annotations

import time
from enum import Enum
from typing import Callable

from agents.forge.write import ErpWriteError

DEFAULT_FAILURE_THRESHOLD = 5
DEFAULT_COOLDOWN_SECONDS = 300.0


class BreakerState(str, Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class BreakerOpen(ErpWriteError):
    """Refused up front: the breaker is open, the ERP is not called at all."""


class CircuitBreaker:
    def __init__(
        self,
        *,
        failure_threshold: int = DEFAULT_FAILURE_THRESHOLD,
        cooldown_seconds: float = DEFAULT_COOLDOWN_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if failure_threshold < 1:
            raise ValueError("failure_threshold must be >= 1")
        self._threshold = failure_threshold
        self._cooldown = cooldown_seconds
        self._clock = clock
        self._consecutive_failures = 0
        self._opened_at: float | None = None
        self._probing = False

    @property
    def state(self) -> BreakerState:
        if self._opened_at is None:
            return BreakerState.CLOSED
        if self._clock() - self._opened_at >= self._cooldown:
            return BreakerState.HALF_OPEN
        return BreakerState.OPEN

    def check(self) -> None:
        """Gate before touching the ERP. Raises `BreakerOpen` while open;
        in HALF_OPEN, lets exactly one probe through and refuses the rest
        until that probe reports back."""
        state = self.state
        if state is BreakerState.CLOSED:
            return
        if state is BreakerState.HALF_OPEN and not self._probing:
            self._probing = True
            return
        remaining = (
            self._cooldown - (self._clock() - self._opened_at)
            if self._opened_at is not None
            else 0.0
        )
        raise BreakerOpen(
            f"circuit breaker open after {self._consecutive_failures} consecutive "
            f"ERP write failures; retry in {max(remaining, 0.0):.0f}s"
        )

    def record_success(self) -> None:
        self._consecutive_failures = 0
        self._opened_at = None
        self._probing = False

    def record_failure(self) -> bool:
        """Count a failure. Returns True on the transition to OPEN — the
        caller's one moment to escalate to a human."""
        was_open = self._opened_at is not None
        self._consecutive_failures += 1
        self._probing = False
        if self._consecutive_failures >= self._threshold or was_open:
            # Trip (or re-trip after a failed half-open probe): restart cooldown.
            self._opened_at = self._clock()
            return not was_open
        return False
