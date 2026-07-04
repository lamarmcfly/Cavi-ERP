"""Mapper — anti-corruption layer (event runtime).

Consumes `mapper.transform` requests and re-emits the coerced payload on the
same subject at the target version, so consumers expecting the newer contract
receive a valid event. Because Mapper validates against the target schema, it
never republishes a corrupt payload.

Request payload shape:
    {"subject": "ledger.entry", "from_version": 1, "to_version": 2,
     "payload": { ...the v1 event body... }}
"""
from __future__ import annotations

import logging

from agents.base import BaseAgent, Event
from agents.mapper.mapper import Mapper, NoTransformPath
from agents.mapper.transforms import register_all

log = logging.getLogger("cavi.mapper")


class MapperAgent(BaseAgent):
    name = "mapper"

    def __init__(self, mapper: Mapper | None = None) -> None:
        super().__init__()
        self.mapper = mapper or register_all(Mapper())

    @property
    def subjects(self) -> list[str]:
        return ["mapper.transform"]

    def handle(self, event: Event) -> None:
        req = event.payload
        subject = req["subject"]
        from_version = int(req["from_version"])
        to_version = int(req["to_version"])
        try:
            coerced = self.mapper.transform(
                subject, from_version, to_version, req["payload"]
            )
        except NoTransformPath as exc:
            log.warning("mapper cannot coerce %s: %s", subject, exc)
            return

        log.info("mapper coerced %s v%d -> v%d", subject, from_version, to_version)
        self.emit(
            Event(
                subject=subject,
                schema_version=to_version,
                source=self.name,
                correlation_id=event.correlation_id,
                payload=coerced,
            )
        )


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    MapperAgent().run()
