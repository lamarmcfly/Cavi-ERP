"""Beacon — KPI reporting (domain logic).

Beacon watches the failure/outcome events every other agent produces (see
`beacon.py`). Those same observations roll up into a periodic operations report:
counts by severity, dead-letter volume, and totals for a window. This module
turns a sequence of observed subjects into a canonical `beacon.report.generated`
payload.

`summarize_kpis` reuses Beacon's `severity_for` policy so the report's severity
buckets match how alerts are classified — one source of truth for "what's
urgent." The result rides in the contract's free-form `kpis` object.
"""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
from typing import Callable, Iterable

from agents.beacon.beacon import Severity, severity_for


def summarize_kpis(observed_subjects: Iterable[str]) -> dict:
    """Roll a sequence of observed event subjects into a KPI map.

    Buckets by severity using Beacon's own `severity_for`, and calls out
    dead-letters (the events that most need a human). Pure and order-independent.
    """
    counts = Counter(observed_subjects)
    by_severity: Counter[str] = Counter()
    dead_letters = 0
    for subject, n in counts.items():
        by_severity[severity_for(subject).name] += n
        if subject.startswith("deadletter."):
            dead_letters += n

    return {
        "events_total": sum(counts.values()),
        "distinct_subjects": len(counts),
        "dead_letters": dead_letters,
        # Every severity present so consumers can chart a stable set of buckets.
        "by_severity": {sev.name: by_severity.get(sev.name, 0) for sev in Severity},
    }


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class ReportBuilder:
    """Builds `beacon.report.generated` payloads. `clock` is injectable so
    `generated_at` is deterministic under test."""

    def __init__(self, *, clock: Callable[[], str] = _now_iso) -> None:
        self._clock = clock

    def generate(
        self,
        tenant_id: str,
        report_type: str,
        observed_subjects: Iterable[str],
        *,
        period_start: str,
        period_end: str,
        delivery_targets: Iterable[str],
    ) -> dict:
        """Assemble a canonical report payload for one reporting window.

        `period_start`/`period_end` are ISO-8601 strings supplied by the caller
        (the scheduler that owns the window); `generated_at` is stamped now.
        """
        return {
            "tenant_id": tenant_id,
            "report_type": report_type,
            "kpis": summarize_kpis(observed_subjects),
            "period_start": period_start,
            "period_end": period_end,
            "generated_at": self._clock(),
            "delivery_targets": list(delivery_targets),
        }
