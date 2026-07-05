"""BaseAgent — the common runtime every Cavi ERP agent inherits.

Responsibilities handled here so individual agents stay focused on business
logic:
  * connect to the Redis event bus
  * subscribe to the subjects the agent declares
  * validate every inbound event against the schema registry
  * hand valid events to the subclass's `handle()` method
  * emit new events back onto the bus

Concrete agents subclass this and implement `subjects` + `handle()`.
"""
from __future__ import annotations

import abc
import json
import logging

from jsonschema import ValidationError

from agents.base.contract import Event
from agents.base.registry import SchemaNotFound, SchemaRegistry
from shared import metrics
from shared.cache import get_client

log = logging.getLogger("cavi.agent")


class BaseAgent(abc.ABC):
    #: name of the agent, e.g. "ledger" — set by each subclass
    name: str = "base"

    def __init__(self, *, bus=None, registry=None, event_store=None) -> None:
        # All deps injectable so the runtime is testable without redis/psycopg;
        # production defaults connect lazily.
        self.bus = bus if bus is not None else get_client()
        self.registry = registry or SchemaRegistry()
        if event_store is None:
            from shared.events import PostgresEventStore

            event_store = PostgresEventStore()
        self.event_store = event_store

    # --- contract each concrete agent fills in -------------------------------
    @property
    @abc.abstractmethod
    def subjects(self) -> list[str]:
        """Event subjects this agent consumes, e.g. ['ledger.entry']."""

    @abc.abstractmethod
    def handle(self, event: Event) -> None:
        """Process a validated inbound event (the business logic)."""

    # --- runtime -------------------------------------------------------------
    def emit(self, event: Event) -> None:
        """Validate, durably record, then publish an event onto the bus.

        Durable-first: the event is written to the append-only ``event_log``
        *before* it is published. Redis pub/sub is fire-and-forget, so without
        the log an event with no connected subscriber would be lost; recording
        first guarantees an audit record and a replay source survive.
        """
        self.registry.validate(event.subject, event.schema_version, event.payload)
        self.event_store.record_event(event)
        self.bus.publish(event.subject, json.dumps(event.to_dict()))
        metrics.REGISTRY.inc(metrics.EVENTS_EMITTED, agent=self.name, subject=event.subject)
        log.info(
            "%s emitted %s (%s)", self.name, event.subject, event.id,
            extra={
                "agent": self.name, "subject": event.subject, "event_id": event.id,
                "correlation_id": event.correlation_id, "tenant_id": event.tenant_id,
            },
        )

    def run(self) -> None:
        """Subscribe to declared subjects and process events forever."""
        pubsub = self.bus.pubsub()
        pubsub.subscribe(*self.subjects)
        log.info("%s listening on %s", self.name, self.subjects)
        for message in pubsub.listen():
            if message["type"] != "message":
                continue
            event = Event.from_dict(json.loads(message["data"]))
            self._dispatch(event)

    def _dispatch(self, event: Event) -> None:
        """Validate an inbound event against its registered schema, then route it.

        This is the agent's first line of defense. An event may fail validation
        because the producer used an unregistered subject (`SchemaNotFound`) or
        because the payload violates the registered contract
        (`jsonschema.ValidationError`). How an ERP reacts to a malformed event
        is a real design decision with money on the line:

          * Drop it silently?        -> data loss, but the bus keeps flowing.
          * Crash the agent?         -> loud, but one bad event halts everything.
          * Route to a dead-letter   -> recoverable, but needs a quarantine
            subject for later replay?    subject + someone to watch it.
          * Forward to Mapper to     -> self-healing, but risks masking real
            attempt a version coerce?    producer bugs.

        TODO(you): implement the dispatch policy below. A reasonable shape:
          1. try self.registry.validate(...) on the event
          2. on success, call self.handle(event)
          3. on SchemaNotFound / jsonschema.ValidationError, apply your chosen
             failure strategy (e.g. publish to a 'deadletter.<subject>' channel
             with the error attached, and log a warning)
        Policy (chosen): dead-letter + alert. A failed event is republished to
        `deadletter.<subject>` with the error attached and a warning is logged,
        so nothing financial is lost and Beacon can pick it up — while the bus
        keeps flowing for every other event.
        """
        try:
            self.registry.validate(event.subject, event.schema_version, event.payload)
        except (SchemaNotFound, ValidationError) as exc:
            self._dead_letter(event, str(exc))
            return
        metrics.REGISTRY.inc(metrics.EVENTS_DISPATCHED, agent=self.name, subject=event.subject)
        self.handle(event)

    def _dead_letter(self, event: Event, error: str) -> None:
        """Quarantine a failed event on `deadletter.<subject>` for Beacon/replay.

        The envelope is validated against `deadletter.envelope.v1` so even the
        failure path can't emit a malformed record. If the envelope itself is
        somehow invalid we still publish (never drop a financial event) but log
        it at ERROR so the contract gap is visible.
        """
        envelope = {**event.to_dict(), "error": error}
        try:
            self.registry.validate("deadletter.envelope", 1, envelope)
        except (SchemaNotFound, ValidationError) as exc:
            log.error("%s dead-letter envelope invalid for %s: %s", self.name, event.id, exc)
        self.event_store.record_deadletter(envelope)
        self.bus.publish(f"deadletter.{event.subject}", json.dumps(envelope))
        metrics.REGISTRY.inc(metrics.DEADLETTERS, agent=self.name, subject=event.subject)
        log.warning(
            "%s dead-lettered %s: %s", self.name, event.id, error,
            extra={
                "agent": self.name, "subject": event.subject, "event_id": event.id,
                "correlation_id": event.correlation_id, "tenant_id": event.tenant_id,
                "error": error,
            },
        )
