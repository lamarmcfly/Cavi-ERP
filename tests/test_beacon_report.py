"""Tests for Beacon KPI reporting.

Pure aggregation + contract validation, no infrastructure. Checks that
`summarize_kpis` buckets observed subjects correctly (reusing Beacon's severity
policy) and that `ReportBuilder` produces a contract-valid
`beacon.report.generated` payload.

Part of issue #2 (canonical emitters): #1 landed the contract; this adds the
Beacon reporting capability that emits it.
"""
from __future__ import annotations

import pytest

from agents.base.registry import SchemaRegistry
from agents.beacon.beacon import Severity
from agents.beacon.report import ReportBuilder, summarize_kpis

FIXED_TS = "2026-07-05T00:05:00+00:00"

# A representative window: two critical dead-letters, a business rejection, and
# two routine facts.
OBSERVED = [
    "deadletter.ledger.entry",   # CRITICAL (money couldn't be parsed)
    "deadletter.ledger.entry",   # CRITICAL (same, counted twice)
    "ledger.rejected",           # WARNING
    "ledger.posted",             # INFO
    "forge.completed",           # INFO
]


@pytest.fixture
def registry() -> SchemaRegistry:
    return SchemaRegistry()


def _builder() -> ReportBuilder:
    return ReportBuilder(clock=lambda: FIXED_TS)


# --------------------------------------------------------------------------- #
# KPI aggregation
# --------------------------------------------------------------------------- #
def test_summarize_kpis_buckets_by_severity_and_counts_deadletters():
    kpis = summarize_kpis(OBSERVED)
    assert kpis["events_total"] == 5
    assert kpis["distinct_subjects"] == 4
    assert kpis["dead_letters"] == 2
    assert kpis["by_severity"] == {
        "INFO": 2, "WARNING": 1, "ERROR": 0, "CRITICAL": 2,
    }


def test_summarize_kpis_always_lists_every_severity_bucket():
    kpis = summarize_kpis([])
    assert kpis["events_total"] == 0
    assert kpis["dead_letters"] == 0
    # Stable bucket set even for an empty window, so dashboards don't break.
    assert set(kpis["by_severity"]) == {s.name for s in Severity}
    assert all(v == 0 for v in kpis["by_severity"].values())


# --------------------------------------------------------------------------- #
# Report payload
# --------------------------------------------------------------------------- #
def test_generated_report_matches_canonical_contract(registry: SchemaRegistry):
    report = _builder().generate(
        "tenant-acme",
        "weekly_ops",
        OBSERVED,
        period_start="2026-06-28T00:00:00+00:00",
        period_end="2026-07-05T00:00:00+00:00",
        delivery_targets=["telegram:1370595013"],
    )

    registry.validate("beacon.report.generated", 1, report)
    assert set(report) == {
        "tenant_id", "report_type", "kpis",
        "period_start", "period_end", "generated_at", "delivery_targets",
    }
    assert report["report_type"] == "weekly_ops"
    assert report["generated_at"] == FIXED_TS
    assert report["delivery_targets"] == ["telegram:1370595013"]
    assert report["kpis"]["events_total"] == 5


def test_empty_window_still_produces_a_valid_report(registry: SchemaRegistry):
    report = _builder().generate(
        "tenant-acme",
        "weekly_ops",
        [],
        period_start="2026-06-28T00:00:00+00:00",
        period_end="2026-07-05T00:00:00+00:00",
        delivery_targets=["telegram:1370595013"],
    )
    registry.validate("beacon.report.generated", 1, report)
    assert report["kpis"]["events_total"] == 0
