"""Beacon — notifications & alerting (event runtime).

Beacon subscribes to the failure/outcome subjects the other agents produce and
turns them into human-facing alerts. It is a terminal sink: it consumes events
and notifies people; it does not emit new events onto the bus.

In production, the dead-letter subjects would be caught with a pattern
subscription (`deadletter.*`). The base runtime subscribes to exact subjects, so
Beacon lists the concrete ones it cares about here.
"""
from __future__ import annotations

import logging

from agents.base import BaseAgent, Event
from agents.beacon.beacon import (
    Beacon,
    HermesChannel,
    LogChannel,
    NotificationChannel,
)

log = logging.getLogger("cavi.beacon")

# Where human alerts are delivered. The send_fn is a placeholder — wire it to
# the Hermes gateway webhook or messages_send in the orchestration layer.
BEACON_TARGET = "telegram:default"


def _placeholder_hermes_sender(target: str, text: str) -> None:
    # TODO: bridge to the Hermes gateway (webhook) or MCP messages_send.
    log.info("Beacon -> Hermes (%s): %s", target, text.splitlines()[0])


def _default_beacon() -> Beacon:
    channels: dict[str, NotificationChannel] = {
        "log": LogChannel(),
        "hermes": HermesChannel(BEACON_TARGET, _placeholder_hermes_sender),
    }
    return Beacon(channels)


class BeaconAgent(BaseAgent):
    name = "beacon"

    def __init__(self, beacon: Beacon | None = None) -> None:
        super().__init__()
        self.beacon = beacon or _default_beacon()

    @property
    def subjects(self) -> list[str]:
        return [
            "deadletter.ledger.entry",   # CRITICAL — money couldn't be parsed
            "ledger.rejected",            # WARNING  — unbalanced posting
            "vault.secret.denied",        # WARNING  — credential request refused
            "ledger.posted",              # INFO     — audit trail
            "forge.completed",            # INFO     — audit trail
        ]

    def handle(self, event: Event) -> None:
        alert = self.beacon.notify(event.subject, event.payload, event.correlation_id)
        if alert is None:
            log.debug("beacon suppressed duplicate for %s", event.subject)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    BeaconAgent().run()
