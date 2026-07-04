"""Ticker — inbound ERP webhook ingestion (event runtime).

Thin bus wiring around `webhook.WebhookIngestor`. Consumes raw ERP webhooks and
emits the canonical `ticker.event.received` for each, so downstream agents see a
uniform, contract-validated event regardless of which ERP sent it.

    ticker.webhook  ->  ticker.event.received

Inbound webhooks are delivered onto `ticker.webhook` by the n8n middleware (or a
direct HTTP sink). Like the other agents' inbound subjects, `ticker.webhook` is
internal and not (yet) in the schema registry. A webhook we can't classify
(`MalformedWebhook`) is dropped with a warning rather than emitting a malformed
event; a *recognized-but-unrouted* type still emits, routed to the fallback.
"""
from __future__ import annotations

import logging

from agents.base import BaseAgent, Event
from agents.ticker.webhook import MalformedWebhook, WebhookIngestor

log = logging.getLogger("cavi.ticker.webhook")


class TickerWebhookAgent(BaseAgent):
    name = "ticker"

    def __init__(self, ingestor: WebhookIngestor | None = None) -> None:
        super().__init__()
        self.ingestor = ingestor or WebhookIngestor()

    @property
    def subjects(self) -> list[str]:
        return ["ticker.webhook"]

    def handle(self, event: Event) -> None:
        req = event.payload
        try:
            payload = self.ingestor.ingest(
                req["tenant_id"], req["erp_platform"], req["raw_payload"]
            )
        except MalformedWebhook as exc:
            log.warning("ticker dropped malformed webhook: %s", exc)
            return

        log.info(
            "ticker ingested %s from %s -> %s",
            payload["event_type"], payload["erp_platform"], payload["routed_to"],
        )
        self.emit(
            Event(
                subject="ticker.event.received",
                schema_version=1,
                source=self.name,
                correlation_id=event.correlation_id,
                payload=payload,
            )
        )


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    TickerWebhookAgent().run()
