"""Ledger — external ERP read path (event runtime).

Thin bus wiring around `query.LedgerQuerier`. Consumes a query request and emits
exactly one canonical outcome:

    ledger.query.request  ->  ledger.query.completed   (rows read)
                          ->  ledger.query.failed      (LedgerQueryError)

Request payload shape:
    {"tenant_id": ..., "erp_platform": "netsuite", "subject": "invoices",
     "filters": { ...query filters... }}

`ledger.query.request` is an internal trigger, not (yet) in the schema registry —
consistent with the other agents' inbound subjects. A read failure is emitted
(never crashes the agent) so the caller can retry or escalate.
"""
from __future__ import annotations

import logging

from agents.base import BaseAgent, Event
from agents.ledger.query import ErpReader, LedgerQuerier, LedgerQueryError

log = logging.getLogger("cavi.ledger.query")


class LedgerQueryAgent(BaseAgent):
    name = "ledger"

    def __init__(
        self,
        querier: LedgerQuerier | None = None,
        *,
        reader: ErpReader | None = None,
    ) -> None:
        super().__init__()
        self.querier = querier or LedgerQuerier(reader=reader)

    @property
    def subjects(self) -> list[str]:
        return ["ledger.query.request"]

    def handle(self, event: Event) -> None:
        req = event.payload
        subject, filters = req["subject"], req.get("filters", {})
        try:
            payload = self.querier.query(
                req["tenant_id"], req["erp_platform"], subject, filters
            )
            out_subject = "ledger.query.completed"
            log.info(
                "ledger queried %s from %s -> %d row(s)",
                subject, req["erp_platform"], payload["result_count"],
            )
        except LedgerQueryError as exc:
            payload = self.querier.failure(
                req["tenant_id"], req["erp_platform"], subject, filters, str(exc)
            )
            out_subject = "ledger.query.failed"
            log.warning("ledger query of %s failed: %s", subject, exc)

        self.emit(
            Event(
                subject=out_subject,
                schema_version=1,
                source=self.name,
                correlation_id=event.correlation_id,
                payload=payload,
            )
        )


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    LedgerQueryAgent().run()
