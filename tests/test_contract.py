"""Smoke tests for the event contract + schema registry.

These run without any infrastructure (no Postgres/Redis needed) so the scaffold
is verifiable immediately.
"""
import pytest

from agents.base.contract import Event
from agents.base.registry import SchemaNotFound, SchemaRegistry


def test_event_roundtrips_through_dict():
    event = Event(
        subject="ledger.entry",
        schema_version=1,
        source="forge",
        payload={"entry_id": "x"},
        correlation_id="corr-1",
    )
    restored = Event.from_dict(event.to_dict())
    assert restored == event


def test_registry_validates_a_known_schema():
    registry = SchemaRegistry()
    good = {
        "entry_id": "00000000-0000-0000-0000-000000000000",
        "currency": "USD",
        "lines": [
            {"account": "1000", "direction": "debit", "amount_minor": 500},
            {"account": "4000", "direction": "credit", "amount_minor": 500},
        ],
    }
    registry.validate("ledger.entry", 1, good)  # must not raise


def test_registry_raises_on_unknown_subject():
    registry = SchemaRegistry()
    with pytest.raises(SchemaNotFound):
        registry.validate("does.not.exist", 1, {})
