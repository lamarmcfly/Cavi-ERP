"""Tests for the BaseAgent runtime — durable emit + dead-letter path.

Exercises the wiring that production depends on but that was previously untested:
every emit is recorded to the event log before publishing, and a schema
violation is quarantined (recorded + dead-lettered) instead of reaching handle().
Uses fakeredis for the bus and an in-memory event store — no infrastructure.
"""
from __future__ import annotations

import json

import fakeredis

from agents.base.agent import BaseAgent
from agents.base.contract import Event
from agents.base.registry import SchemaRegistry
from shared.events import InMemoryEventStore

VALID_DENIED = {
    "tenant_id": "tenant-acme",
    "erp_platform": "sap",
    "reason": "no active grant",
    "requested_at": "2026-07-04T12:00:00Z",
}


class _RecordingAgent(BaseAgent):
    name = "test"

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.handled: list[Event] = []

    @property
    def subjects(self) -> list[str]:
        return ["vault.secret.denied"]

    def handle(self, event: Event) -> None:
        self.handled.append(event)


def _agent(store: InMemoryEventStore | None = None) -> _RecordingAgent:
    return _RecordingAgent(
        bus=fakeredis.FakeStrictRedis(decode_responses=True),
        event_store=store or InMemoryEventStore(),
        registry=SchemaRegistry(),
    )


def test_emit_records_to_event_log_then_publishes():
    store = InMemoryEventStore()
    agent = _agent(store)
    pubsub = agent.bus.pubsub()
    pubsub.subscribe("vault.secret.denied")
    pubsub.get_message(timeout=1)  # consume the subscribe confirmation

    event = Event(subject="vault.secret.denied", schema_version=1, source="test",
                  payload=VALID_DENIED)
    agent.emit(event)

    # Durable-first: the event is in the append-only log.
    assert store.events == [event]
    # ...and it was published to the bus.
    msg = pubsub.get_message(timeout=1)
    assert msg is not None and msg["type"] == "message"
    assert json.loads(msg["data"])["subject"] == "vault.secret.denied"


def test_emit_validates_before_recording():
    agent = _agent()
    bad = Event(subject="vault.secret.denied", schema_version=1, source="test",
                payload={"missing": "fields"})
    import jsonschema
    try:
        agent.emit(bad)
        raised = False
    except jsonschema.ValidationError:
        raised = True
    assert raised
    # Nothing recorded or published for an invalid event.
    assert agent.event_store.events == []


def test_emit_is_idempotent_on_event_id():
    store = InMemoryEventStore()
    agent = _agent(store)
    event = Event(subject="vault.secret.denied", schema_version=1, source="test",
                  payload=VALID_DENIED, id="11111111-1111-1111-1111-111111111111")
    agent.emit(event)
    agent.emit(event)  # bus redelivery of the same id
    assert len(store.events) == 1


def test_dispatch_quarantines_an_invalid_event():
    store = InMemoryEventStore()
    agent = _agent(store)
    bad = Event(subject="vault.secret.denied", schema_version=1, source="test",
                payload={"not": "valid"})
    agent._dispatch(bad)
    # Recorded to the dead-letter log, and handle() was NOT reached.
    assert len(store.deadletters) == 1
    assert store.deadletters[0]["subject"] == "vault.secret.denied"
    assert agent.handled == []


def test_dispatch_routes_a_valid_event_to_handle():
    agent = _agent()
    good = Event(subject="vault.secret.denied", schema_version=1, source="test",
                 payload=VALID_DENIED)
    agent._dispatch(good)
    assert agent.handled == [good]
    assert agent.event_store.deadletters == []


class _FakePubSub:
    def __init__(self, messages):
        self._messages = messages
        self.subscribed: list = []

    def subscribe(self, *subjects):
        self.subscribed.extend(subjects)

    def listen(self):
        # Finite iterator so run()'s `for message in ...` loop terminates.
        return iter(self._messages)


class _FakeBus:
    def __init__(self, messages):
        self._pubsub = _FakePubSub(messages)
        self.published: list = []

    def pubsub(self):
        return self._pubsub

    def publish(self, channel, data):
        self.published.append((channel, data))


def test_emit_and_deadletter_increment_metrics():
    from shared import metrics

    agent = _agent()
    subj = "vault.secret.denied"
    emitted_before = metrics.REGISTRY.value(metrics.EVENTS_EMITTED, agent="test", subject=subj)
    dl_before = metrics.REGISTRY.value(metrics.DEADLETTERS, agent="test", subject=subj)

    agent.emit(Event(subject=subj, schema_version=1, source="test", payload=VALID_DENIED))
    agent._dispatch(Event(subject=subj, schema_version=1, source="test", payload={"bad": 1}))

    assert metrics.REGISTRY.value(metrics.EVENTS_EMITTED, agent="test", subject=subj) == emitted_before + 1
    assert metrics.REGISTRY.value(metrics.DEADLETTERS, agent="test", subject=subj) == dl_before + 1


def test_run_subscribes_and_dispatches_bus_messages():
    good = Event(subject="vault.secret.denied", schema_version=1, source="test",
                 payload=VALID_DENIED)
    messages = [
        {"type": "subscribe", "data": 1},                         # non-message: skipped
        {"type": "message", "data": json.dumps(good.to_dict())},   # dispatched
    ]
    agent = _RecordingAgent(
        bus=_FakeBus(messages), event_store=InMemoryEventStore(), registry=SchemaRegistry()
    )
    agent.run()  # returns once the finite message stream is exhausted

    assert agent.bus.pubsub().subscribed == ["vault.secret.denied"]
    assert len(agent.handled) == 1
    assert agent.handled[0].subject == "vault.secret.denied"
