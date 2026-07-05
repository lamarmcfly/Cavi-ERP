"""Tests for Forge — the work-order state machine and revenue recognition.

Pure domain logic, no infrastructure. The last test closes the loop with Ledger:
the entry Forge produces posts cleanly, proving the end-to-end contract holds.
"""
import pytest

from agents.forge.forge import (
    Forge,
    InvalidTransition,
    WorkOrder,
    WorkOrderState,
    ledger_entry_id,
)
from agents.ledger.ledger import InMemoryLedgerStore, JournalEntry, Ledger


def _order(**overrides) -> WorkOrder:
    base = dict(
        work_order_id="wo-1001",
        tenant_id="tenant-acme",
        sku="WIDGET-9",
        quantity=3,
        unit_price_minor=1000,  # $10.00
        currency="USD",
        tax_rate_bps=825,        # 8.25%
        state=WorkOrderState.IN_PROGRESS,
    )
    base.update(overrides)
    return WorkOrder(**base)


@pytest.fixture
def forge() -> Forge:
    return Forge()


def test_completion_produces_a_balanced_ledger_entry(forge: Forge):
    result = forge.complete(_order())
    lines = result.ledger_entry["lines"]
    debits = sum(ln["amount_minor"] for ln in lines if ln["direction"] == "debit")
    credits = sum(ln["amount_minor"] for ln in lines if ln["direction"] == "credit")
    assert debits == credits          # balanced by construction
    assert debits == 3 * 1000 + 247   # gross = net 3000 + tax floor(3000*825/10000)=247


def test_zero_tax_yields_two_balanced_lines(forge: Forge):
    result = forge.complete(_order(tax_rate_bps=0))
    lines = result.ledger_entry["lines"]
    assert len(lines) == 2  # no tax line
    assert lines[0]["amount_minor"] == lines[1]["amount_minor"] == 3000


def test_cannot_complete_a_completed_order(forge: Forge):
    completed = forge.complete(_order()).order
    assert completed.state is WorkOrderState.COMPLETED
    with pytest.raises(InvalidTransition):
        forge.complete(completed)


def test_cannot_complete_a_cancelled_order(forge: Forge):
    cancelled = forge.cancel(_order(state=WorkOrderState.CREATED))
    with pytest.raises(InvalidTransition):
        forge.complete(cancelled)


def test_ledger_entry_id_is_deterministic(forge: Forge):
    # Same work order -> same entry id, so a replayed completion dedupes in Ledger.
    assert ledger_entry_id(_order()) == ledger_entry_id(_order())
    assert ledger_entry_id(_order()) != ledger_entry_id(_order(work_order_id="wo-2002"))


def test_forge_entry_posts_to_ledger_end_to_end(forge: Forge):
    # The whole point: Forge's output is valid input for Ledger.
    result = forge.complete(_order())
    entry = JournalEntry.from_payload(result.ledger_entry)
    ledger = Ledger(store=InMemoryLedgerStore())

    first = ledger.post(entry)
    assert first.status == "posted"
    # Replaying the same completion is idempotent across the two agents.
    assert ledger.post(JournalEntry.from_payload(result.ledger_entry)).status == "duplicate"
