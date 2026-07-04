"""Forge — ERP write lifecycle (domain logic).

A mutating ERP write is never fire-and-forget: Forge **proposes** it, a reviewer
**approves or rejects** it, and only an approved write is **executed** against
the ERP. Each stage produces one canonical event:

    forge.write.requested  -> proposal recorded, awaiting review
    forge.write.approved   -> cleared to execute
    forge.write.rejected   -> refused, will not execute
    forge.write.completed  -> applied, carries the ERP's confirmation

Two invariants live here, enforced by a small state machine:
  * **Approval gate** — a write can only be executed from the APPROVED state, so
    an unreviewed (or rejected) write can never reach the ERP.
  * **Decide once** — REJECTED and COMPLETED are terminal; you can't approve a
    rejected write or complete one twice. Illegal moves raise `InvalidTransition`.

The actual ERP call is an injected `ErpWriter`, so this layer is fully testable
with no live ERP. A production writer would sign the request with a Vault-vended
token and call the ERP's REST API, returning its confirmation record.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from enum import Enum
from typing import Callable, Mapping, Protocol

from agents.forge.forge import ForgeError, InvalidTransition


class WriteState(str, Enum):
    REQUESTED = "requested"
    APPROVED = "approved"
    REJECTED = "rejected"
    COMPLETED = "completed"


# Allowed forward transitions; REJECTED and COMPLETED are terminal.
_ALLOWED: dict[WriteState, set[WriteState]] = {
    WriteState.REQUESTED: {WriteState.APPROVED, WriteState.REJECTED},
    WriteState.APPROVED: {WriteState.COMPLETED},
    WriteState.REJECTED: set(),
    WriteState.COMPLETED: set(),
}


class ErpWriteError(ForgeError):
    """Raised when the ERP rejects or fails to apply an approved write."""


@dataclass(frozen=True)
class WriteOperation:
    """An in-flight mutating ERP write and its lifecycle state.

    `write_id` is the correlation id shared across the requested/approved/
    completed events for one write.
    """

    write_id: str
    tenant_id: str
    erp_platform: str
    operation: str          # create | update | void | ...
    target_module: str      # ERP module/entity, e.g. "SalesOrder"
    payload: dict           # intended write body (free-form)
    requested_by: str
    diff_preview: str
    state: WriteState = WriteState.REQUESTED

    def transition_to(self, new_state: WriteState) -> "WriteOperation":
        if new_state not in _ALLOWED[self.state]:
            raise InvalidTransition(
                f"write {self.write_id}: {self.state.value} -> {new_state.value}"
            )
        return replace(self, state=new_state)


class ErpWriter(Protocol):
    """How an approved write is applied to the ERP. Injected so the lifecycle is
    testable without a live ERP. Returns the ERP's confirmation record (ids,
    revision, receipt), which rides on `forge.write.completed`."""

    def apply(self, op: "WriteOperation") -> dict: ...


class UnconfiguredErpWriter:
    """Default writer: refuses to execute until a real ERP writer is injected, so
    a misconfigured deployment fails loudly instead of silently dropping writes."""

    def apply(self, op: "WriteOperation") -> dict:
        raise ErpWriteError(
            f"no ERP writer configured for {op.erp_platform!r}; inject an ErpWriter"
        )


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _new_write_id() -> str:
    return str(uuid.uuid4())


# --------------------------------------------------------------------------- #
# Canonical payload builders (pure)
# --------------------------------------------------------------------------- #
def requested_payload(op: WriteOperation, *, requested_at: str) -> dict:
    return {
        "tenant_id": op.tenant_id,
        "erp_platform": op.erp_platform,
        "operation": op.operation,
        "target_module": op.target_module,
        "payload": dict(op.payload),
        "requested_by": op.requested_by,
        "requested_at": requested_at,
        "diff_preview": op.diff_preview,
    }


def approved_payload(op: WriteOperation, approved_by: str, *, approved_at: str) -> dict:
    return {
        "tenant_id": op.tenant_id,
        "erp_platform": op.erp_platform,
        "operation": op.operation,
        "approved_by": approved_by,
        "approved_at": approved_at,
        "write_id": op.write_id,
    }


def rejected_payload(
    op: WriteOperation, rejected_by: str, reason: str, *, rejected_at: str
) -> dict:
    return {
        "tenant_id": op.tenant_id,
        "erp_platform": op.erp_platform,
        "operation": op.operation,
        "rejected_by": rejected_by,
        "rejected_at": rejected_at,
        "reason": reason,
    }


def completed_payload(
    op: WriteOperation, erp_confirmation: Mapping, *, completed_at: str
) -> dict:
    return {
        "tenant_id": op.tenant_id,
        "erp_platform": op.erp_platform,
        "operation": op.operation,
        "write_id": op.write_id,
        "erp_confirmation": dict(erp_confirmation),
        "completed_at": completed_at,
    }


@dataclass(frozen=True)
class WriteStep:
    """The result of one lifecycle step: the new operation state plus the
    canonical event (subject + payload) to publish for it."""

    op: WriteOperation
    subject: str
    event: dict


class WriteCoordinator:
    """Drives a write through request -> approve/reject -> execute, producing one
    canonical `WriteStep` per stage. `clock` and `id_factory` are injectable so
    events are deterministic under test."""

    def __init__(
        self,
        writer: ErpWriter | None = None,
        *,
        clock: Callable[[], str] = _now_iso,
        id_factory: Callable[[], str] = _new_write_id,
    ) -> None:
        self._writer = writer or UnconfiguredErpWriter()
        self._clock = clock
        self._id = id_factory

    def request(
        self,
        *,
        tenant_id: str,
        erp_platform: str,
        operation: str,
        target_module: str,
        payload: Mapping,
        requested_by: str,
        diff_preview: str,
    ) -> WriteStep:
        op = WriteOperation(
            write_id=self._id(),
            tenant_id=tenant_id,
            erp_platform=erp_platform,
            operation=operation,
            target_module=target_module,
            payload=dict(payload),
            requested_by=requested_by,
            diff_preview=diff_preview,
        )
        return WriteStep(
            op, "forge.write.requested", requested_payload(op, requested_at=self._clock())
        )

    def approve(self, op: WriteOperation, approved_by: str) -> WriteStep:
        approved = op.transition_to(WriteState.APPROVED)
        return WriteStep(
            approved,
            "forge.write.approved",
            approved_payload(approved, approved_by, approved_at=self._clock()),
        )

    def reject(self, op: WriteOperation, rejected_by: str, reason: str) -> WriteStep:
        rejected = op.transition_to(WriteState.REJECTED)
        return WriteStep(
            rejected,
            "forge.write.rejected",
            rejected_payload(rejected, rejected_by, reason, rejected_at=self._clock()),
        )

    def execute(self, op: WriteOperation) -> WriteStep:
        """Apply an approved write to the ERP and produce the completed event.

        Guards on APPROVED *before* touching the ERP, so a non-approved write
        never triggers a side effect. `ErpWriteError` from the writer propagates
        and the write stays APPROVED (retryable), never COMPLETED.
        """
        if op.state is not WriteState.APPROVED:
            raise InvalidTransition(
                f"write {op.write_id}: cannot execute from {op.state.value}"
            )
        confirmation = self._writer.apply(op)
        completed = op.transition_to(WriteState.COMPLETED)
        return WriteStep(
            completed,
            "forge.write.completed",
            completed_payload(completed, confirmation, completed_at=self._clock()),
        )
