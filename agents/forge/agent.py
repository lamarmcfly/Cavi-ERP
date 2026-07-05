"""Forge — production & order fulfillment (event runtime).

Consumes `forge.workorder` events (a work order ready to complete) and emits:

  * `forge.completed` — the domain fact (Beacon, dashboards, etc. consume it)
  * `ledger.entry`    — the balanced financial consequence (Ledger consumes it)

This is the first end-to-end flow in Cavi ERP: a completed work order in Forge
becomes a posted journal entry in Ledger, with no direct call between them.
"""
from __future__ import annotations

import logging

from agents.base import BaseAgent, Event
from agents.forge.forge import Forge, InvalidTransition, WorkOrder, WorkOrderState

log = logging.getLogger("cavi.forge")


class ForgeAgent(BaseAgent):
    name = "forge"

    def __init__(self, forge: Forge | None = None) -> None:
        super().__init__()
        self.forge = forge or Forge()

    @property
    def subjects(self) -> list[str]:
        return ["forge.workorder"]

    def handle(self, event: Event) -> None:
        order = WorkOrder.from_payload(event.payload)
        # A work order arriving for fulfillment is treated as in progress.
        if order.state is WorkOrderState.CREATED:
            order = self.forge.start(order)
        try:
            result = self.forge.complete(order)
        except InvalidTransition as exc:
            log.warning("forge could not complete %s: %s", order.work_order_id, exc)
            return

        log.info(
            "forge completed %s (%s gross=%d)",
            result.order.work_order_id, result.order.currency, result.order.gross_minor,
        )
        # Emit the domain fact and the financial consequence, both scoped to the
        # work order's tenant so the audit log and the books stay tenant-isolated.
        tenant_id = result.order.tenant_id
        self.emit(
            Event(
                subject="forge.completed",
                schema_version=1,
                source=self.name,
                correlation_id=event.correlation_id,
                tenant_id=tenant_id,
                payload=result.completed_event,
            )
        )
        self.emit(
            Event(
                subject="ledger.entry",
                schema_version=1,
                source=self.name,
                correlation_id=event.correlation_id,
                tenant_id=tenant_id,
                payload=result.ledger_entry,
            )
        )


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    ForgeAgent().run()
