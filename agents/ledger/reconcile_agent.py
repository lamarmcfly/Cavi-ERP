"""Ledger — reconciliation runtime (event wiring).

Thin bus wiring around `reconcile.Reconciler`. The cadence lives outside (the
n8n `reconciliation-cadence` workflow publishes `ticker.reconciliation.due` on
a schedule — Ticker's clock, routed by the middleware); this agent runs the
pass when told to:

    ticker.reconciliation.due -> ledger.reconciliation.completed   (every run)
                              -> ledger.drift.detected             (drift only)

The two state sources are injected `Protocol`s:

  * `ExpectedStateSource` — what Cavi believes the ERP holds, keyed by external
    id (in production: a projection over the `forge.write` history in
    `event_log`).
  * `ErpStateSource` — what the ERP actually holds for the scope (in
    production: a Vault-signed NetSuite query over `externalid LIKE 'cavi-%'`;
    partner-scoped at the Epic 4 gate).

Fail closed, per FR9: with a source unconfigured or a fetch failing, the due
event is **dead-lettered** (Beacon sees it, it stays replayable) — the agent
never reconciles against guessed state, and never reports a clean pass it did
not actually run.
"""
from __future__ import annotations

import logging
from typing import Mapping, Protocol

from agents.base import BaseAgent, Event
from agents.ledger.reconcile import Reconciler

log = logging.getLogger("cavi.ledger.reconcile")


class ExpectedStateSource(Protocol):
    def records(self, tenant_id: str, scope: str) -> Mapping[str, Mapping]: ...


class ErpStateSource(Protocol):
    def records(
        self, tenant_id: str, erp_platform: str, scope: str
    ) -> Mapping[str, Mapping]: ...


class SourceUnavailable(Exception):
    """A state source is unconfigured or failed — the pass must not run."""


class UnconfiguredSource:
    """Default for both sources: refuses so a misconfigured deployment
    dead-letters loudly instead of reporting clean passes it never ran."""

    def records(self, *args, **kwargs) -> Mapping[str, Mapping]:
        raise SourceUnavailable("no reconciliation state source configured")


class LedgerReconcileAgent(BaseAgent):
    name = "ledger"

    def __init__(
        self,
        reconciler: Reconciler | None = None,
        *,
        expected_source: ExpectedStateSource | None = None,
        erp_source: ErpStateSource | None = None,
    ) -> None:
        super().__init__()
        self.reconciler = reconciler or Reconciler()
        self.expected_source = expected_source or UnconfiguredSource()
        self.erp_source = erp_source or UnconfiguredSource()

    @property
    def subjects(self) -> list[str]:
        return ["ticker.reconciliation.due"]

    def handle(self, event: Event) -> None:
        req = event.payload
        tenant_id = req["tenant_id"]
        erp_platform = req["erp_platform"]
        scope = req["scope"]

        try:
            expected = self.expected_source.records(tenant_id, scope)
            actual = self.erp_source.records(tenant_id, erp_platform, scope)
        except SourceUnavailable as exc:
            log.error("reconciliation pass could not run: %s", exc)
            self._dead_letter(event, f"reconciliation source unavailable: {exc}")
            return

        result = self.reconciler.run(
            tenant_id=tenant_id,
            erp_platform=erp_platform,
            scope=scope,
            expected=expected,
            actual=actual,
        )
        log.info(
            "reconciliation %s: checked=%d drift=%d (tenant=%s scope=%s)",
            result.run_id, result.checked, result.drift_count, tenant_id, scope,
        )
        self._emit("ledger.reconciliation.completed", result.completed_payload(), event)
        drift = result.drift_payload()
        if drift is not None:
            self._emit("ledger.drift.detected", drift, event)

    def _emit(self, subject: str, payload: dict, cause: Event) -> None:
        self.emit(
            Event(
                subject=subject,
                schema_version=1,
                source=self.name,
                correlation_id=cause.correlation_id,
                payload=payload,
            )
        )


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    LedgerReconcileAgent().run()
