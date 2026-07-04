"""Forge — production & order fulfillment (domain logic).

Forge runs work orders through a small state machine and, on completion, turns
the sale into a **balanced** `ledger.entry` payload (revenue recognition). It is
the upstream producer for Ledger.

Two invariants live here:
  * **Legal transitions** — a work order can't be completed twice or revived
    after cancellation. Illegal moves raise `InvalidTransition`.
  * **Balanced recognition** — the generated ledger entry always satisfies
    debits == credits by construction (gross = net + tax).

The ledger `entry_id` is derived deterministically from the work order, so a
replayed completion yields the same id and Ledger's idempotency dedupes it.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, replace
from enum import Enum
from typing import Mapping

# Default GL accounts for revenue recognition. These are the business seam —
# adjust the chart-of-accounts mapping in `revenue_lines` to fit the deployment.
ACCOUNT_RECEIVABLE = "1100-accounts-receivable"
ACCOUNT_REVENUE = "4000-revenue"
ACCOUNT_TAX_PAYABLE = "2200-tax-payable"

# Stable namespace so ledger entry ids are reproducible across replays.
_ENTRY_NAMESPACE = uuid.uuid5(uuid.NAMESPACE_DNS, "cavi-erp.forge.ledger-entry")


class WorkOrderState(str, Enum):
    CREATED = "created"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    CANCELLED = "cancelled"


# Allowed forward transitions; terminal states have none.
_ALLOWED: dict[WorkOrderState, set[WorkOrderState]] = {
    WorkOrderState.CREATED: {WorkOrderState.IN_PROGRESS, WorkOrderState.CANCELLED},
    WorkOrderState.IN_PROGRESS: {WorkOrderState.COMPLETED, WorkOrderState.CANCELLED},
    WorkOrderState.COMPLETED: set(),
    WorkOrderState.CANCELLED: set(),
}


class ForgeError(Exception):
    """Base class for Forge failures."""


class InvalidTransition(ForgeError):
    pass


@dataclass(frozen=True)
class WorkOrder:
    work_order_id: str
    tenant_id: str
    sku: str
    quantity: int
    unit_price_minor: int
    currency: str
    tax_rate_bps: int = 0  # basis points, e.g. 825 = 8.25%
    state: WorkOrderState = WorkOrderState.CREATED

    @property
    def net_minor(self) -> int:
        return self.quantity * self.unit_price_minor

    @property
    def tax_minor(self) -> int:
        # Integer minor units; gross stays exactly net + tax so the entry balances.
        return self.net_minor * self.tax_rate_bps // 10_000

    @property
    def gross_minor(self) -> int:
        return self.net_minor + self.tax_minor

    def transition_to(self, new_state: WorkOrderState) -> "WorkOrder":
        if new_state not in _ALLOWED[self.state]:
            raise InvalidTransition(
                f"work order {self.work_order_id}: {self.state.value} -> {new_state.value}"
            )
        return replace(self, state=new_state)

    @classmethod
    def from_payload(cls, payload: Mapping) -> "WorkOrder":
        return cls(
            work_order_id=payload["work_order_id"],
            tenant_id=payload["tenant_id"],
            sku=payload["sku"],
            quantity=int(payload["quantity"]),
            unit_price_minor=int(payload["unit_price_minor"]),
            currency=payload["currency"],
            tax_rate_bps=int(payload.get("tax_rate_bps", 0)),
            state=WorkOrderState(payload.get("state", WorkOrderState.CREATED.value)),
        )


def ledger_entry_id(order: WorkOrder) -> str:
    """Deterministic ledger entry id for a work order (replay-safe)."""
    return str(uuid.uuid5(_ENTRY_NAMESPACE, f"{order.tenant_id}:{order.work_order_id}"))


def revenue_lines(order: WorkOrder) -> tuple[dict, ...]:
    """Map a completed sale to balanced double-entry lines.

    Debit receivable for the gross; credit revenue for the net; credit tax
    payable for the tax (omitted when zero). debits == credits by construction.
    This is the chart-of-accounts seam — change the accounts/splits here.
    """
    lines = [
        {"account": ACCOUNT_RECEIVABLE, "direction": "debit", "amount_minor": order.gross_minor},
        {"account": ACCOUNT_REVENUE, "direction": "credit", "amount_minor": order.net_minor},
    ]
    if order.tax_minor:
        lines.append(
            {"account": ACCOUNT_TAX_PAYABLE, "direction": "credit", "amount_minor": order.tax_minor}
        )
    return tuple(lines)


@dataclass(frozen=True)
class CompletionResult:
    order: WorkOrder            # the completed work order
    completed_event: dict       # forge.completed payload
    ledger_entry: dict          # ledger.entry payload (balanced)


class Forge:
    def start(self, order: WorkOrder) -> WorkOrder:
        return order.transition_to(WorkOrderState.IN_PROGRESS)

    def cancel(self, order: WorkOrder) -> WorkOrder:
        return order.transition_to(WorkOrderState.CANCELLED)

    def complete(self, order: WorkOrder) -> CompletionResult:
        """Complete a work order and derive its financial consequence.

        Raises `InvalidTransition` if the order isn't in progress.
        """
        completed = order.transition_to(WorkOrderState.COMPLETED)
        completed_event = {
            "work_order_id": completed.work_order_id,
            "tenant_id": completed.tenant_id,
            "sku": completed.sku,
            "quantity": completed.quantity,
            "currency": completed.currency,
            "net_minor": completed.net_minor,
            "tax_minor": completed.tax_minor,
            "gross_minor": completed.gross_minor,
        }
        ledger_entry = {
            "entry_id": ledger_entry_id(completed),
            "currency": completed.currency,
            "memo": f"sale {completed.sku} x{completed.quantity} (wo {completed.work_order_id})",
            "lines": list(revenue_lines(completed)),
        }
        return CompletionResult(
            order=completed, completed_event=completed_event, ledger_entry=ledger_entry
        )
