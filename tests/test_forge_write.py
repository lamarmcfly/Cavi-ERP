"""Tests for the Forge ERP write lifecycle.

Pure domain logic + contract validation, no infrastructure. Drives the
`WriteCoordinator` through request -> approve/reject -> execute with an injected
stub ERP writer and a fixed clock/id, and validates every produced event against
its canonical `forge.write.*` contract via the SchemaRegistry.

Part of issue #2 (canonical emitters): #1 landed the contracts; this adds the
Forge capability that emits them.
"""
from __future__ import annotations

import pytest

from agents.base.registry import SchemaRegistry
from agents.forge.forge import InvalidTransition
from agents.forge.write import (
    ErpWriteError,
    UnconfiguredErpWriter,
    WriteCoordinator,
    WriteState,
)

FIXED_TS = "2026-07-04T12:00:00+00:00"
FIXED_ID = "w_test_1"

REQUEST = dict(
    tenant_id="tenant-acme",
    erp_platform="netsuite",
    operation="create",
    target_module="SalesOrder",
    payload={"customer": "ACME", "total_minor": 10_000},
    requested_by="agent:forge",
    diff_preview="+ SalesOrder ACME $100.00",
)


class StubErpWriter:
    """Records apply() calls and returns a canned confirmation."""

    def __init__(self, confirmation: dict | None = None) -> None:
        self.calls: list = []
        self._conf = confirmation or {"id": "SO-123", "revision": 1}

    def apply(self, op) -> dict:
        self.calls.append(op)
        return dict(self._conf)


class ExplodingErpWriter:
    def apply(self, op) -> dict:
        raise ErpWriteError("ERP rejected the write")


@pytest.fixture
def registry() -> SchemaRegistry:
    return SchemaRegistry()


def _coord(writer=None) -> WriteCoordinator:
    return WriteCoordinator(
        writer=writer or StubErpWriter(),
        clock=lambda: FIXED_TS,
        id_factory=lambda: FIXED_ID,
    )


# --------------------------------------------------------------------------- #
# Happy path — each stage emits a contract-valid canonical event
# --------------------------------------------------------------------------- #
def test_full_lifecycle_emits_all_canonical_events(registry: SchemaRegistry):
    writer = StubErpWriter()
    coord = _coord(writer)

    req = coord.request(**REQUEST)
    appr = coord.approve(req.op, "user:owner")
    comp = coord.execute(appr.op)

    # Each event validates against its finalized contract (also enforces
    # additionalProperties:false).
    registry.validate("forge.write.requested", 1, req.event)
    registry.validate("forge.write.approved", 1, appr.event)
    registry.validate("forge.write.completed", 1, comp.event)

    # write_id is the correlation id shared across the whole lifecycle. The
    # requested event intentionally carries none (the id is minted with it);
    # approved + completed both carry the same write_id.
    assert req.op.write_id == appr.op.write_id == comp.op.write_id == FIXED_ID
    assert "write_id" not in req.event
    assert appr.event["write_id"] == comp.event["write_id"] == FIXED_ID

    # The ERP was called exactly once, with the approved op, and its confirmation
    # rides on the completed event.
    assert writer.calls == [appr.op]
    assert comp.event["erp_confirmation"] == {"id": "SO-123", "revision": 1}
    assert comp.op.state is WriteState.COMPLETED


def test_requested_event_carries_the_full_proposal(registry: SchemaRegistry):
    req = _coord().request(**REQUEST)
    registry.validate("forge.write.requested", 1, req.event)
    assert set(req.event) == {
        "tenant_id", "erp_platform", "operation", "target_module",
        "payload", "requested_by", "requested_at", "diff_preview",
    }
    assert req.event["payload"] == {"customer": "ACME", "total_minor": 10_000}
    assert req.event["diff_preview"] == "+ SalesOrder ACME $100.00"
    assert req.event["requested_at"] == FIXED_TS


def test_reject_emits_canonical_rejected(registry: SchemaRegistry):
    coord = _coord()
    rej = coord.reject(coord.request(**REQUEST).op, "user:owner", "duplicate order")
    registry.validate("forge.write.rejected", 1, rej.event)
    assert rej.event["reason"] == "duplicate order"
    assert rej.op.state is WriteState.REJECTED


# --------------------------------------------------------------------------- #
# State-machine guards
# --------------------------------------------------------------------------- #
def test_cannot_execute_an_unapproved_write_and_no_erp_side_effect():
    writer = StubErpWriter()
    coord = _coord(writer)
    req = coord.request(**REQUEST)
    with pytest.raises(InvalidTransition):
        coord.execute(req.op)          # still REQUESTED, not APPROVED
    assert writer.calls == []          # the approval gate blocked the ERP call


def test_cannot_approve_a_rejected_write():
    coord = _coord()
    rej = coord.reject(coord.request(**REQUEST).op, "user:owner", "no")
    with pytest.raises(InvalidTransition):
        coord.approve(rej.op, "user:owner")   # REJECTED is terminal


def test_cannot_execute_twice():
    coord = _coord()
    comp = coord.execute(coord.approve(coord.request(**REQUEST).op, "u").op)
    with pytest.raises(InvalidTransition):
        coord.execute(comp.op)                # COMPLETED is terminal


# --------------------------------------------------------------------------- #
# ERP writer failures
# --------------------------------------------------------------------------- #
def test_erp_write_failure_leaves_write_approved_for_retry():
    coord = _coord(ExplodingErpWriter())
    appr = coord.approve(coord.request(**REQUEST).op, "user:owner")
    with pytest.raises(ErpWriteError):
        coord.execute(appr.op)
    # Not advanced to COMPLETED — the approved op is unchanged and retryable.
    assert appr.op.state is WriteState.APPROVED


def test_default_writer_refuses_until_configured():
    coord = WriteCoordinator(clock=lambda: FIXED_TS, id_factory=lambda: FIXED_ID)
    appr = coord.approve(coord.request(**REQUEST).op, "user:owner")
    with pytest.raises(ErpWriteError):
        coord.execute(appr.op)   # default UnconfiguredErpWriter


def test_unconfigured_writer_raises_directly():
    from agents.forge.write import WriteOperation

    op = WriteOperation(
        write_id="w1", tenant_id="t", erp_platform="netsuite", operation="create",
        target_module="SalesOrder", payload={}, requested_by="a", diff_preview="",
        state=WriteState.APPROVED,
    )
    with pytest.raises(ErpWriteError):
        UnconfiguredErpWriter().apply(op)
