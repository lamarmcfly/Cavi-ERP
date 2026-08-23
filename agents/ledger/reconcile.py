"""Ledger — Cavi vs ERP reconciliation (domain logic).

Once Cavi writes into an external system of record, the two can drift: a write
lands then someone edits the record in the ERP, a record goes missing, an
unexpected one appears under Cavi's key-space. Reconciliation makes that
disagreement *visible on a schedule* instead of discovered during an audit.

Governance rules, per FR6 / Story 4.2:

  * **Advisory only.** A discrepancy raises a flag a human disposes — nothing
    here corrects, re-writes, or touches the ERP. A correction, if wanted, is
    a new `forge.write` proposal through the normal approval gate.
  * **Only managed fields are compared.** Cavi writes are upserts of the
    fields it manages; the ERP legitimately holds more. A field absent from
    the expected record is not drift.
  * Every run emits `ledger.reconciliation.completed` (the heartbeat that the
    check ran — silence is itself a signal); a run that found discrepancies
    additionally emits `ledger.drift.detected` carrying every one, which
    Beacon surfaces to a human.

Both sides arrive as plain mappings keyed by external id (the write path's
idempotency key), so the comparator is pure and testable with no ERP, no
database — the sources are the runtime's concern (`reconcile_agent.py`).
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Mapping

# Discrepancy kinds (the schema's enum mirrors these).
MISSING_IN_ERP = "missing_in_erp"        # Cavi expects it; the ERP has no record
UNEXPECTED_IN_ERP = "unexpected_in_erp"  # ERP holds a record Cavi never wrote
FIELD_MISMATCH = "field_mismatch"        # both exist; managed fields disagree


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _new_run_id() -> str:
    return str(uuid.uuid4())


@dataclass(frozen=True)
class Discrepancy:
    external_id: str
    kind: str
    fields: tuple[dict, ...] = ()  # for FIELD_MISMATCH: {field, expected, actual}

    def to_payload(self) -> dict:
        payload: dict = {"external_id": self.external_id, "kind": self.kind}
        if self.fields:
            payload["fields"] = [dict(f) for f in self.fields]
        return payload


def compare(
    expected: Mapping[str, Mapping],
    actual: Mapping[str, Mapping],
) -> list[Discrepancy]:
    """Compare Cavi-expected records against ERP-actual records.

    Both are keyed by external id. Deterministic ordering (sorted ids) so a
    rerun over the same state produces the identical report.
    """
    discrepancies: list[Discrepancy] = []
    for ext_id in sorted(expected):
        if ext_id not in actual:
            discrepancies.append(Discrepancy(ext_id, MISSING_IN_ERP))
            continue
        record, current = expected[ext_id], actual[ext_id]
        mismatches = tuple(
            {"field": name, "expected": record[name], "actual": current.get(name)}
            for name in sorted(record)
            if record[name] != current.get(name)
        )
        if mismatches:
            discrepancies.append(Discrepancy(ext_id, FIELD_MISMATCH, mismatches))
    for ext_id in sorted(set(actual) - set(expected)):
        discrepancies.append(Discrepancy(ext_id, UNEXPECTED_IN_ERP))
    return discrepancies


@dataclass(frozen=True)
class ReconciliationResult:
    tenant_id: str
    erp_platform: str
    scope: str                     # record type reconciled, e.g. "journalEntry"
    run_id: str
    checked: int                   # distinct external ids examined (both sides)
    discrepancies: tuple[Discrepancy, ...]
    completed_at: str

    @property
    def drift_count(self) -> int:
        return len(self.discrepancies)

    def completed_payload(self) -> dict:
        """`ledger.reconciliation.completed` — emitted for every run."""
        return {
            "tenant_id": self.tenant_id,
            "erp_platform": self.erp_platform,
            "scope": self.scope,
            "run_id": self.run_id,
            "checked": self.checked,
            "matched": self.checked - self.drift_count,
            "drift_count": self.drift_count,
            "completed_at": self.completed_at,
        }

    def drift_payload(self) -> dict | None:
        """`ledger.drift.detected` — only when the run found discrepancies."""
        if not self.discrepancies:
            return None
        return {
            "tenant_id": self.tenant_id,
            "erp_platform": self.erp_platform,
            "scope": self.scope,
            "run_id": self.run_id,
            "discrepancies": [d.to_payload() for d in self.discrepancies],
            "detected_at": self.completed_at,
        }


class Reconciler:
    """Runs one reconciliation pass and stamps the result. `clock` and
    `id_factory` are injectable so runs are deterministic under test."""

    def __init__(
        self,
        *,
        clock: Callable[[], str] = _now_iso,
        id_factory: Callable[[], str] = _new_run_id,
    ) -> None:
        self._clock = clock
        self._id = id_factory

    def run(
        self,
        *,
        tenant_id: str,
        erp_platform: str,
        scope: str,
        expected: Mapping[str, Mapping],
        actual: Mapping[str, Mapping],
    ) -> ReconciliationResult:
        return ReconciliationResult(
            tenant_id=tenant_id,
            erp_platform=erp_platform,
            scope=scope,
            run_id=self._id(),
            checked=len(set(expected) | set(actual)),
            discrepancies=tuple(compare(expected, actual)),
            completed_at=self._clock(),
        )
