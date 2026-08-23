"""Mapper — Cavi -> NetSuite record mappings (the write-back mapping tables).

The reference implementation is a supplement retailer, which sets the priority
order (see docs/PRD-writeback.md, Epic 2): **lot-numbered inventory item**,
**inventory adjustment**, **transfer order**. Each mapping is a declarative
`RecordMapping` applied through the existing `ErpTransformer`, so mapping runs
emit the canonical `mapper.transform.completed` / `mapper.transform.failed`
events like every other transform.

Two governance rules are enforced mechanically, per FR2:

  * **A missing required field blocks the write and surfaces the gap.** All
    gaps are reported at once (`MappingBlocked` lists every one, including
    per-line gaps like ``lines[2].sku``) so a reviewer fixes the record in one
    pass — never a partial ERP record.
  * **No silent drops.** Every Cavi field must resolve to a NetSuite target or
    be *declared* unmapped in the spec. An undeclared field blocks the mapping
    rather than vanishing, so schema drift between Cavi and the mapping table
    is loud.

NetSuite record-type and field names below are the standard REST names; the
exact set (including partner-specific custom fields and chart-of-accounts
targets) is confirmed against the design partner's account at the Epic 2 gate
(PRD §7) — extend `fields` / `required` there, not with ad-hoc code.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Callable, Mapping

from agents.mapper.erp import ErpTransformError, ErpTransformer


class MappingBlocked(ErpTransformError):
    """The record cannot map to a complete ERP payload; carries every gap."""

    def __init__(self, source_schema: str, gaps: list[str]) -> None:
        self.gaps = list(gaps)
        super().__init__(
            f"mapping blocked for {source_schema}: " + "; ".join(self.gaps)
        )


def _minor_to_major(value: object) -> str:
    """Integer minor units -> decimal-string major units (money never floats)."""
    return str(Decimal(int(value)) / 100)


@dataclass(frozen=True)
class LineMapping:
    """Mapping for one entry of a list field (e.g. adjustment/transfer lines).

    ``target`` is the NetSuite list-field the mapped lines land in. Applied per
    element; a gap in any line is reported with its index so the whole batch
    blocks with every problem visible at once.
    """

    target: str                               # netsuite list field, e.g. "inventory"
    fields: Mapping[str, str]                 # cavi line field -> netsuite field
    required: tuple[str, ...] = ()
    converters: Mapping[str, Callable[[object], object]] = field(default_factory=dict)

    def apply(self, lines: object, *, field_name: str) -> tuple[list[dict], list[str]]:
        gaps: list[str] = []
        out: list[dict] = []
        if not isinstance(lines, list) or not lines:
            return [], [f"{field_name} must be a non-empty list"]
        for i, line in enumerate(lines):
            if not isinstance(line, Mapping):
                gaps.append(f"{field_name}[{i}] must be an object")
                continue
            for req in self.required:
                if line.get(req) in (None, ""):
                    gaps.append(f"{field_name}[{i}].{req} is required")
            unknown = set(line) - set(self.fields)
            for name in sorted(unknown):
                gaps.append(f"{field_name}[{i}].{name} has no NetSuite target (undeclared)")
            mapped = {}
            for src, target in self.fields.items():
                if src in line and line[src] is not None:
                    convert = self.converters.get(src, lambda v: v)
                    mapped[target] = convert(line[src])
            out.append(mapped)
        return out, gaps


@dataclass(frozen=True)
class RecordMapping:
    """Declarative Cavi-entity -> NetSuite-record mapping.

    ``fields`` maps every carried Cavi field to its NetSuite target;
    ``unmapped`` declares the Cavi fields deliberately *not* carried (identity
    fields that travel on the write lifecycle instead, internal notes, ...).
    Anything in neither set blocks the mapping — no silent drops.
    """

    source_schema: str          # e.g. "cavi.item.v1"
    target_schema: str          # e.g. "netsuite.lotNumberedInventoryItem.v1"
    target_module: str          # NetSuite record type for the Forge write path
    fields: Mapping[str, str]
    required: tuple[str, ...] = ()
    unmapped: tuple[str, ...] = ()
    converters: Mapping[str, Callable[[object], object]] = field(default_factory=dict)
    lines: Mapping[str, LineMapping] = field(default_factory=dict)

    def apply(self, record: dict) -> dict:
        gaps: list[str] = []
        for req in self.required:
            if record.get(req) in (None, "", []):
                gaps.append(f"{req} is required")
        declared = set(self.fields) | set(self.unmapped) | set(self.lines)
        for name in sorted(set(record) - declared):
            gaps.append(f"{name} has no NetSuite target (undeclared)")

        out: dict = {}
        for src, target in self.fields.items():
            if src in record and record[src] is not None:
                convert = self.converters.get(src, lambda v: v)
                out[target] = convert(record[src])
        for src, line_mapping in self.lines.items():
            if src not in record:
                continue  # absence is handled by `required` above
            mapped, line_gaps = line_mapping.apply(record[src], field_name=src)
            gaps.extend(line_gaps)
            out[line_mapping.target] = mapped

        if gaps:
            raise MappingBlocked(self.source_schema, gaps)
        return out


# --------------------------------------------------------------------------- #
# The three reference mappings (priority order from the design partnership)
# --------------------------------------------------------------------------- #
ITEM = RecordMapping(
    source_schema="cavi.item.v1",
    target_schema="netsuite.lotNumberedInventoryItem.v1",
    target_module="lotNumberedInventoryItem",
    required=("sku", "name"),
    fields={
        "sku": "itemId",
        "name": "displayName",
        "upc": "upcCode",
        "shelf_life_days": "shelfLife",
        "unit_price_minor": "basePrice",
    },
    converters={"unit_price_minor": _minor_to_major},
    # Internal fields that never leave Cavi: notes are operator-facing, and the
    # record's identity travels as the write lifecycle's idempotency key.
    unmapped=("item_id", "notes"),
)

ADJUSTMENT_LINES = LineMapping(
    target="inventory",
    fields={
        "sku": "item",
        "quantity_delta": "adjustQtyBy",
        "lot_number": "receiptInventoryNumber",
        "expiry_date": "expirationDate",
    },
    required=("sku", "quantity_delta"),
)

ADJUSTMENT = RecordMapping(
    source_schema="cavi.inventory_adjustment.v1",
    target_schema="netsuite.inventoryAdjustment.v1",
    target_module="inventoryAdjustment",
    required=("account", "location", "lines"),
    fields={
        "account": "account",
        "location": "adjLocation",
        "memo": "memo",
    },
    lines={"lines": ADJUSTMENT_LINES},
    unmapped=("adjustment_id", "counted_by"),
)

TRANSFER_LINES = LineMapping(
    target="item",
    fields={
        "sku": "item",
        "quantity": "quantity",
        "lot_number": "issueInventoryNumber",
    },
    required=("sku", "quantity"),
)

TRANSFER = RecordMapping(
    source_schema="cavi.transfer.v1",
    target_schema="netsuite.transferOrder.v1",
    target_module="transferOrder",
    required=("location_from", "location_to", "lines"),
    fields={
        "location_from": "location",
        "location_to": "transferLocation",
        "expected_ship_date": "shipDate",
        "memo": "memo",
    },
    lines={"lines": TRANSFER_LINES},
    unmapped=("transfer_id",),
)

#: All reference mappings, keyed by source schema — the write path resolves
#: `target_module` for a proposed record from here.
NETSUITE_RECORD_MAPPINGS: dict[str, RecordMapping] = {
    m.source_schema: m for m in (ITEM, ADJUSTMENT, TRANSFER)
}


def register_netsuite_mappings(transformer: ErpTransformer) -> ErpTransformer:
    """Register every reference mapping on an ErpTransformer, so mapping runs
    flow through the canonical mapper.transform.completed/failed events."""
    for mapping in NETSUITE_RECORD_MAPPINGS.values():
        transformer.register(mapping.source_schema, mapping.target_schema, mapping.apply)
    return transformer
