"""Built-in schema transforms registered with Mapper.

Each transform is a pure function payload(v_from) -> payload(v_to). Keep them
total and side-effect free so a coercion is deterministic and replayable.
"""
from __future__ import annotations

from agents.mapper.mapper import Mapper


def ledger_entry_v1_to_v2(payload: dict) -> dict:
    """ledger.entry v1 -> v2.

    v2 renames `memo` -> `description` and adds a required `entry_type`. The old
    `memo` key must be dropped because v2 is `additionalProperties: false`.
    """
    out = {k: v for k, v in payload.items() if k != "memo"}
    out["entry_type"] = "sale"  # default classification for legacy entries
    memo = payload.get("memo")
    if memo is not None:
        out["description"] = memo
    return out


def forge_write_requested_v1_to_v2(payload: dict) -> dict:
    """forge.write.requested v1 -> v2.

    v2 adds a required (nullable) `reverses` — the write_id of the completed
    write a compensating proposal reverses. A v1 event predates reversals, so
    it is by definition an ordinary write: reverses is null.
    """
    return {**payload, "reverses": None}


def register_all(mapper: Mapper) -> Mapper:
    """Register every built-in transform on a Mapper instance."""
    mapper.register("ledger.entry", 1, 2, ledger_entry_v1_to_v2)
    mapper.register("forge.write.requested", 1, 2, forge_write_requested_v1_to_v2)
    return mapper
