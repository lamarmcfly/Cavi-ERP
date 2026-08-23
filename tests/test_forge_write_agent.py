"""Tests for the Forge write agent runtime — replay safety, tenant-scoped
breakers, and escalation routing (Codex review follow-ups).

Uses the fakeredis + in-memory store harness (like test_base_agent.py) and
drives `handle()` directly. Proves:

  * replaying an approval for an already-APPROVED write retries execution
    instead of raising `InvalidTransition` (which would kill the agent loop);
  * an illegal decision (e.g. rejecting an approved write) is dead-lettered,
    not crashed on;
  * circuit breakers are scoped per (tenant, ERP) — one tenant's outage never
    blocks a healthy tenant;
  * every dead-letter subject the write path emits is actually in Beacon's
    subscriptions, so "escalates to a human" is wired, not aspirational.
"""
from __future__ import annotations

import fakeredis

from agents.base.contract import Event
from agents.base.registry import SchemaRegistry
from agents.forge.breaker import CircuitBreaker
from agents.forge.write import ErpWriteError, WriteCoordinator, WriteState
from agents.forge.write_agent import ForgeWriteAgent
from shared.events import InMemoryEventStore

FIXED_TS = "2026-07-04T12:00:00+00:00"

PROPOSE = {
    "tenant_id": "tenant-acme",
    "erp_platform": "netsuite",
    "operation": "create",
    "target_module": "journalEntry",
    "payload": {"memo": "posting"},
    "requested_by": "agent:forge",
    "diff_preview": "+ journalEntry",
}


class FlakyWriter:
    """Fails until told to succeed; records tenants of every apply."""

    def __init__(self) -> None:
        self.failing = True
        self.applied: list[str] = []

    def apply(self, op) -> dict:
        if self.failing:
            raise ErpWriteError("ERP down")
        self.applied.append(op.tenant_id)
        return {"id": "NS-1"}


def _agent(writer, *, threshold: int = 2):
    ids = iter(f"w{i}" for i in range(100))
    store = InMemoryEventStore()
    agent = ForgeWriteAgent(
        coordinator=WriteCoordinator(
            writer=writer, clock=lambda: FIXED_TS, id_factory=lambda: next(ids)
        ),
        breaker_factory=lambda: CircuitBreaker(
            failure_threshold=threshold, cooldown_seconds=300.0, clock=lambda: 0.0
        ),
        bus=fakeredis.FakeStrictRedis(decode_responses=True),
        event_store=store,
        registry=SchemaRegistry(),
    )
    return agent, store


def _evt(subject: str, payload: dict) -> Event:
    return Event(subject=subject, schema_version=1, source="test", payload=payload)


def _propose(agent, tenant: str = "tenant-acme") -> str:
    before = set(agent._pending)
    agent.handle(_evt("forge.write.propose", {**PROPOSE, "tenant_id": tenant}))
    [write_id] = set(agent._pending) - before      # the newly minted write
    return write_id


def _decide(agent, write_id: str, decision: str = "approve") -> None:
    agent.handle(
        _evt(
            "forge.write.decision",
            {"write_id": write_id, "decision": decision, "reviewer": "user:owner"},
        )
    )


# --------------------------------------------------------------------------- #
# Replay safety
# --------------------------------------------------------------------------- #
def test_replayed_approval_retries_execution_instead_of_crashing():
    writer = FlakyWriter()
    agent, store = _agent(writer, threshold=5)
    write_id = _propose(agent)

    # First approval: approve succeeds, execute fails, write stays APPROVED.
    _decide(agent, write_id)
    assert agent._pending[write_id].state is WriteState.APPROVED

    # ERP recovers; the SAME decision event is replayed (dead-letter replay).
    # This must not raise InvalidTransition — it retries the execution.
    writer.failing = False
    _decide(agent, write_id)

    assert writer.applied == ["tenant-acme"]
    assert write_id not in agent._pending          # completed and cleared
    assert any(e.subject == "forge.write.completed" for e in store.events)
    # approved was emitted exactly once — the replay did not re-approve.
    approvals = [e for e in store.events if e.subject == "forge.write.approved"]
    assert len(approvals) == 1


def test_illegal_decision_is_dead_lettered_not_crashed_on():
    agent, store = _agent(FlakyWriter(), threshold=5)
    write_id = _propose(agent)
    _decide(agent, write_id)                       # approve (execute fails)

    _decide(agent, write_id, decision="reject")    # reject-after-approve: illegal
    assert store.deadletters, "illegal decision should be quarantined"
    assert "illegal decision" in store.deadletters[-1]["error"]
    # The write survives, still approved and retryable.
    assert agent._pending[write_id].state is WriteState.APPROVED


# --------------------------------------------------------------------------- #
# Tenant-scoped breakers
# --------------------------------------------------------------------------- #
def test_one_tenants_outage_does_not_block_a_healthy_tenant():
    writer = FlakyWriter()
    agent, store = _agent(writer, threshold=2)

    # Tenant A fails to the threshold: its breaker opens.
    a_id = _propose(agent, tenant="tenant-a")
    _decide(agent, a_id)                           # failure 1
    _decide(agent, a_id)                           # failure 2 -> breaker opens
    assert any("circuit breaker opened" in d["error"] for d in store.deadletters)

    # Tenant B's writes still execute — its breaker is separate and closed.
    writer.failing = False
    b_id = _propose(agent, tenant="tenant-b")
    _decide(agent, b_id)
    assert writer.applied == ["tenant-b"]

    # Tenant A remains refused up front while its breaker cools down.
    _decide(agent, a_id)
    assert "tenant-a" not in writer.applied


# --------------------------------------------------------------------------- #
# Escalation routing — the write path's dead letters reach Beacon
# --------------------------------------------------------------------------- #
def test_every_write_path_dead_letter_subject_is_beacon_subscribed():
    from agents.beacon.agent import BeaconAgent
    from agents.beacon.beacon import Severity, severity_for

    beacon = BeaconAgent()   # construction is lazy: no redis/postgres I/O
    for subject in (
        "deadletter.forge.write.propose",
        "deadletter.forge.write.decision",   # includes breaker-open escalation
        "deadletter.forge.write.reverse",
        "deadletter.ticker.reconciliation.due",
    ):
        assert subject in beacon.subjects, f"Beacon not subscribed to {subject}"
        assert severity_for(subject) >= Severity.ERROR
