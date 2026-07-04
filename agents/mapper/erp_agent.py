"""Mapper — ERP-schema transformation (event runtime).

Thin bus wiring around `erp.ErpTransformer`. Consumes a transform request and
emits exactly one canonical outcome:

    mapper.erp.transform  ->  mapper.transform.completed   (on success)
                          ->  mapper.transform.failed      (on ErpTransformError)

Request payload shape:
    {"tenant_id": ..., "source_erp": "sap", "source_schema": "sap.invoice.v2",
     "target_schema": "cavi.invoice.v1", "record": { ...source record... }}

`mapper.erp.transform` is an internal trigger, not (yet) in the schema registry —
consistent with the other agents' inbound subjects. A failure is emitted (never
crashes the agent) so a bad record stays visible and replayable.
"""
from __future__ import annotations

import logging

from agents.base import BaseAgent, Event
from agents.mapper.erp import ErpTransformError, ErpTransformer

log = logging.getLogger("cavi.mapper.erp")


class ErpMapperAgent(BaseAgent):
    name = "mapper"

    def __init__(self, transformer: ErpTransformer | None = None) -> None:
        super().__init__()
        self.transformer = transformer or ErpTransformer()

    @property
    def subjects(self) -> list[str]:
        return ["mapper.erp.transform"]

    def handle(self, event: Event) -> None:
        req = event.payload
        try:
            payload = self.transformer.transform(
                req["tenant_id"],
                req["source_erp"],
                req["source_schema"],
                req["target_schema"],
                req["record"],
            )
            subject = "mapper.transform.completed"
            log.info(
                "mapper transformed %s -> %s (%s)",
                req["source_schema"], req["target_schema"], payload["input_hash"],
            )
        except ErpTransformError as exc:
            payload = self.transformer.failure(
                req["tenant_id"], req["source_erp"], req["source_schema"], str(exc)
            )
            subject = "mapper.transform.failed"
            log.warning("mapper could not transform %s: %s", req.get("source_schema"), exc)

        self.emit(
            Event(
                subject=subject,
                schema_version=1,
                source=self.name,
                correlation_id=event.correlation_id,
                payload=payload,
            )
        )


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    ErpMapperAgent().run()
