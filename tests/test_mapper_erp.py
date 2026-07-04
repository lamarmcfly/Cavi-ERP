"""Tests for Mapper ERP-schema transformation.

Pure domain logic + contract validation, no infrastructure. Registers a
source->target transform, drives `ErpTransformer` with a fixed clock, and
validates the produced `mapper.transform.completed` / `mapper.transform.failed`
payloads against the canonical contracts via the SchemaRegistry.

Part of issue #2 (canonical emitters): #1 landed the contracts; this adds the
Mapper ERP-transform capability that emits them, alongside the existing
version-coercion path.
"""
from __future__ import annotations

import pytest

from agents.base.registry import SchemaRegistry
from agents.mapper.erp import ErpTransformError, ErpTransformer, input_hash

FIXED_TS = "2026-07-04T12:00:00+00:00"

SAP_INVOICE = {"DocNum": "INV-1", "NetAmount": 100, "Currency": "USD"}


def _sap_to_cavi(record: dict) -> dict:
    """Sample transform: SAP invoice shape -> canonical cavi invoice shape."""
    return {
        "id": record["DocNum"],
        "total_minor": int(record["NetAmount"]) * 100,
        "currency": record["Currency"],
    }


@pytest.fixture
def registry() -> SchemaRegistry:
    return SchemaRegistry()


def _transformer() -> ErpTransformer:
    t = ErpTransformer(clock=lambda: FIXED_TS)
    t.register("sap.invoice.v2", "cavi.invoice.v1", _sap_to_cavi)
    return t


# --------------------------------------------------------------------------- #
# Success path
# --------------------------------------------------------------------------- #
def test_transform_completed_matches_contract(registry: SchemaRegistry):
    payload = _transformer().transform(
        "tenant-acme", "sap", "sap.invoice.v2", "cavi.invoice.v1", SAP_INVOICE
    )
    registry.validate("mapper.transform.completed", 1, payload)
    assert set(payload) == {
        "tenant_id", "source_erp", "source_schema", "target_schema",
        "input_hash", "output", "transformed_at",
    }
    assert payload["output"] == {"id": "INV-1", "total_minor": 10_000, "currency": "USD"}
    assert payload["input_hash"] == input_hash(SAP_INVOICE)
    assert payload["transformed_at"] == FIXED_TS


# --------------------------------------------------------------------------- #
# input_hash properties
# --------------------------------------------------------------------------- #
def test_input_hash_is_stable_and_order_independent():
    a = input_hash({"a": 1, "b": 2})
    b = input_hash({"b": 2, "a": 1})   # same content, different key order
    assert a == b and a.startswith("sha256:")


def test_input_hash_differs_for_different_records():
    assert input_hash({"a": 1}) != input_hash({"a": 2})


# --------------------------------------------------------------------------- #
# Failure paths
# --------------------------------------------------------------------------- #
def test_unregistered_mapping_raises_and_failure_payload_validates(registry: SchemaRegistry):
    t = _transformer()
    with pytest.raises(ErpTransformError):
        t.transform("tenant-acme", "sap", "sap.invoice.v2", "unknown.target", SAP_INVOICE)

    failed = t.failure("tenant-acme", "sap", "sap.invoice.v2", "no transform registered")
    registry.validate("mapper.transform.failed", 1, failed)
    assert set(failed) == {"tenant_id", "source_erp", "source_schema", "reason", "failed_at"}
    assert failed["failed_at"] == FIXED_TS


def test_a_raising_transform_becomes_a_business_failure():
    t = ErpTransformer(clock=lambda: FIXED_TS)
    t.register("sap.invoice.v2", "cavi.invoice.v1", lambda r: r["missing_key"])
    with pytest.raises(ErpTransformError) as exc:
        t.transform("t", "sap", "sap.invoice.v2", "cavi.invoice.v1", SAP_INVOICE)
    # The original cause is surfaced in the reason, not swallowed.
    assert "failed" in str(exc.value)


def test_decorator_registration_works(registry: SchemaRegistry):
    t = ErpTransformer(clock=lambda: FIXED_TS)

    @t.transform_for("ns.customer.v1", "cavi.customer.v1")
    def _map(record: dict) -> dict:
        return {"id": record["entityId"], "name": record["companyName"]}

    payload = t.transform(
        "t", "netsuite", "ns.customer.v1", "cavi.customer.v1",
        {"entityId": "C-1", "companyName": "ACME"},
    )
    registry.validate("mapper.transform.completed", 1, payload)
    assert payload["output"] == {"id": "C-1", "name": "ACME"}
