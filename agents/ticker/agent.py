"""Ticker — pricing & FX (event runtime).

Consumes `ticker.price.request` events and replies with a `ticker.price`
snapshot, served read-through from the Redis cache. If the request includes an
`amount_minor`, the converted amount is included so callers (e.g. Forge pricing
a work order in another currency) get the conversion in one round trip.
"""
from __future__ import annotations

import logging
from decimal import Decimal

from agents.base import BaseAgent, Event
from agents.ticker.ticker import RateNotFound, StaticRateSource, Ticker

log = logging.getLogger("cavi.ticker")

# Placeholder rate table — swap for a live FX provider (RateSource) in prod.
_DEMO_RATES = {
    ("USD", "EUR"): Decimal("0.92"),
    ("USD", "GBP"): Decimal("0.79"),
    ("EUR", "GBP"): Decimal("0.86"),
}


class TickerAgent(BaseAgent):
    name = "ticker"

    def __init__(self, ticker: Ticker | None = None) -> None:
        super().__init__()
        self.ticker = ticker or Ticker(StaticRateSource(_DEMO_RATES))

    @property
    def subjects(self) -> list[str]:
        return ["ticker.price.request"]

    def handle(self, event: Event) -> None:
        req = event.payload
        base, quote = req["base"], req["quote"]
        try:
            rate = self.ticker.get_rate(base, quote)
        except RateNotFound as exc:
            log.warning("ticker has no rate %s->%s: %s", base, quote, exc)
            return

        payload = {
            "base": rate.base,
            "quote": rate.quote,
            "rate": str(rate.rate),  # Decimal as string to preserve precision
            "as_of": rate.as_of,
        }
        if "amount_minor" in req:
            amount = int(req["amount_minor"])
            payload["amount_minor"] = amount
            payload["converted_minor"] = rate.convert_minor(amount)

        log.info("ticker quoted %s->%s @ %s", rate.base, rate.quote, rate.rate)
        self.emit(
            Event(
                subject="ticker.price",
                schema_version=1,
                source=self.name,
                correlation_id=event.correlation_id,
                payload=payload,
            )
        )


if __name__ == "__main__":
    from agents.base.runtime import run_agent

    run_agent(TickerAgent())
