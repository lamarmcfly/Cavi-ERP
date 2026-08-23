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


class DedupingErpWriter:
    """Simulates an ERP that honors an idempotency / external-id key.

    Records at most one commit per key. The first apply for a key "commits"
    server-side but can be told to lose its response (raise), modelling a
    timeout where the ERP actually persisted. A retry with the same key finds
    the existing record and returns it without committing again — so
    ``commits`` never exceeds the number of distinct keys.
    """

    def __init__(self, *, lose_first_response: bool = False) -> None:
        self.records: dict[str, dict] = {}
        self.commits = 0
        self._lose_first = lose_first_response

    def apply(self, op) -> dict:
        key = op.idempotency_key
        if key not in self.records:
            # First sighting of this key: the ERP commits exactly one record.
            self.commits += 1
            self.records[key] = {"id": f"NS-{self.commits}", "external_id": key}
            if self._lose_first:
                # Committed, but the response is lost in transit.
                self._lose_first = False
                raise ErpWriteError("timeout after commit (response lost)")
        return dict(self.records[key])


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


# --------------------------------------------------------------------------- #
# Idempotency (FR5) — a retry must never double-post
# --------------------------------------------------------------------------- #
def test_idempotency_key_is_stable_and_namespaced():
    op = _coord().request(**REQUEST).op
    assert op.idempotency_key == f"cavi-{FIXED_ID}"
    # Stable across a state transition — the key rides the whole lifecycle.
    assert op.transition_to(WriteState.APPROVED).idempotency_key == op.idempotency_key


def test_retry_after_lost_response_does_not_double_post():
    # The ERP commits on the first call but the response is lost, so execute()
    # raises and the write stays APPROVED (retryable).
    writer = DedupingErpWriter(lose_first_response=True)
    coord = _coord(writer)
    appr = coord.approve(coord.request(**REQUEST).op, "user:owner")

    with pytest.raises(ErpWriteError):
        coord.execute(appr.op)
    assert appr.op.state is WriteState.APPROVED   # unchanged, ready to retry

    # Retry with the *same* op → same idempotency key → the ERP returns the
    # already-committed record instead of posting a second one.
    comp = coord.execute(appr.op)
    assert comp.op.state is WriteState.COMPLETED
    assert writer.commits == 1                     # exactly one server-side commit
    assert comp.event["erp_confirmation"]["external_id"] == f"cavi-{FIXED_ID}"


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
