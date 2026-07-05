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
from typing import Callable

import httpx

from agents.base import BaseAgent, Event
from agents.beacon.beacon import (
    Beacon,
    HermesChannel,
    LogChannel,
    NotificationChannel,
    RedisDedupStore,
)
from shared.settings import get_settings

log = logging.getLogger("cavi.beacon")

# HTTP transport signature: (url, ...) -> response exposing .raise_for_status().
Poster = Callable[..., httpx.Response]


def build_hermes_sender(
    webhook_url: str, *, post: Poster | None = None
) -> Callable[[str, str], None]:
    """Build the transport Beacon uses to reach a human through Hermes.

    Delivers by POSTing ``{"target", "message"}`` to the Hermes gateway
    ``/notify`` endpoint — the same contract the n8n ``deadletter-escalation``
    workflow speaks (see middleware/n8n/workflows/deadletter-escalation.json),
    so the Python and n8n escalation paths stay interchangeable.

    A delivery failure must never take Beacon (or the agent) down — surviving
    failures elsewhere is Beacon's whole job — so network/HTTP errors are logged
    at ERROR and swallowed. With no webhook configured, delivery degrades to
    log-only, keeping local/dev and tests side-effect free.

    ``post`` is injectable so tests can assert the payload without real HTTP.
    """
    poster = post or httpx.post

    def send(target: str, text: str) -> None:
        if not webhook_url:
            log.info(
                "Beacon -> Hermes (%s) [no webhook configured]: %s",
                target, text.splitlines()[0],
            )
            return
        try:
            response = poster(
                webhook_url,
                json={"target": target, "message": text},
                timeout=10.0,
            )
            response.raise_for_status()
        except httpx.HTTPError as exc:
            log.error("Beacon -> Hermes delivery to %s failed: %s", target, exc)

    return send


def _default_beacon() -> Beacon:
    settings = get_settings()
    channels: dict[str, NotificationChannel] = {
        "log": LogChannel(),
        "hermes": HermesChannel(
            settings.beacon_target,
            build_hermes_sender(settings.hermes_webhook_url),
        ),
    }
    # Durable, fleet-wide dedup so a restart/replica doesn't re-page humans.
    return Beacon(channels, dedup=RedisDedupStore())


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
    from agents.base.runtime import run_agent

    run_agent(BeaconAgent())
