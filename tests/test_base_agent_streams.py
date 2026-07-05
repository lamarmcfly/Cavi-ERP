"""Tests for the Redis Streams delivery path (A3b — ADR 0003).

The pub/sub bus was at-most-once: a consumer that crashed mid-handle() never saw
the message again. These tests pin the guarantees the Streams migration adds —
ack-on-success, redelivery of unacked work, a poison cap, and once-per-group
(fleet-wide) delivery — using fakeredis, which implements consumer groups.
"""
from __future__ import annotations

import json

import fakeredis

from agents.base.agent import BaseAgent
from agents.base.contract import Event
from agents.base.registry import SchemaRegistry
from shared import metrics
from shared.events import InMemoryEventStore

VALID_DENIED = {
    "tenant_id": "tenant-acme",
    "erp_platform": "sap",
    "reason": "no active grant",
    "requested_at": "2026-07-04T12:00:00Z",
}
SUBJECT = "vault.secret.denied"


class _ProgrammableAgent(BaseAgent):
    """A test agent whose handle() fails a configurable number of times.

    `fail_times=N` raises on the first N deliveries then succeeds — used to drive
    the redelivery and poison paths deterministically. `fail_times=None` always
    raises (permanent poison).
    """

    name = "test"

    def __init__(self, *, fail_times: int | None = 0, **kwargs) -> None:
        super().__init__(**kwargs)
        self.handled: list[Event] = []
        self.attempts = 0
        self._fail_times = fail_times
        # Fast, deterministic reclaim in tests: no idle backoff.
        self._reclaim_idle_ms = 0
        self._block_ms = 50

    @property
    def subjects(self) -> list[str]:
        return [SUBJECT]

    def handle(self, event: Event) -> None:
        self.attempts += 1
        if self._fail_times is None or self.attempts <= self._fail_times:
            raise RuntimeError(f"boom (attempt {self.attempts})")
        self.handled.append(event)


def _agent(**kwargs) -> _ProgrammableAgent:
    return _ProgrammableAgent(
        bus=fakeredis.FakeStrictRedis(decode_responses=True),
        event_store=InMemoryEventStore(),
        registry=SchemaRegistry(),
        **kwargs,
    )


def _pending(agent: _ProgrammableAgent) -> list:
    return agent.bus.xpending_range(SUBJECT, agent._group, min="-", max="+", count=100)


def _event() -> Event:
    return Event(subject=SUBJECT, schema_version=1, source="test", payload=VALID_DENIED)


# --------------------------------------------------------------------------- #
# Emit puts events on the durable stream (as well as pub/sub)
# --------------------------------------------------------------------------- #
def test_emit_xadds_to_the_subject_stream():
    agent = _agent()
    agent.emit(_event())
    entries = agent.bus.xrange(SUBJECT)
    assert len(entries) == 1
    _id, fields = entries[0]
    assert json.loads(fields["data"])["subject"] == SUBJECT


# --------------------------------------------------------------------------- #
# Happy path — consume, handle, ack
# --------------------------------------------------------------------------- #
def test_consume_acks_a_successfully_handled_message():
    agent = _agent()  # fail_times=0 -> succeeds immediately
    agent._ensure_groups()
    agent.emit(_event())
    agent._consume_once()
    assert len(agent.handled) == 1
    # Acked -> nothing left pending for redelivery.
    assert _pending(agent) == []


def test_ensure_groups_is_idempotent():
    agent = _agent()
    agent._ensure_groups()
    agent._ensure_groups()  # BUSYGROUP swallowed, not raised
    agent.emit(_event())
    agent._consume_once()
    assert len(agent.handled) == 1


# --------------------------------------------------------------------------- #
# At-least-once — a failed handle() is redelivered, not lost
# --------------------------------------------------------------------------- #
def test_failed_handle_is_left_pending_then_redelivered():
    agent = _agent(fail_times=1)  # fail once, then succeed
    agent._ensure_groups()
    agent.emit(_event())

    agent._consume_once()               # attempt 1 raises -> not acked
    assert agent.handled == []
    assert len(_pending(agent)) == 1    # still owed to a consumer

    agent._reclaim_pending()            # reclaim + retry -> attempt 2 succeeds
    assert len(agent.handled) == 1
    assert _pending(agent) == []        # now acked


def test_a_new_consumer_reclaims_a_dead_consumers_pending_message():
    # Simulates a crash: consumer A reads (and dies before ack); consumer B, a
    # sibling in the same group on the same Redis, reclaims and finishes the work.
    redis = fakeredis.FakeStrictRedis(decode_responses=True)
    a = _agent()
    a.bus = redis
    a._fail_times = 1  # A's single read "fails" (stands in for the crash)
    a._ensure_groups()
    a.emit(_event())
    a._consume_once()
    assert len(_pending(a)) == 1

    b = _ProgrammableAgent(
        bus=redis, event_store=InMemoryEventStore(), registry=SchemaRegistry(), fail_times=0
    )
    b._reclaim_idle_ms = 0
    b._reclaim_pending()                # B steals the idle message and handles it
    assert len(b.handled) == 1
    assert b.bus.xpending_range(SUBJECT, b._group, min="-", max="+", count=100) == []


# --------------------------------------------------------------------------- #
# Poison cap — a permanently failing message is dead-lettered, not retried forever
# --------------------------------------------------------------------------- #
def test_poison_message_is_dead_lettered_after_max_deliveries():
    agent = _agent(fail_times=None)     # always raises
    agent._max_deliveries = 3
    store: InMemoryEventStore = agent.event_store  # type: ignore[assignment]
    agent._ensure_groups()
    agent.emit(_event())

    agent._consume_once()               # delivery 1 (fail)
    for _ in range(5):                  # reclaim passes drive count to the cap
        agent._reclaim_pending()

    # Dead-lettered exactly once, then acked -> no longer pending, no infinite retry.
    assert len(store.deadletters) == 1
    assert store.deadletters[0]["subject"] == SUBJECT
    assert "poison" in store.deadletters[0]["error"]
    assert _pending(agent) == []
    assert agent.handled == []          # never succeeded


def test_poison_increments_the_poisoned_metric():
    before = metrics.REGISTRY.value(metrics.EVENTS_POISONED, agent="test", subject=SUBJECT)
    agent = _agent(fail_times=None)
    agent._max_deliveries = 2
    agent._ensure_groups()
    agent.emit(_event())
    agent._consume_once()
    for _ in range(4):
        agent._reclaim_pending()
    after = metrics.REGISTRY.value(metrics.EVENTS_POISONED, agent="test", subject=SUBJECT)
    assert after == before + 1


# --------------------------------------------------------------------------- #
# Terminal-on-first-try failures are acked, never retried
# --------------------------------------------------------------------------- #
def test_schema_invalid_message_is_dead_lettered_and_acked_not_retried():
    agent = _agent()
    store: InMemoryEventStore = agent.event_store  # type: ignore[assignment]
    agent._ensure_groups()
    # Bypass emit()'s validation to place a schema-invalid frame on the stream.
    bad = Event(subject=SUBJECT, schema_version=1, source="test", payload={"missing": "fields"})
    agent.bus.xadd(SUBJECT, {"data": json.dumps(bad.to_dict())})

    agent._consume_once()
    assert len(store.deadletters) == 1  # quarantined
    assert agent.handled == []          # handle() never reached
    assert _pending(agent) == []        # acked — a schema error can't be fixed by retrying


def test_unparseable_frame_is_dead_lettered_and_acked():
    agent = _agent()
    store: InMemoryEventStore = agent.event_store  # type: ignore[assignment]
    agent._ensure_groups()
    agent.bus.xadd(SUBJECT, {"data": "not-json{{"})

    agent._consume_once()
    assert len(store.deadletters) == 1
    assert store.deadletters[0]["error"].startswith("unparseable")
    assert _pending(agent) == []


# --------------------------------------------------------------------------- #
# Consumer groups — a message is processed once per group, fleet-wide
# --------------------------------------------------------------------------- #
def test_shared_group_delivers_a_message_to_only_one_consumer():
    redis = fakeredis.FakeStrictRedis(decode_responses=True)
    a = _ProgrammableAgent(
        bus=redis, event_store=InMemoryEventStore(), registry=SchemaRegistry(), fail_times=0
    )
    b = _ProgrammableAgent(
        bus=redis, event_store=InMemoryEventStore(), registry=SchemaRegistry(), fail_times=0
    )
    # Same agent name -> same group; distinct consumer identities.
    assert a._group == b._group and a._consumer != b._consumer
    a._ensure_groups()  # b shares the group (BUSYGROUP)
    b._ensure_groups()
    a.emit(_event())

    a._consume_once()
    b._consume_once()
    # Exactly one of them handled it — not both. (a reads first with `>`.)
    assert len(a.handled) + len(b.handled) == 1
