"""Forge — ERP write lifecycle (event runtime).

Thin bus wiring around `write.WriteCoordinator`. It turns inbound triggers into
the canonical `forge.write.*` events:

    forge.write.propose  -> emits forge.write.requested (proposal recorded)
    forge.write.decision -> emits forge.write.approved + forge.write.completed
                            (on approve), or forge.write.rejected (on reject)
    forge.write.reverse  -> emits forge.write.requested for a compensating
                            write linked (`reverses`) to a COMPLETED original;
                            the reversal then flows through the same decision
                            gate as any other write

Two W9 protections wrap execution:

  * a **circuit breaker**, scoped per (tenant, ERP) so one tenant's outage
    never halts a healthy tenant — consecutive ERP failures trip it, after which
    executes are refused up front (writes stay APPROVED and retryable) and the
    trip itself is escalated by dead-lettering the triggering decision, so
    Beacon pages a human instead of the agent hammering a broken ERP;
  * every execute is metered (W10): lifecycle stage counts, failures by
    reason, summed latency, breaker trips — scrapeable at /metrics.

Pending proposals and completed writes are held in memory keyed by `write_id`
so decisions and reversals can be matched to their originals. That is fine for
a single-process runtime; a durable store is the follow-up needed for
multi-instance / crash-safe operation (the same limitation Beacon's in-memory
dedup has). Like the other agents, the inbound trigger subjects are internal
and not (yet) in the schema registry.

The ERP call is an injected `ErpWriter`; the default refuses to execute until a
real one is wired, so a misconfigured deploy fails loudly.
"""
from __future__ import annotations

import logging
import time
from typing import Callable

from agents.base import BaseAgent, Event
from agents.forge.breaker import BreakerOpen, CircuitBreaker
from agents.forge.forge import InvalidTransition
from agents.forge.write import (
    ErpReader,
    ErpWriteError,
    ErpWriter,
    WriteCoordinator,
    WriteOperation,
    WriteState,
    WriteStep,
)
from shared import metrics

log = logging.getLogger("cavi.forge.write")


class ForgeWriteAgent(BaseAgent):
    name = "forge"

    def __init__(
        self,
        coordinator: WriteCoordinator | None = None,
        *,
        writer: ErpWriter | None = None,
        reader: ErpReader | None = None,
        breaker_factory: Callable[[], CircuitBreaker] | None = None,
        **base_kwargs,
    ) -> None:
        super().__init__(**base_kwargs)
        self.coordinator = coordinator or WriteCoordinator(writer=writer, reader=reader)
        # Breakers are scoped per (tenant, ERP): credentials, rate limits, and
        # outages are tenant-scoped, so one tenant's broken connection must
        # never halt writes for a healthy tenant.
        self._breaker_factory = breaker_factory or CircuitBreaker
        self._breakers: dict[tuple[str, str], CircuitBreaker] = {}
        self._pending: dict[str, WriteOperation] = {}
        self._completed: dict[str, WriteOperation] = {}

    def _breaker_for(self, op: WriteOperation) -> CircuitBreaker:
        key = (op.tenant_id, op.erp_platform)
        breaker = self._breakers.get(key)
        if breaker is None:
            breaker = self._breakers[key] = self._breaker_factory()
        return breaker

    @property
    def subjects(self) -> list[str]:
        return ["forge.write.propose", "forge.write.decision", "forge.write.reverse"]

    def handle(self, event: Event) -> None:
        if event.subject == "forge.write.propose":
            self._propose(event)
        elif event.subject == "forge.write.decision":
            self._decide(event)
        elif event.subject == "forge.write.reverse":
            self._reverse(event)

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

    def _reverse(self, event: Event) -> None:
        req = event.payload
        original = self._completed.get(req["write_id"])
        if original is None:
            # Fail closed: a reversal must reference a write this runtime can
            # prove completed — quarantine rather than compensate blind.
            log.error("forge.write reverse for unknown/uncompleted %s", req["write_id"])
            self._dead_letter(
                event, f"reversal target {req['write_id']} is not a completed write"
            )
            return
        try:
            step = self.coordinator.request_reversal(
                original,
                operation=req["operation"],
                payload=req["payload"],
                requested_by=req["requested_by"],
                diff_preview=req.get("diff_preview"),
            )
        except ErpWriteError as exc:
            log.error("forge.write reverse failed dry-run: %s", exc)
            self._dead_letter(event, f"dry-run failed: {exc}")
            return
        # From here the reversal is an ordinary pending write: it needs its own
        # forge.write.decision approval before anything reaches the ERP.
        self._pending[step.op.write_id] = step.op
        self._emit(step, event.correlation_id)

    def _decide(self, event: Event) -> None:
        write_id = event.payload["write_id"]
        op = self._pending.get(write_id)
        if op is None:
            log.warning("forge.write decision for unknown write_id %s", write_id)
            return

        try:
            if event.payload["decision"] == "approve":
                if op.state is WriteState.APPROVED:
                    # Replay of an approval already granted — a dead-lettered
                    # decision re-driven after an execute failure or an open
                    # breaker. The approval stands; don't re-approve (that's an
                    # illegal APPROVED -> APPROVED transition), just retry the
                    # execution.
                    self._execute(op, event, event.correlation_id)
                    return
                approved = self.coordinator.approve(op, event.payload["reviewer"])
                self._pending[write_id] = approved.op
                self._emit(approved, event.correlation_id)
                self._execute(approved.op, event, event.correlation_id)
            else:
                rejected = self.coordinator.reject(
                    op, event.payload["reviewer"], event.payload.get("reason", "rejected")
                )
                self._pending.pop(write_id, None)
                self._emit(rejected, event.correlation_id)
        except InvalidTransition as exc:
            # Any other illegal decision (e.g. rejecting an already-approved
            # write) is quarantined, not crashed on: the agent loop must
            # survive a bad decision event, and a human sees it via Beacon.
            log.error("forge.write decision rejected by state machine: %s", exc)
            self._dead_letter(event, f"illegal decision: {exc}")

    def _execute(
        self, op: WriteOperation, cause: Event, correlation_id: str | None
    ) -> None:
        breaker = self._breaker_for(op)
        try:
            breaker.check()
        except BreakerOpen as exc:
            # The write stays APPROVED and retryable; the decision event is
            # quarantined so the refusal is visible and replayable once the
            # ERP recovers — never a silent drop of an approved write.
            log.error("forge.write %s refused: %s", op.write_id, exc)
            metrics.REGISTRY.inc(metrics.ERP_WRITE_FAILURES, reason="breaker_open")
            self._dead_letter(cause, str(exc))
            return

        started = time.monotonic()
        try:
            completed = self.coordinator.execute(op)
        except ErpWriteError as exc:
            # Write stays APPROVED (retryable); do not emit completed.
            log.error("forge.write execute failed for %s: %s", op.write_id, exc)
            metrics.REGISTRY.inc(metrics.ERP_WRITE_FAILURES, reason="erp_error")
            if breaker.record_failure():
                # Transition to OPEN — the one escalation moment (Story 6.2).
                log.critical(
                    "forge.write circuit breaker OPEN for tenant %s (%s) "
                    "after repeated ERP failures", op.tenant_id, op.erp_platform,
                )
                metrics.REGISTRY.inc(metrics.BREAKER_OPENS)
                self._dead_letter(
                    cause, f"circuit breaker opened; last error: {exc}"
                )
            return
        breaker.record_success()
        metrics.REGISTRY.inc(
            metrics.ERP_WRITE_SECONDS, value=time.monotonic() - started
        )
        self._pending.pop(op.write_id, None)
        self._completed[completed.op.write_id] = completed.op
        self._emit(completed, correlation_id)

    def _emit(self, step: WriteStep, correlation_id: str | None) -> None:
        stage = step.subject.rsplit(".", 1)[-1]  # requested|approved|rejected|completed
        metrics.REGISTRY.inc(metrics.ERP_WRITES, stage=stage)
        self.emit(
            Event(
                subject=step.subject,
                schema_version=step.schema_version,
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
