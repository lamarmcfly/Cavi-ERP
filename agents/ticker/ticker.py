"""Ticker — pricing & FX (domain logic).

Ticker serves exchange-rate snapshots through a **read-through cache**: a lookup
checks the cache first and only hits the upstream rate source on a miss, storing
the result with a TTL. This bounds load on the FX provider while keeping rates
fresh enough for pricing.

Money rules enforced here:
  * Rates are `Decimal`, never float (float rounding corrupts money).
  * Amounts are integer minor units; conversion rounds with an explicit policy
    (`ROUND_HALF_UP` by default) — fractional cents are an accounting decision.

The cache and the rate source are both injectable, so tests run with an
in-memory cache + a fake clock and never touch Redis or a real FX API.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from typing import Callable, Mapping, Protocol

DEFAULT_TTL_SECONDS = 60  # price snapshots are cached for 60s


class TickerError(Exception):
    pass


class RateNotFound(TickerError):
    pass


# --------------------------------------------------------------------------- #
# Rate snapshot
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Rate:
    base: str
    quote: str
    rate: Decimal  # 1 unit of `base` == `rate` units of `quote`
    as_of: float   # epoch seconds the rate was sourced

    def convert_minor(self, amount_minor: int, rounding: str = ROUND_HALF_UP) -> int:
        """Convert an amount in `base` minor units to `quote` minor units.

        Assumes both currencies share a minor-unit scale (e.g. 2 decimals).
        Rounding is explicit because fractional cents are a policy choice.
        """
        converted = (Decimal(amount_minor) * self.rate).quantize(
            Decimal(1), rounding=rounding
        )
        return int(converted)


# --------------------------------------------------------------------------- #
# Rate sources (upstream of the cache)
# --------------------------------------------------------------------------- #
class RateSource(Protocol):
    def fetch(self, base: str, quote: str) -> Decimal: ...


class StaticRateSource:
    """A fixed rate table — stands in for a live FX API. Resolves identity and
    inverse pairs automatically. Tracks `calls` so tests can assert cache hits
    avoided a fetch."""

    def __init__(self, rates: Mapping[tuple[str, str], Decimal]) -> None:
        self._rates = {(b.upper(), q.upper()): Decimal(r) for (b, q), r in rates.items()}
        self.calls = 0

    def fetch(self, base: str, quote: str) -> Decimal:
        self.calls += 1
        base, quote = base.upper(), quote.upper()
        if base == quote:
            return Decimal(1)
        if (base, quote) in self._rates:
            return self._rates[(base, quote)]
        inverse = self._rates.get((quote, base))
        if inverse is not None:
            return Decimal(1) / inverse
        raise RateNotFound(f"no rate for {base}->{quote}")


# --------------------------------------------------------------------------- #
# Cache backends
# --------------------------------------------------------------------------- #
class CacheBackend(Protocol):
    def get(self, key: str) -> str | None: ...
    def set(self, key: str, value: str, ttl_seconds: int) -> None: ...


class InMemoryCache:
    """TTL cache for tests. Expiry is driven by an injectable clock so tests
    don't depend on wall time."""

    def __init__(self, clock: Callable[[], float] = time.time) -> None:
        self._clock = clock
        self._data: dict[str, tuple[float, str]] = {}

    def get(self, key: str) -> str | None:
        item = self._data.get(key)
        if item is None:
            return None
        expires_at, value = item
        if self._clock() >= expires_at:
            del self._data[key]
            return None
        return value

    def set(self, key: str, value: str, ttl_seconds: int) -> None:
        self._data[key] = (self._clock() + ttl_seconds, value)


class RedisCache:
    """Production cache backed by Redis `SETEX`. Imports the client lazily."""

    def __init__(self, client=None) -> None:
        self._client = client

    def _conn(self):
        if self._client is None:
            from shared.cache import get_client

            self._client = get_client()
        return self._client

    def get(self, key: str) -> str | None:
        return self._conn().get(key)

    def set(self, key: str, value: str, ttl_seconds: int) -> None:
        self._conn().setex(key, ttl_seconds, value)


# --------------------------------------------------------------------------- #
# Ticker
# --------------------------------------------------------------------------- #
class Ticker:
    def __init__(
        self,
        source: RateSource,
        cache: CacheBackend | None = None,
        *,
        ttl_seconds: int = DEFAULT_TTL_SECONDS,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._source = source
        self._cache = cache or RedisCache()
        self._ttl = ttl_seconds
        self._clock = clock

    @staticmethod
    def _cache_key(base: str, quote: str) -> str:
        return f"ticker:rate:{base}:{quote}"

    @staticmethod
    def _encode(rate: Rate) -> str:
        return json.dumps(
            {"base": rate.base, "quote": rate.quote, "rate": str(rate.rate), "as_of": rate.as_of}
        )

    @staticmethod
    def _decode(blob: str) -> Rate:
        d = json.loads(blob)
        return Rate(base=d["base"], quote=d["quote"], rate=Decimal(d["rate"]), as_of=d["as_of"])

    def get_rate(self, base: str, quote: str) -> Rate:
        """Return a rate snapshot, read-through cached for `ttl_seconds`."""
        base, quote = base.upper(), quote.upper()
        key = self._cache_key(base, quote)

        cached = self._cache.get(key)
        if cached is not None:
            return self._decode(cached)

        rate = Rate(base=base, quote=quote, rate=self._source.fetch(base, quote), as_of=self._clock())
        self._cache.set(key, self._encode(rate), self._ttl)
        return rate

    def convert(
        self, amount_minor: int, base: str, quote: str, rounding: str = ROUND_HALF_UP
    ) -> int:
        return self.get_rate(base, quote).convert_minor(amount_minor, rounding)
