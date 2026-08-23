"""Forge — ERP write lifecycle (event runtime).

Thin bus wiring around `write.WriteCoordinator`. It turns inbound triggers into
the canonical `forge.write.*` events:

    forge.write.propose  -> emits forge.write.requested (proposal recorded)
    forge.write.decision -> emits forge.write.approved + forge.write.completed
                            (on approve), or forge.write.rejected (on reject)

Pending proposals are held in memory keyed by `write_id` so a later decision can
be matched to its request. That is fine for a single-process runtime; a durable
store is the follow-up needed for multi-instance / crash-safe operation (the
same limitation Beacon's in-memory dedup has). Like the other agents, the
inbound trigger subjects are internal and not (yet) in the schema registry.

The ERP call is an injected `ErpWriter`; the default refuses to execute until a
real one is wired, so a misconfigured deploy fails loudly.
"""
from __future__ import annotations

import logging

from agents.base import BaseAgent, Event
from agents.forge.write import (
    ErpReader,
    ErpWriteError,
    ErpWriter,
    WriteCoordinator,
    WriteOperation,
    WriteStep,
)

log = logging.getLogger("cavi.forge.write")


class ForgeWriteAgent(BaseAgent):
    name = "forge"

    def __init__(
        self,
        coordinator: WriteCoordinator | None = None,
        *,
        writer: ErpWriter | None = None,
        reader: ErpReader | None = None,
    ) -> None:
        super().__init__()
        self.coordinator = coordinator or WriteCoordinator(writer=writer, reader=reader)
        self._pending: dict[str, WriteOperation] = {}

    @property
    def subjects(self) -> list[str]:
        return ["forge.write.propose", "forge.write.decision"]

    def handle(self, event: Event) -> None:
        if event.subject == "forge.write.propose":
            self._propose(event)
        elif event.subject == "forge.write.decision":
            self._decide(event)

    # --- inbound handlers ---------------------------------------------------
    def _propose(self, event: Event) -> None:
        try:
            step = self.coordinator.request(**event.payload)
        except ErpWriteError as exc:
            # Dry-run fetch failed: fail closed. No proposal is recorded with a
            # fabricated diff — the event is quarantined for Beacon/replay so a
            # human sees it rather than the bus dropping it silently.
            log.error("forge.write propose failed dry-run: %s", exc)
            self._dead_letter(event, f"dry-run failed: {exc}")
            return
        self._pending[step.op.write_id] = step.op
        self._emit(step, event.correlation_id)

    def _decide(self, event: Event) -> None:
        write_id = event.payload["write_id"]
        op = self._pending.get(write_id)
        if op is None:
            log.warning("forge.write decision for unknown write_id %s", write_id)
            return

        if event.payload["decision"] == "approve":
            approved = self.coordinator.approve(op, event.payload["reviewer"])
            self._pending[write_id] = approved.op
            self._emit(approved, event.correlation_id)
            self._execute(approved.op, event.correlation_id)
        else:
            rejected = self.coordinator.reject(
                op, event.payload["reviewer"], event.payload.get("reason", "rejected")
            )
            self._pending.pop(write_id, None)
            self._emit(rejected, event.correlation_id)

    def _execute(self, op: WriteOperation, correlation_id: str | None) -> None:
        try:
            completed = self.coordinator.execute(op)
        except ErpWriteError as exc:
            # Write stays APPROVED (retryable); do not emit completed.
            log.error("forge.write execute failed for %s: %s", op.write_id, exc)
            return
        self._pending.pop(op.write_id, None)
        self._emit(completed, correlation_id)

    def _emit(self, step: WriteStep, correlation_id: str | None) -> None:
        self.emit(
            Event(
                subject=step.subject,
                schema_version=1,
                source=self.name,
                correlation_id=correlation_id,
                payload=step.event,
            )
        )


def build_agent() -> ForgeWriteAgent:
    """Production wiring: the Vault-signed NetSuite client as both writer and
    dry-run reader when VAULT_URL / CAVI_VAULT_API_SECRET / NETSUITE_REST_URL
    are configured. Left unconfigured, the default `UnconfiguredErpWriter`
    keeps every approved write failing closed instead of posting silently."""
    from agents.forge.netsuite import NetSuiteClient
    from shared.settings import get_settings

    s = get_settings()
    if s.vault_url and s.vault_api_secret and s.netsuite_rest_url:
        client = NetSuiteClient.from_settings()
        log.info("forge.write using NetSuite client via Vault at %s", s.vault_url)
        return ForgeWriteAgent(writer=client, reader=client)
    log.warning(
        "forge.write has no NetSuite client configured (set VAULT_URL, "
        "CAVI_VAULT_API_SECRET, NETSUITE_REST_URL); approved writes will fail closed"
    )
    return ForgeWriteAgent()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    build_agent().run()
