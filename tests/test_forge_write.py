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
    NO_DRY_RUN_PREVIEW,
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

    # Each event validates against its canonical contract (also enforces
    # additionalProperties:false). requested is v2 (adds `reverses`); the
    # step carries its schema version so the runtime emits the right one.
    assert (req.schema_version, appr.schema_version, comp.schema_version) == (2, 1, 1)
    registry.validate("forge.write.requested", 2, req.event)
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
    registry.validate("forge.write.requested", 2, req.event)
    assert set(req.event) == {
        "tenant_id", "erp_platform", "operation", "target_module",
        "payload", "requested_by", "requested_at", "diff_preview", "reverses",
    }
    assert req.event["payload"] == {"customer": "ACME", "total_minor": 10_000}
    assert req.event["diff_preview"] == "+ SalesOrder ACME $100.00"
    assert req.event["requested_at"] == FIXED_TS
    assert req.event["reverses"] is None   # ordinary write, not a compensation


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
# Dry-run diffs (FR3) — the preview is derived from current ERP state
# --------------------------------------------------------------------------- #
class StubErpReader:
    """Returns a canned current-state record (None = record does not exist)."""

    def __init__(self, record: dict | None = None) -> None:
        self.record = record
        self.calls: list = []

    def fetch(self, op):
        self.calls.append(op)
        return self.record


class ExplodingErpReader:
    def fetch(self, op):
        raise ErpWriteError("ERP unreachable during dry-run")


def _coord_with_reader(reader) -> WriteCoordinator:
    return WriteCoordinator(
        writer=StubErpWriter(),
        reader=reader,
        clock=lambda: FIXED_TS,
        id_factory=lambda: FIXED_ID,
    )


def test_derived_diff_for_a_create(registry: SchemaRegistry):
    reader = StubErpReader(record=None)   # record does not exist yet
    req = _coord_with_reader(reader).request(**REQUEST)

    registry.validate("forge.write.requested", 2, req.event)
    diff = req.event["diff_preview"]
    # Header names exactly what will be touched, keyed by the idempotency key.
    assert diff.splitlines()[0] == (
        f"netsuite SalesOrder eid cavi-{FIXED_ID} — create (no existing record)"
    )
    # Every payload field appears as an addition.
    assert '+ customer: "ACME"' in diff
    assert "+ total_minor: 10000" in diff
    # The reader was consulted for this exact operation.
    assert [op.write_id for op in reader.calls] == [FIXED_ID]


def test_derived_diff_for_an_update_shows_only_changes():
    reader = StubErpReader(
        record={"customer": "ACME", "total_minor": 5_000, "status": "open"}
    )
    req = _coord_with_reader(reader).request(**REQUEST)

    diff = req.event["diff_preview"]
    assert "updates existing record" in diff.splitlines()[0]
    # Changed field: old value out, new value in.
    assert "- total_minor: 5000" in diff
    assert "+ total_minor: 10000" in diff
    # Unchanged field is not noise; untouched ERP-only field is not a removal.
    assert "customer" not in diff.replace(diff.splitlines()[0], "")
    assert "status" not in diff


def test_derived_diff_when_record_already_matches():
    reader = StubErpReader(record={"customer": "ACME", "total_minor": 10_000})
    diff = _coord_with_reader(reader).request(**REQUEST).event["diff_preview"]
    assert "(no field changes" in diff


def test_derived_diff_overrides_a_caller_supplied_preview():
    # The requester's asserted string never reaches the reviewer when a reader
    # is configured — the computed diff wins.
    req = _coord_with_reader(StubErpReader()).request(**REQUEST)
    assert req.event["diff_preview"] != REQUEST["diff_preview"]
    assert req.event["diff_preview"].startswith("netsuite SalesOrder eid ")


def test_dry_run_fetch_failure_fails_closed():
    with pytest.raises(ErpWriteError):
        _coord_with_reader(ExplodingErpReader()).request(**REQUEST)


def test_no_reader_and_no_preview_yields_explicit_placeholder(registry: SchemaRegistry):
    request = {k: v for k, v in REQUEST.items() if k != "diff_preview"}
    req = _coord().request(**request)
    registry.validate("forge.write.requested", 2, req.event)
    assert req.event["diff_preview"] == NO_DRY_RUN_PREVIEW


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


# --------------------------------------------------------------------------- #
# Reversals (FR8) — a compensating write through the same gate
# --------------------------------------------------------------------------- #
def _completed_op(coord: WriteCoordinator):
    return coord.execute(coord.approve(coord.request(**REQUEST).op, "user:owner").op).op


def test_reversal_is_a_new_gated_lifecycle_linked_to_the_original(
    registry: SchemaRegistry,
):
    coord = WriteCoordinator(
        writer=StubErpWriter(), clock=lambda: FIXED_TS,
        id_factory=iter(["w_orig", "w_rev"]).__next__,
    )
    original = _completed_op(coord)

    rev = coord.request_reversal(
        original,
        operation="create",
        payload={"customer": "ACME", "total_minor": -10_000},
        requested_by="user:owner",
        diff_preview="- reverse SalesOrder ACME",
    )
    registry.validate("forge.write.requested", 2, rev.event)
    # A new lifecycle: fresh write_id, linked to the original via `reverses`.
    assert rev.op.write_id == "w_rev" and rev.op.reverses == "w_orig"
    assert rev.event["reverses"] == "w_orig"
    assert rev.op.state is WriteState.REQUESTED

    # The reversal obeys the same approval gate: no approval, no ERP call.
    with pytest.raises(InvalidTransition):
        coord.execute(rev.op)


def test_only_a_completed_write_can_be_reversed():
    coord = _coord()
    req = coord.request(**REQUEST)
    with pytest.raises(InvalidTransition, match="only a completed write"):
        coord.request_reversal(
            req.op, operation="create", payload={}, requested_by="u"
        )
    appr = coord.approve(req.op, "u")
    with pytest.raises(InvalidTransition, match="only a completed write"):
        coord.request_reversal(
            appr.op, operation="create", payload={}, requested_by="u"
        )


def test_v1_requested_events_coerce_to_v2(registry: SchemaRegistry):
    from agents.mapper.transforms import forge_write_requested_v1_to_v2

    v1_event = {k: v for k, v in _coord().request(**REQUEST).event.items()
                if k != "reverses"}
    v2_event = forge_write_requested_v1_to_v2(v1_event)
    registry.validate("forge.write.requested", 2, v2_event)
    assert v2_event["reverses"] is None


# --------------------------------------------------------------------------- #
# Batch execution (FR9, Story 6.1) — a partial batch halts and reports
# --------------------------------------------------------------------------- #
class FailOnErpWriter:
    """Succeeds until the Nth apply (1-based), which raises."""

    def __init__(self, fail_on: int) -> None:
        self.calls = 0
        self._fail_on = fail_on

    def apply(self, op) -> dict:
        self.calls += 1
        if self.calls == self._fail_on:
            raise ErpWriteError("ERP fell over mid-batch")
        return {"id": f"SO-{self.calls}"}


def _approved_batch(coord: WriteCoordinator, n: int):
    return [
        coord.approve(coord.request(**REQUEST).op, "user:owner").op
        for _ in range(n)
    ]


def test_full_batch_executes_and_reports_no_halt():
    ids = iter([f"w{i}" for i in range(3)])
    coord = WriteCoordinator(
        writer=StubErpWriter(), clock=lambda: FIXED_TS, id_factory=ids.__next__
    )
    result = coord.execute_batch(_approved_batch(coord, 3))
    assert not result.halted and not result.partial
    assert [s.op.state for s in result.steps] == [WriteState.COMPLETED] * 3


def test_partial_batch_halts_and_reports_exactly_what_happened():
    ids = iter([f"w{i}" for i in range(3)])
    writer = FailOnErpWriter(fail_on=2)
    coord = WriteCoordinator(
        writer=writer, clock=lambda: FIXED_TS, id_factory=ids.__next__
    )
    batch = _approved_batch(coord, 3)
    result = coord.execute_batch(batch)

    # Halted at write 2: one committed, one failed, one never attempted —
    # nothing after the failure touched the ERP.
    assert result.halted and result.partial
    assert [s.op.write_id for s in result.steps] == ["w0"]
    assert result.failed is batch[1] and "fell over" in result.error
    assert [op.write_id for op in result.not_attempted] == ["w2"]
    assert writer.calls == 2
    # The failed write is still APPROVED — retryable, never falsely completed.
    assert result.failed.state is WriteState.APPROVED


def test_batch_with_any_unapproved_write_is_refused_before_any_erp_call():
    writer = StubErpWriter()
    ids = iter([f"w{i}" for i in range(2)])
    coord = WriteCoordinator(
        writer=writer, clock=lambda: FIXED_TS, id_factory=ids.__next__
    )
    approved = coord.approve(coord.request(**REQUEST).op, "u").op
    unapproved = coord.request(**REQUEST).op
    with pytest.raises(InvalidTransition, match="no write in the batch"):
        coord.execute_batch([approved, unapproved])
    assert writer.calls == []   # the whole batch was refused up front


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
