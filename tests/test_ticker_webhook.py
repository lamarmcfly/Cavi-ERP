"""Tests for Ticker inbound ERP webhook ingestion.

Pure domain logic + contract validation, no infrastructure. Drives
`WebhookIngestor` with a fixed clock and validates the produced
`ticker.event.received` payloads against the canonical contract via the
SchemaRegistry.

Part of issue #2 (canonical emitters): #1 landed the contract; this adds the
Ticker capability that emits it.
"""
from __future__ import annotations

import pytest

from agents.base.registry import SchemaRegistry
from agents.ticker.webhook import (
    DEFAULT_FALLBACK,
    MalformedWebhook,
    WebhookIngestor,
    event_type_of,
)

FIXED_TS = "2026-07-04T12:00:00+00:00"


@pytest.fixture
def registry() -> SchemaRegistry:
    return SchemaRegistry()


def _ingestor(**kw) -> WebhookIngestor:
    kw.setdefault("clock", lambda: FIXED_TS)
    return WebhookIngestor(**kw)


# --------------------------------------------------------------------------- #
# Happy path
# --------------------------------------------------------------------------- #
def test_recognized_event_routes_and_validates(registry: SchemaRegistry):
    raw = {"event_type": "invoice.paid", "id": "INV-1", "amount": 100}
    payload = _ingestor().ingest("tenant-acme", "netsuite", raw)

    registry.validate("ticker.event.received", 1, payload)
    assert set(payload) == {
        "tenant_id", "erp_platform", "event_type", "raw_payload", "received_at", "routed_to",
    }
    assert payload["event_type"] == "invoice.paid"
    assert payload["routed_to"] == "ledger.query"      # from DEFAULT_ROUTES
    assert payload["received_at"] == FIXED_TS
    # Raw payload preserved verbatim (nothing dropped).
    assert payload["raw_payload"] == raw


def test_unrecognized_event_routes_to_fallback_still_emits(registry: SchemaRegistry):
    raw = {"event_type": "something.exotic", "x": 1}
    payload = _ingestor().ingest("tenant-acme", "sap", raw)
    registry.validate("ticker.event.received", 1, payload)
    assert payload["routed_to"] == DEFAULT_FALLBACK    # unknown -> fallback, not dropped


def test_custom_routes_are_honored(registry: SchemaRegistry):
    ing = _ingestor(routes={"payment.received": "ledger.query"}, fallback="ops.review")
    payload = ing.ingest("t", "netsuite", {"type": "payment.received"})
    registry.validate("ticker.event.received", 1, payload)
    assert payload["routed_to"] == "ledger.query"
    # A type not in the custom table falls back to the custom fallback.
    other = ing.ingest("t", "netsuite", {"type": "unmapped.event"})
    assert other["routed_to"] == "ops.review"


# --------------------------------------------------------------------------- #
# Event-type extraction
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("key", ["event_type", "type", "kind"])
def test_event_type_extracted_from_any_known_key(key: str):
    assert event_type_of({key: "invoice.paid"}) == "invoice.paid"


def test_malformed_webhook_without_a_type_raises():
    with pytest.raises(MalformedWebhook):
        _ingestor().ingest("tenant-acme", "netsuite", {"no": "type", "here": True})


def test_blank_type_is_treated_as_malformed():
    with pytest.raises(MalformedWebhook):
        event_type_of({"event_type": "   "})


def test_non_string_type_is_ignored_in_favor_of_next_key():
    # A numeric "type" is skipped; the string "kind" wins.
    assert event_type_of({"type": 123, "kind": "order.created"}) == "order.created"


# --------------------------------------------------------------------------- #
# Config guards
# --------------------------------------------------------------------------- #
def test_empty_fallback_is_rejected():
    with pytest.raises(ValueError):
        WebhookIngestor(fallback="")
