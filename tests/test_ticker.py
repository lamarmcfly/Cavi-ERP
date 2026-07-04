"""Tests for Ticker — read-through caching and Decimal-safe FX conversion.

Uses an in-memory cache driven by a fake clock, so cache-expiry behavior is
deterministic and no Redis is required.
"""
from decimal import Decimal

import pytest

from agents.ticker.ticker import (
    InMemoryCache,
    RateNotFound,
    StaticRateSource,
    Ticker,
)


class FakeClock:
    def __init__(self, t: float = 1_000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt


RATES = {
    ("USD", "EUR"): Decimal("0.92"),
    ("USD", "GBP"): Decimal("0.79"),
}


def _ticker(ttl=60):
    clock = FakeClock()
    source = StaticRateSource(RATES)
    ticker = Ticker(source, InMemoryCache(clock=clock), ttl_seconds=ttl, clock=clock)
    return ticker, source, clock


def test_read_through_cache_avoids_a_second_fetch():
    ticker, source, _ = _ticker()
    ticker.get_rate("USD", "EUR")
    ticker.get_rate("USD", "EUR")  # served from cache
    assert source.calls == 1


def test_cache_expiry_triggers_a_refetch():
    ticker, source, clock = _ticker(ttl=60)
    ticker.get_rate("USD", "EUR")
    clock.advance(61)  # past the TTL
    ticker.get_rate("USD", "EUR")
    assert source.calls == 2


def test_same_currency_rate_is_one():
    ticker, _, _ = _ticker()
    rate = ticker.get_rate("USD", "USD")
    assert rate.rate == Decimal(1)
    assert rate.convert_minor(12345) == 12345


def test_inverse_rate_is_resolved():
    ticker, _, _ = _ticker()
    # Only USD->EUR is in the table; EUR->USD must come from the inverse.
    rate = ticker.get_rate("EUR", "USD")
    assert rate.rate == Decimal(1) / Decimal("0.92")


def test_convert_minor_rounds_half_up():
    ticker, _, _ = _ticker()
    # 1 unit @ 2.5 -> 2.5 -> rounds up to 3; 2 units @ 0.92 -> 1.84 -> 2.
    src = StaticRateSource({("AAA", "BBB"): Decimal("2.5")})
    t = Ticker(src, InMemoryCache())
    assert t.convert(1, "AAA", "BBB") == 3
    assert ticker.convert(2, "USD", "EUR") == 2  # 2 * 0.92 = 1.84 -> 2


def test_unknown_rate_raises():
    ticker, _, _ = _ticker()
    with pytest.raises(RateNotFound):
        ticker.get_rate("USD", "JPY")
