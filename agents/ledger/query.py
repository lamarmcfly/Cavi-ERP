"""Ledger — external ERP read path (domain logic).

The accounting core of Ledger (`ledger.py`) *posts* journal entries. This side is
the **read** path: it queries an external ERP for a resource (invoices, purchase
orders, …), normalizes the rows, and emits the canonical
`ledger.query.completed` / `ledger.query.failed` events.

The actual ERP call is an injected `ErpReader`, so the query path is fully
testable with no live ERP. A production reader signs the request with a
Vault-vended token and calls the ERP's REST API, returning **already-normalized**
rows tagged with `schema_version` (the shape callers can rely on). A read that
fails (auth, timeout, unknown subject) becomes `ledger.query.failed` — surfaced,
never a crash — so the caller can retry or escalate.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Callable, Mapping, Protocol, Sequence

from agents.ledger.ledger import LedgerError

# Version tag for the normalized row shape rows are returned in. Callers key off
# this to know how to read `payload[]`.
DEFAULT_SCHEMA_VERSION = "cavi.ledger.v1"


class LedgerQueryError(LedgerError):
    """An external ERP read could not be completed."""


class ErpReader(Protocol):
    """Reads a resource from an external ERP and returns normalized rows.

    Injected so the query path is testable without a live ERP. A production
    implementation signs with a Vault-vended token and calls the ERP REST API.
    """

    def read(self, subject: str, filters: Mapping) -> Sequence[dict]: ...


class UnconfiguredErpReader:
    """Default reader: refuses until a real reader is injected, so a
    misconfigured deployment fails loudly (as a query failure) instead of
    silently returning nothing."""

    def read(self, subject: str, filters: Mapping) -> Sequence[dict]:
        raise LedgerQueryError("no ERP reader configured; inject an ErpReader")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def completed_payload(
    tenant_id: str,
    erp_platform: str,
    subject: str,
    filters: Mapping,
    rows: Sequence[dict],
    schema_version: str,
    *,
    queried_at: str,
) -> dict:
    rows = list(rows)
    return {
        "tenant_id": tenant_id,
        "erp_platform": erp_platform,
        "subject": subject,
        "filters": dict(filters),
        "result_count": len(rows),
        "payload": rows,
        "schema_version": schema_version,
        "queried_at": queried_at,
    }


def failed_payload(
    tenant_id: str,
    erp_platform: str,
    subject: str,
    filters: Mapping,
    reason: str,
    *,
    failed_at: str,
) -> dict:
    return {
        "tenant_id": tenant_id,
        "erp_platform": erp_platform,
        "subject": subject,
        "filters": dict(filters),
        "reason": reason,
        "failed_at": failed_at,
    }


class LedgerQuerier:
    """Queries an external ERP through an injected `ErpReader`, producing
    canonical completed/failed payloads. `schema_version` tags the normalized row
    shape; `clock` is injectable so timestamps are deterministic under test."""

    def __init__(
        self,
        reader: ErpReader | None = None,
        *,
        schema_version: str = DEFAULT_SCHEMA_VERSION,
        clock: Callable[[], str] = _now_iso,
    ) -> None:
        self._reader = reader or UnconfiguredErpReader()
        self._schema_version = schema_version
        self._clock = clock

    def query(
        self, tenant_id: str, erp_platform: str, subject: str, filters: Mapping
    ) -> dict:
        """Read `subject` from the ERP and return a `ledger.query.completed`
        payload. Raises `LedgerQueryError` (with a human reason) on any read
        failure, including a misconfigured reader."""
        try:
            rows = self._reader.read(subject, dict(filters))
        except LedgerQueryError:
            raise
        except Exception as exc:  # any upstream read error is a query failure
            raise LedgerQueryError(
                f"read of {subject!r} from {erp_platform} failed: {exc}"
            ) from exc
        return completed_payload(
            tenant_id, erp_platform, subject, filters, rows, self._schema_version,
            queried_at=self._clock(),
        )

    def failure(
        self, tenant_id: str, erp_platform: str, subject: str, filters: Mapping, reason: str
    ) -> dict:
        """Build a `ledger.query.failed` payload stamped now."""
        return failed_payload(
            tenant_id, erp_platform, subject, filters, reason, failed_at=self._clock()
        )
