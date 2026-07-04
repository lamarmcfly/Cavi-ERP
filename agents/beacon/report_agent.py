"""Beacon — KPI reporting (event runtime).

A companion to the alerting `BeaconAgent`: it watches the same outcome/failure
subjects, keeps a running tally of what it has seen, and on a
`beacon.report.request` trigger rolls the window up into a canonical
`beacon.report.generated` event (delivered onward through the same Hermes
targets Beacon uses for alerts).

    <outcome subjects> ...    -> tallied in memory
    beacon.report.request     -> emit beacon.report.generated, reset the window

The tally is in-memory (fine single-process; a durable store is the follow-up
for multi-instance, same as Beacon's dedup). `beacon.report.request` is an
internal trigger, not (yet) in the schema registry — consistent with the other
agents' inbound subjects.
"""
from __future__ import annotations

import logging

from agents.base import BaseAgent, Event
from agents.beacon.report import ReportBuilder
from shared.settings import get_settings

log = logging.getLogger("cavi.beacon.report")

# The outcome/failure subjects worth reporting on — the set BeaconAgent alerts on.
OUTCOME_SUBJECTS = [
    "deadletter.ledger.entry",
    "ledger.rejected",
    "vault.secret.denied",
    "ledger.posted",
    "forge.completed",
]
REPORT_REQUEST = "beacon.report.request"


class BeaconReportAgent(BaseAgent):
    name = "beacon"

    def __init__(
        self,
        builder: ReportBuilder | None = None,
        *,
        default_targets: list[str] | None = None,
    ) -> None:
        super().__init__()
        self.builder = builder or ReportBuilder()
        self._default_targets = default_targets or [get_settings().beacon_target]
        self._observed: list[str] = []

    @property
    def subjects(self) -> list[str]:
        return [*OUTCOME_SUBJECTS, REPORT_REQUEST]

    def handle(self, event: Event) -> None:
        if event.subject == REPORT_REQUEST:
            self._emit_report(event)
        else:
            self._observed.append(event.subject)

    def _emit_report(self, event: Event) -> None:
        req = event.payload
        report = self.builder.generate(
            req["tenant_id"],
            req["report_type"],
            self._observed,
            period_start=req["period_start"],
            period_end=req["period_end"],
            delivery_targets=req.get("delivery_targets") or self._default_targets,
        )
        log.info(
            "beacon generated %s report over %d event(s)",
            report["report_type"], report["kpis"]["events_total"],
        )
        self.emit(
            Event(
                subject="beacon.report.generated",
                schema_version=1,
                source=self.name,
                correlation_id=event.correlation_id,
                payload=report,
            )
        )
        # New window starts fresh.
        self._observed = []


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    BeaconReportAgent().run()
