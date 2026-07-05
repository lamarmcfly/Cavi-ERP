"""Tests for the Ledger double-entry core.

Run against an in-memory store, so no database is needed. These cover the two
invariants the schema registry can't enforce: balance and idempotency.
"""
import pytest

from agents.ledger.ledger import (
    Direction,
    InMemoryLedgerStore,
    JournalEntry,
    Ledger,
    UnbalancedEntry,
)


def _entry(entry_id="00000000-0000-0000-0000-000000000001", lines=None, currency="USD",
           tenant_id="tenant-acme"):
    """A balanced 2-line entry by default; override `lines` to break balance."""
    payload = {
        "entry_id": entry_id,
        "currency": currency,
        "lines": lines
        or [
            {"account": "1000-cash", "direction": "debit", "amount_minor": 2500},
            {"account": "4000-revenue", "direction": "credit", "amount_minor": 2500},
        ],
    }
    return JournalEntry.from_payload(payload, tenant_id=tenant_id)


@pytest.fixture
def ledger() -> Ledger:
    return Ledger(store=InMemoryLedgerStore())


def test_balanced_entry_posts(ledger: Ledger):
    result = ledger.post(_entry())
    assert result.status == "posted"
    assert result.total_minor == 2500
    assert result.line_count == 2


def test_multi_line_balanced_entry_posts(ledger: Ledger):
    # One debit split across two credit accounts — still balanced.
    entry = _entry(
        lines=[
            {"account": "1000-cash", "direction": "debit", "amount_minor": 1000},
            {"account": "4000-revenue", "direction": "credit", "amount_minor": 600},
            {"account": "2200-tax", "direction": "credit", "amount_minor": 400},
        ]
    )
    assert entry.is_balanced()
    assert ledger.post(entry).status == "posted"


def test_unbalanced_entry_is_rejected(ledger: Ledger):
    entry = _entry(
        lines=[
            {"account": "1000-cash", "direction": "debit", "amount_minor": 2500},
            {"account": "4000-revenue", "direction": "credit", "amount_minor": 2400},
        ]
    )
    with pytest.raises(UnbalancedEntry):
        ledger.post(entry)


def test_all_debits_is_unbalanced(ledger: Ledger):
    # Two debit lines, no credits: structurally valid per schema, but credits=0.
    entry = _entry(
        lines=[
            {"account": "1000-cash", "direction": "debit", "amount_minor": 500},
            {"account": "1500-inventory", "direction": "debit", "amount_minor": 500},
        ]
    )
    assert entry.total_credits == 0
    assert not entry.is_balanced()
    with pytest.raises(UnbalancedEntry):
        ledger.post(entry)


def test_reposting_same_entry_is_idempotent(ledger: Ledger):
    first = ledger.post(_entry())
    second = ledger.post(_entry())  # same entry_id — replayed by the bus
    assert first.status == "posted"
    assert second.status == "duplicate"


def test_from_payload_parses_directions_and_totals():
    entry = _entry()
    assert entry.lines[0].direction is Direction.DEBIT
    assert entry.lines[1].direction is Direction.CREDIT
    assert entry.total_debits == entry.total_credits == 2500
    assert entry.memo is None
