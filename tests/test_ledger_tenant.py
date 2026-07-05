"""Tests for Ledger tenant isolation (H1).

The books are tenant-scoped: an entry requires a tenant_id, and one tenant can
never read another's entries. In-memory store here; the Postgres store enforces
the same via NOT NULL columns + tenant-scoped queries (migration 0003).
"""
from __future__ import annotations

import pytest

from agents.ledger.ledger import (
    InMemoryLedgerStore,
    JournalEntry,
    Ledger,
    LedgerError,
)

LINES = [
    {"account": "1000-cash", "direction": "debit", "amount_minor": 2500},
    {"account": "4000-revenue", "direction": "credit", "amount_minor": 2500},
]


def _payload(entry_id: str = "00000000-0000-0000-0000-0000000000ab") -> dict:
    return {"entry_id": entry_id, "currency": "USD", "lines": LINES}


def test_entry_requires_a_tenant_id():
    with pytest.raises(LedgerError):
        JournalEntry.from_payload(_payload(), tenant_id="")


def test_entry_carries_its_tenant_id():
    entry = JournalEntry.from_payload(_payload(), tenant_id="tenant-acme")
    assert entry.tenant_id == "tenant-acme"


def test_one_tenant_cannot_read_anothers_entry():
    store = InMemoryLedgerStore()
    ledger = Ledger(store=store)
    entry = JournalEntry.from_payload(_payload(), tenant_id="tenant-acme")
    assert ledger.post(entry).status == "posted"

    # The owning tenant sees it...
    assert store.get("tenant-acme", entry.entry_id) is not None
    # ...another tenant, asking for the very same entry_id, sees nothing.
    assert store.get("tenant-intruder", entry.entry_id) is None


def test_same_entry_id_is_scoped_by_tenant_on_read():
    # entry_id is globally unique in practice, but the store must still refuse a
    # cross-tenant read even if an id is guessed/reused.
    store = InMemoryLedgerStore()
    store.insert_if_absent(JournalEntry.from_payload(_payload("id-1"), tenant_id="A"))
    assert store.get("A", "id-1") is not None
    assert store.get("B", "id-1") is None
