"""Tests for Mapper — schema-version coercion and the anti-corruption guarantee.

The single-hop test validates against the real ledger.entry.v2 schema; the
multi-hop/path tests disable output validation (no synthetic v3 schema needed)
to focus on the composition algorithm.
"""
import jsonschema
import pytest

from agents.mapper.mapper import Mapper, NoTransformPath
from agents.mapper.transforms import ledger_entry_v1_to_v2, register_all


def _v1_entry(memo="initial sale"):
    return {
        "entry_id": "00000000-0000-0000-0000-000000000001",
        "currency": "USD",
        "memo": memo,
        "lines": [
            {"account": "1100-ar", "direction": "debit", "amount_minor": 2500},
            {"account": "4000-rev", "direction": "credit", "amount_minor": 2500},
        ],
    }


def test_identity_transform_is_a_validated_passthrough():
    mapper = register_all(Mapper())
    out = mapper.transform("ledger.entry", 1, 1, _v1_entry())
    assert out["memo"] == "initial sale"  # v1 schema still has memo


def test_v1_to_v2_renames_memo_and_adds_entry_type():
    mapper = register_all(Mapper())
    out = mapper.transform("ledger.entry", 1, 2, _v1_entry())
    assert out["entry_type"] == "sale"
    assert out["description"] == "initial sale"
    assert "memo" not in out  # old key dropped — required by additionalProperties:false


def test_output_validation_blocks_a_corrupt_transform():
    # A bad transform that forgets to drop `memo` must be rejected, not emitted.
    mapper = Mapper()
    mapper.register("ledger.entry", 1, 2, lambda p: {**p, "entry_type": "sale"})
    with pytest.raises(jsonschema.ValidationError):
        mapper.transform("ledger.entry", 1, 2, _v1_entry())


def test_multi_hop_path_is_composed():
    # v1->v2->v3 chains automatically; validation off (no v3 schema).
    mapper = Mapper(validate_output=False)
    mapper.register("widget", 1, 2, lambda p: {**p, "step2": True})
    mapper.register("widget", 2, 3, lambda p: {**p, "step3": True})
    out = mapper.transform("widget", 1, 3, {"id": "w"})
    assert out["step2"] and out["step3"]


def test_shortest_path_is_preferred():
    calls = []
    mapper = Mapper(validate_output=False)
    mapper.register("widget", 1, 2, lambda p: (calls.append("1->2"), p)[1])
    mapper.register("widget", 2, 3, lambda p: (calls.append("2->3"), p)[1])
    mapper.register("widget", 1, 3, lambda p: (calls.append("1->3"), p)[1])  # direct
    mapper.transform("widget", 1, 3, {"id": "w"})
    assert calls == ["1->3"]  # single direct hop chosen over the two-hop route


def test_no_path_raises():
    mapper = Mapper(validate_output=False)
    with pytest.raises(NoTransformPath):
        mapper.transform("widget", 1, 9, {"id": "w"})


def test_transform_function_is_pure():
    # The built-in transform must not mutate its input.
    original = _v1_entry()
    snapshot = dict(original)
    ledger_entry_v1_to_v2(original)
    assert original == snapshot
