"""Ticker — inbound ERP webhook ingestion (domain logic).

The FX side of Ticker (`ticker.py`) serves rates. This side is the **inbound
edge**: an ERP (NetSuite, etc.) POSTs a webhook, and Ticker normalizes it into a
canonical `ticker.event.received` event — preserving the raw payload verbatim,
classifying its `event_type`, and deciding which internal subject should handle
it (`routed_to`).

Two seams do the work, both injectable/overridable:
  * **Type extraction** (`event_type_of`) — where the ERP puts the event name.
  * **Routing** (`DEFAULT_ROUTES` + fallback) — event_type -> internal subject.
    This is the business seam: tune the table for your deployment.

Malformed webhooks (no discernible event type) raise `MalformedWebhook` rather
than being silently routed, so a contract gap at the ERP boundary is visible.
The raw payload is never dropped — a recognized *or* unrecognized event still
produces a `ticker.event.received` (unrecognized ones route to the fallback), so
nothing an ERP sends is lost.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Callable, Mapping

from agents.ticker.ticker import TickerError

# event_type -> internal subject that should process it. The business seam:
# adjust this table (and the fallback) for the deployment's routing.
DEFAULT_ROUTES: dict[str, str] = {
    "invoice.paid": "ledger.query",
    "invoice.updated": "mapper.transform",
    "salesorder.created": "forge.write.propose",
}
# Where an event with no matching route goes — must be non-empty so the contract
# (routed_to: minLength 1) always holds.
DEFAULT_FALLBACK = "ticker.unrouted"

# Keys an ERP might use to name the event, tried in order.
DEFAULT_TYPE_KEYS = ("event_type", "type", "kind")


class MalformedWebhook(TickerError):
    """Raised when an inbound webhook carries no discernible event type."""


def event_type_of(
    raw_payload: Mapping, *, keys: tuple[str, ...] = DEFAULT_TYPE_KEYS
) -> str:
    """Extract the event type from a raw webhook, trying `keys` in order.

    Raises `MalformedWebhook` if none of the keys hold a non-empty string.
    """
    for key in keys:
        value = raw_payload.get(key)
        if isinstance(value, str) and value.strip():
            return value
    raise MalformedWebhook(
        f"webhook has no event type in any of {keys}"
    )


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class WebhookIngestor:
    """Normalizes inbound ERP webhooks into `ticker.event.received` payloads.

    `routes`, `fallback`, `type_keys`, and `clock` are injectable so routing and
    timestamps are deterministic under test.
    """

    def __init__(
        self,
        routes: Mapping[str, str] | None = None,
        *,
        fallback: str = DEFAULT_FALLBACK,
        type_keys: tuple[str, ...] = DEFAULT_TYPE_KEYS,
        clock: Callable[[], str] = _now_iso,
    ) -> None:
        self._routes = dict(routes) if routes is not None else dict(DEFAULT_ROUTES)
        if not fallback:
            raise ValueError("fallback route must be a non-empty subject")
        self._fallback = fallback
        self._type_keys = type_keys
        self._clock = clock

    def route_for(self, event_type: str) -> str:
        """Internal subject an event type is dispatched to (fallback if unknown)."""
        return self._routes.get(event_type, self._fallback)

    def ingest(
        self, tenant_id: str, erp_platform: str, raw_payload: Mapping
    ) -> dict:
        """Build a canonical `ticker.event.received` payload from a raw webhook.

        Raises `MalformedWebhook` if the event type can't be determined.
        """
        event_type = event_type_of(raw_payload, keys=self._type_keys)
        return {
            "tenant_id": tenant_id,
            "erp_platform": erp_platform,
            "event_type": event_type,
            "raw_payload": dict(raw_payload),  # preserved verbatim
            "received_at": self._clock(),
            "routed_to": self.route_for(event_type),
        }
