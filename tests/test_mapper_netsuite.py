"""Tests for the Cavi -> NetSuite record mappings (Epic 2 / FR2).

Pure domain + contract validation, no infrastructure. Drives the three
reference mappings (lot-numbered item, inventory adjustment, transfer order)
through the ErpTransformer and proves the two Epic 2 acceptance rules:

  * a missing required field blocks the mapping and surfaces EVERY gap at
    once (including per-line gaps), never producing a partial ERP record;
  * every Cavi field resolves to a NetSuite target or an explicitly declared
    unmapped state — an undeclared field blocks rather than silently drops.
"""
from __future__ import annotations

import pytest

from agents.base.registry import SchemaRegistry
from agents.mapper.erp import ErpTransformError, ErpTransformer
from agents.mapper.netsuite_records import (
    ADJUSTMENT,
    ITEM,
    NETSUITE_RECORD_MAPPINGS,
    TRANSFER,
    MappingBlocked,
    register_netsuite_mappings,
)

FIXED_TS = "2026-07-04T12:00:00+00:00"

ITEM_RECORD = {
    "sku": "VITC-500",
    "name": "Vitamin C 500mg",
    "upc": "012345678905",
    "shelf_life_days": 720,
    "unit_price_minor": 1_499,
    "notes": "internal only",          # declared unmapped — never leaves Cavi
}

ADJUSTMENT_RECORD = {
    "adjustment_id": "adj-77",          # declared unmapped (identity rides the write path)
    "account": "5100-inventory-adjustment",
    "location": "BR-2",
    "memo": "cycle count",
    "lines": [
        {"sku": "VITC-500", "quantity_delta": -3, "lot_number": "LOT-9", "expiry_date": "2027-01-31"},
        {"sku": "ZINC-50", "quantity_delta": 12},
    ],
}

TRANSFER_RECORD = {
    "transfer_id": "tr-12",
    "location_from": "BR-1",
    "location_to": "BR-2",
    "expected_ship_date": "2026-07-10",
    "lines": [{"sku": "VITC-500", "quantity": 24, "lot_number": "LOT-9"}],
}


def _transformer() -> ErpTransformer:
    return register_netsuite_mappings(ErpTransformer(clock=lambda: FIXED_TS))


# --------------------------------------------------------------------------- #
# Happy paths — each record type maps completely and emits the canonical event
# --------------------------------------------------------------------------- #
def test_item_maps_to_lot_numbered_inventory_item():
    out = ITEM.apply(dict(ITEM_RECORD))
    assert out == {
        "itemId": "VITC-500",
        "displayName": "Vitamin C 500mg",
        "upcCode": "012345678905",
        "shelfLife": 720,
        "basePrice": "14.99",   # minor units -> decimal-string major units
    }
    assert "notes" not in str(out)   # declared-unmapped field never carried


def test_adjustment_maps_lines_including_lot_and_expiry():
    out = ADJUSTMENT.apply(dict(ADJUSTMENT_RECORD))
    assert out["account"] == "5100-inventory-adjustment"
    assert out["adjLocation"] == "BR-2"
    assert out["inventory"] == [
        {
            "item": "VITC-500", "adjustQtyBy": -3,
            "receiptInventoryNumber": "LOT-9", "expirationDate": "2027-01-31",
        },
        {"item": "ZINC-50", "adjustQtyBy": 12},
    ]


def test_transfer_maps_locations_and_lot_lines():
    out = TRANSFER.apply(dict(TRANSFER_RECORD))
    assert out["location"] == "BR-1"
    assert out["transferLocation"] == "BR-2"
    assert out["shipDate"] == "2026-07-10"
    assert out["item"] == [
        {"item": "VITC-500", "quantity": 24, "issueInventoryNumber": "LOT-9"}
    ]


def test_mappings_flow_through_canonical_transform_events():
    registry = SchemaRegistry()
    payload = _transformer().transform(
        "tenant-acme", "cavi", "cavi.item.v1",
        "netsuite.lotNumberedInventoryItem.v1", ITEM_RECORD,
    )
    registry.validate("mapper.transform.completed", 1, payload)
    assert payload["output"]["itemId"] == "VITC-500"


def test_every_mapping_declares_a_write_path_module():
    assert {m.target_module for m in NETSUITE_RECORD_MAPPINGS.values()} == {
        "lotNumberedInventoryItem", "inventoryAdjustment", "transferOrder",
    }


# --------------------------------------------------------------------------- #
# Blocking — gaps surface all at once; nothing partial gets through
# --------------------------------------------------------------------------- #
def test_missing_required_fields_block_and_all_gaps_surface():
    record = dict(ITEM_RECORD)
    del record["sku"], record["name"]
    with pytest.raises(MappingBlocked) as exc:
        ITEM.apply(record)
    assert exc.value.gaps == ["sku is required", "name is required"]


def test_undeclared_field_blocks_instead_of_silently_dropping():
    record = {**ITEM_RECORD, "surprise_field": "x"}
    with pytest.raises(MappingBlocked, match="surprise_field has no NetSuite target"):
        ITEM.apply(record)


def test_line_level_gaps_surface_with_their_index():
    record = dict(ADJUSTMENT_RECORD)
    record["lines"] = [
        {"sku": "VITC-500"},                               # missing quantity_delta
        {"quantity_delta": 5, "mystery": 1},               # missing sku + undeclared
    ]
    with pytest.raises(MappingBlocked) as exc:
        ADJUSTMENT.apply(record)
    assert "lines[0].quantity_delta is required" in exc.value.gaps
    assert "lines[1].sku is required" in exc.value.gaps
    assert "lines[1].mystery has no NetSuite target (undeclared)" in exc.value.gaps


def test_non_integer_money_blocks_instead_of_coercing():
    # Money is integer minor units, full stop — a float (or "14.99" string)
    # upstream is a bug to surface, not a value to round.
    with pytest.raises(ErpTransformError, match="integer minor units"):
        ITEM.apply({**ITEM_RECORD, "unit_price_minor": 14.99})


def test_empty_lines_block():
    record = {**TRANSFER_RECORD, "lines": []}
    with pytest.raises(MappingBlocked, match="lines is required"):
        TRANSFER.apply(record)


def test_blocked_mapping_becomes_a_canonical_failure_event():
    registry = SchemaRegistry()
    t = _transformer()
    with pytest.raises(ErpTransformError):
        t.transform(
            "tenant-acme", "cavi", "cavi.item.v1",
            "netsuite.lotNumberedInventoryItem.v1", {"upc": "1"},
        )
    failed = t.failure("tenant-acme", "cavi", "cavi.item.v1", "mapping blocked")
    registry.validate("mapper.transform.failed", 1, failed)
