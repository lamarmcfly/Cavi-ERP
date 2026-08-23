"""Tests for Cavi-vs-ERP reconciliation and drift detection (Epic 4 / FR6).

Pure domain + contract validation, no infrastructure. Drives the `Reconciler`
with fixed clock/run-id over in-memory expected/actual states and validates
the produced payloads against the canonical `ledger.reconciliation.completed`
and `ledger.drift.detected` contracts. The governance rules proven here:

  * every pass reports (the completed event is the heartbeat);
  * drift is advisory — the result carries discrepancies, it corrects nothing;
  * only Cavi-managed fields are compared: extra ERP-side fields are not drift;
  * an injected discrepancy of each kind is detected and correctly classified.
"""
from __future__ import annotations

import pytest

from agents.base.registry import SchemaRegistry
from agents.ledger.reconcile import (
    FIELD_MISMATCH,
    MISSING_IN_ERP,
    UNEXPECTED_IN_ERP,
    Reconciler,
    compare,
)

FIXED_TS = "2026-07-04T12:00:00+00:00"
FIXED_RUN = "run_test_1"

EXPECTED = {
    "cavi-w1": {"memo": "posting 1", "currency": "USD"},
    "cavi-w2": {"memo": "posting 2", "currency": "USD"},
}


@pytest.fixture
def registry() -> SchemaRegistry:
    return SchemaRegistry()


def _reconciler() -> Reconciler:
    return Reconciler(clock=lambda: FIXED_TS, id_factory=lambda: FIXED_RUN)


def _run(expected, actual):
    return _reconciler().run(
        tenant_id="tenant-acme", erp_platform="netsuite", scope="journalEntry",
        expected=expected, actual=actual,
    )


# --------------------------------------------------------------------------- #
# Clean pass
# --------------------------------------------------------------------------- #
def test_clean_pass_reports_heartbeat_and_no_drift(registry: SchemaRegistry):
    result = _run(EXPECTED, dict(EXPECTED))
    payload = result.completed_payload()
    registry.validate("ledger.reconciliation.completed", 1, payload)
    assert payload == {
        "tenant_id": "tenant-acme", "erp_platform": "netsuite",
        "scope": "journalEntry", "run_id": FIXED_RUN,
        "checked": 2, "matched": 2, "drift_count": 0,
        "completed_at": FIXED_TS,
    }
    assert result.drift_payload() is None   # no drift event on a clean pass


def test_extra_erp_side_fields_are_not_drift():
    # The ERP legitimately holds fields Cavi does not manage (ids, revisions,
    # audit stamps) — comparing them would page humans about nothing.
    actual = {
        k: {**v, "id": "123", "lastModifiedDate": "2026-07-04"}
        for k, v in EXPECTED.items()
    }
    assert compare(EXPECTED, actual) == []


# --------------------------------------------------------------------------- #
# Injected discrepancies — each kind detected and classified (Epic 4 gate)
# --------------------------------------------------------------------------- #
def test_missing_in_erp_is_detected():
    actual = {"cavi-w1": EXPECTED["cavi-w1"]}   # w2 vanished from the ERP
    [d] = compare(EXPECTED, actual)
    assert (d.external_id, d.kind) == ("cavi-w2", MISSING_IN_ERP)


def test_unexpected_in_erp_is_detected():
    actual = {**EXPECTED, "cavi-w9": {"memo": "who wrote this?"}}
    [d] = compare(EXPECTED, actual)
    assert (d.external_id, d.kind) == ("cavi-w9", UNEXPECTED_IN_ERP)


def test_field_mismatch_carries_expected_and_actual_values():
    actual = {
        "cavi-w1": {"memo": "posting 1", "currency": "USD"},
        "cavi-w2": {"memo": "EDITED IN ERP", "currency": "USD"},
    }
    [d] = compare(EXPECTED, actual)
    assert (d.external_id, d.kind) == ("cavi-w2", FIELD_MISMATCH)
    assert d.fields == (
        {"field": "memo", "expected": "posting 2", "actual": "EDITED IN ERP"},
    )


def test_drift_event_validates_and_correlates_with_the_heartbeat(
    registry: SchemaRegistry,
):
    actual = {
        "cavi-w1": {"memo": "posting 1", "currency": "EUR"},   # edited
        # cavi-w2 missing entirely
        "cavi-w9": {"memo": "unexpected"},
    }
    result = _run(EXPECTED, actual)

    completed = result.completed_payload()
    drift = result.drift_payload()
    registry.validate("ledger.reconciliation.completed", 1, completed)
    registry.validate("ledger.drift.detected", 1, drift)

    assert completed["checked"] == 3 and completed["drift_count"] == 3
    assert completed["matched"] == 0
    assert drift["run_id"] == completed["run_id"]   # one pass, two events
    kinds = {(d["external_id"], d["kind"]) for d in drift["discrepancies"]}
    assert kinds == {
        ("cavi-w1", FIELD_MISMATCH),
        ("cavi-w2", MISSING_IN_ERP),
        ("cavi-w9", UNEXPECTED_IN_ERP),
    }


def test_reruns_over_identical_state_produce_identical_reports():
    actual = {"cavi-w1": {"memo": "x", "currency": "USD"}}
    assert compare(EXPECTED, actual) == compare(EXPECTED, actual)


# --------------------------------------------------------------------------- #
# Cadence contract
# --------------------------------------------------------------------------- #
def test_reconciliation_due_trigger_matches_its_contract(registry: SchemaRegistry):
    # The envelope the n8n reconciliation-cadence workflow publishes.
    registry.validate(
        "ticker.reconciliation.due", 1,
        {
            "tenant_id": "tenant-acme", "erp_platform": "netsuite",
            "scope": "journalEntry", "due_at": FIXED_TS,
        },
    )
