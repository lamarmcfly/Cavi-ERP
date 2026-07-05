"""Ledger — the double-entry accounting core (event runtime).

Consumes `ledger.entry` events, posts balanced journal entries via the Ledger
domain library, and emits the outcome:

  * `ledger.posted`   — entry accepted and persisted
  * `ledger.rejected` — well-formed but unbalanced (a *business* rejection)

Malformed events never reach `handle()` — `BaseAgent._dispatch` validates them
against the schema registry first and dead-letters failures.
"""
from __future__ import annotations

import logging

from agents.base import BaseAgent, Event
from agents.ledger.ledger import JournalEntry, Ledger, UnbalancedEntry

log = logging.getLogger("cavi.ledger")


class LedgerAgent(BaseAgent):
    name = "ledger"

    def __init__(self, ledger: Ledger | None = None) -> None:
        super().__init__()
        self.ledger = ledger or Ledger()

    @property
    def subjects(self) -> list[str]:
        return ["ledger.entry"]

    def handle(self, event: Event) -> None:
        tenant_id = event.tenant_id
        if not tenant_id:
            # Defensive: producers must set the envelope tenant_id. Never post to
            # an unknown tenant's books; the journal columns are NOT NULL too.
            log.error("ledger dropped entry without tenant_id: %s",
                      event.payload.get("entry_id"))
            return
        entry = JournalEntry.from_payload(event.payload, tenant_id=tenant_id)
        try:
            result = self.ledger.post(entry)
        except UnbalancedEntry as exc:
            log.warning("ledger rejected %s: %s", entry.entry_id, exc)
            self.emit(
                Event(
                    subject="ledger.rejected",
                    schema_version=1,
                    source=self.name,
                    correlation_id=event.correlation_id,
                    tenant_id=tenant_id,
                    payload={
                        "entry_id": entry.entry_id,
                        "currency": entry.currency,
                        "reason": str(exc),
                        "total_debits": entry.total_debits,
                        "total_credits": entry.total_credits,
                    },
                )
            )
            return

        if result.status == "duplicate":
            # At-least-once delivery replayed an already-posted entry — ignore.
            log.info("ledger ignored duplicate %s", entry.entry_id)
            return

        log.info("ledger posted %s (%s %d)", entry.entry_id, entry.currency, result.total_minor)
        self.emit(
            Event(
                subject="ledger.posted",
                schema_version=1,
                source=self.name,
                correlation_id=event.correlation_id,
                tenant_id=tenant_id,
                payload={
                    "entry_id": result.entry_id,
                    "currency": result.currency,
                    "total_minor": result.total_minor,
                    "line_count": result.line_count,
                },
            )
        )


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    LedgerAgent().run()
