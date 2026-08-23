"""Tests for the hash-chained audit log (W7 / FR7).

Pure domain, no database: proves the chain algebra (deterministic, genesis-
anchored, order-sensitive), that the in-memory store — which mirrors the
Postgres store's semantics — maintains the chain and stays idempotent under
redelivery, and that verification detects tampering (edit, delete, reorder)
while accepting an intact log. The export round-trip proves an exported log is
verifiable with no access to the store it came from.
"""
from __future__ import annotations

from agents.base.contract import Event
from scripts.audit_export import export_row, verify_export
from shared.audit import GENESIS_HASH, chain_hash, verify_chain
from shared.events import InMemoryEventStore


def _event(n: int) -> Event:
    return Event(
        subject="forge.completed",
        schema_version=1,
        source="forge",
        payload={"work_order_id": f"wo-{n}"},
        id=f"00000000-0000-0000-0000-00000000000{n}",
        tenant_id="tenant-acme",
    )


def _store_with(n: int) -> InMemoryEventStore:
    store = InMemoryEventStore()
    for i in range(n):
        store.record_event(_event(i))
    return store


def _entries(store: InMemoryEventStore) -> list[tuple[dict, str]]:
    return [(e.to_dict(), h) for e, h in zip(store.events, store.chain_hashes)]


# --------------------------------------------------------------------------- #
# Chain algebra
# --------------------------------------------------------------------------- #
def test_chain_hash_is_deterministic_and_key_order_independent():
    a = chain_hash(GENESIS_HASH, {"id": "1", "payload": {"x": 1, "y": 2}})
    b = chain_hash(GENESIS_HASH, {"payload": {"y": 2, "x": 1}, "id": "1"})
    assert a == b and a.startswith("sha256:")


def test_chain_hash_depends_on_the_predecessor():
    event = _event(0).to_dict()
    assert chain_hash(GENESIS_HASH, event) != chain_hash("sha256:" + "f" * 64, event)


# --------------------------------------------------------------------------- #
# Store behavior
# --------------------------------------------------------------------------- #
def test_store_builds_an_intact_chain_from_genesis():
    store = _store_with(3)
    assert len(store.chain_hashes) == 3
    assert store.chain_hashes[0] == chain_hash(GENESIS_HASH, store.events[0].to_dict())
    assert verify_chain(_entries(store)) == []


def test_redelivered_event_does_not_extend_or_fork_the_chain():
    store = _store_with(2)
    store.record_event(_event(0))   # at-least-once redelivery
    assert len(store.events) == 2 and len(store.chain_hashes) == 2
    assert verify_chain(_entries(store)) == []


# --------------------------------------------------------------------------- #
# Tamper detection
# --------------------------------------------------------------------------- #
def test_editing_a_historical_event_breaks_the_chain_at_that_position():
    store = _store_with(3)
    entries = _entries(store)
    tampered = dict(entries[1][0])
    tampered["payload"] = {"work_order_id": "wo-FORGED"}
    entries[1] = (tampered, entries[1][1])

    problems = verify_chain(entries)
    assert len(problems) == 1                   # one break, not a cascade
    assert "position 1" in problems[0]


def test_deleting_a_row_breaks_the_chain():
    store = _store_with(3)
    entries = _entries(store)
    del entries[1]
    assert verify_chain(entries)                # the gap is detected


def test_reordering_rows_breaks_the_chain():
    store = _store_with(3)
    entries = _entries(store)
    entries[0], entries[1] = entries[1], entries[0]
    assert verify_chain(entries)


# --------------------------------------------------------------------------- #
# Export round-trip — verifiable without the database
# --------------------------------------------------------------------------- #
def test_export_is_self_verifying():
    store = _store_with(3)
    lines = [
        export_row(
            {
                "seq": i + 1,
                "id": event.id,
                "subject": event.subject,
                "schema_version": event.schema_version,
                "source": event.source,
                "correlation_id": event.correlation_id,
                "tenant_id": event.tenant_id,
                "payload": event.payload,
                "chain_hash": recorded,
                "created_at": "2026-07-04T12:00:00+00:00",
            }
        )
        for i, (event, recorded) in enumerate(zip(store.events, store.chain_hashes))
    ]
    assert verify_export(lines) == []

    lines[2]["event"]["payload"] = {"work_order_id": "wo-FORGED"}
    problems = verify_export(lines)
    assert len(problems) == 1 and "position 2" in problems[0]
