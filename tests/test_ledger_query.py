"""Tests for the Ledger external-ERP read path.

Pure domain logic + contract validation, no infrastructure. Drives
`LedgerQuerier` with a stub `ErpReader` and a fixed clock, and validates the
produced `ledger.query.completed` / `ledger.query.failed` payloads against the
canonical contracts via the SchemaRegistry.

Part of issue #2 (canonical emitters): #1 landed the contracts; this adds the
Ledger read capability that emits them, alongside the existing posting path.
"""
from __future__ import annotations

import pytest

from agents.base.registry import SchemaRegistry
from agents.ledger.query import (
    DEFAULT_SCHEMA_VERSION,
    LedgerQuerier,
    LedgerQueryError,
)

FIXED_TS = "2026-07-04T12:00:00+00:00"

INVOICE_ROWS = [
    {"id": "INV-1", "total_minor": 10_000, "status": "open"},
    {"id": "INV-2", "total_minor": 25_000, "status": "open"},
]


class StubErpReader:
    """Returns canned rows and records the (tenant_id, subject, filters) asked for."""

    def __init__(self, rows: list[dict] | None = None) -> None:
        self.rows = INVOICE_ROWS if rows is None else rows
        self.calls: list = []

    def read(self, tenant_id, subject, filters):
        self.calls.append((tenant_id, subject, filters))
        return list(self.rows)


class ExplodingErpReader:
    def read(self, tenant_id, subject, filters):
        raise TimeoutError("upstream ERP timed out")


@pytest.fixture
def registry() -> SchemaRegistry:
    return SchemaRegistry()


def _querier(reader=None) -> LedgerQuerier:
    return LedgerQuerier(reader=reader or StubErpReader(), clock=lambda: FIXED_TS)


# --------------------------------------------------------------------------- #
# Success path
# --------------------------------------------------------------------------- #
def test_query_completed_matches_contract(registry: SchemaRegistry):
    reader = StubErpReader()
    payload = _querier(reader).query(
        "tenant-acme", "netsuite", "invoices", {"status": "open"}
    )

    registry.validate("ledger.query.completed", 1, payload)
    assert set(payload) == {
        "tenant_id", "erp_platform", "subject", "filters",
        "result_count", "payload", "schema_version", "queried_at",
    }
    assert payload["result_count"] == 2
    assert payload["payload"] == INVOICE_ROWS
    assert payload["filters"] == {"status": "open"}
    assert payload["schema_version"] == DEFAULT_SCHEMA_VERSION
    assert payload["queried_at"] == FIXED_TS
    # The reader was asked for exactly this tenant's resource + filters.
    assert reader.calls == [("tenant-acme", "invoices", {"status": "open"})]


def test_empty_result_is_a_valid_completed_query(registry: SchemaRegistry):
    payload = _querier(StubErpReader(rows=[])).query("t", "netsuite", "invoices", {})
    registry.validate("ledger.query.completed", 1, payload)
    assert payload["result_count"] == 0
    assert payload["payload"] == []


# --------------------------------------------------------------------------- #
# Failure paths
# --------------------------------------------------------------------------- #
def test_reader_error_becomes_query_failure(registry: SchemaRegistry):
    q = _querier(ExplodingErpReader())
    with pytest.raises(LedgerQueryError) as exc:
        q.query("tenant-acme", "netsuite", "invoices", {"status": "open"})
    assert "timed out" in str(exc.value)

    failed = q.failure("tenant-acme", "netsuite", "invoices", {"status": "open"}, str(exc.value))
    registry.validate("ledger.query.failed", 1, failed)
    assert set(failed) == {
        "tenant_id", "erp_platform", "subject", "filters", "reason", "failed_at",
    }
    assert failed["failed_at"] == FIXED_TS


def test_default_reader_refuses_until_configured():
    q = LedgerQuerier(clock=lambda: FIXED_TS)  # default UnconfiguredErpReader
    with pytest.raises(LedgerQueryError):
        q.query("t", "netsuite", "invoices", {})
