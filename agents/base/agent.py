"""BaseAgent — the common runtime every Cavi ERP agent inherits.

Responsibilities handled here so individual agents stay focused on business
logic:
  * connect to the Redis event bus
  * consume the subjects the agent declares (Redis Streams consumer group)
  * validate every inbound event against the schema registry
  * hand valid events to the subclass's `handle()` method
  * emit new events back onto the bus

Concrete agents subclass this and implement `subjects` + `handle()`.

Delivery model (see ``docs/adr/0003-bus-durability.md``)
--------------------------------------------------------
Agents consume via **Redis Streams + consumer groups**, not pub/sub: ``XADD`` on
emit, ``XREADGROUP`` per agent group, ``XACK`` only after ``handle()`` succeeds.
A consumer that crashes mid-``handle()`` never ``XACK``s, so Redis keeps the
message pending and it is redelivered — at-least-once *processing*, not just the
at-most-once fan-out pub/sub gave us. The group name is the agent name, so every
replica of one agent shares a group and a message is processed once fleet-wide
(a storm pages a human once, not once per pod). A message whose ``handle()``
fails ``stream_max_deliveries`` times is poison: dead-lettered and acked so it
can't wedge the group behind it.

``emit()`` still ``publish()``es to pub/sub *as well* as ``XADD``: n8n's
``redisTrigger`` node subscribes to pub/sub channels and can't read consumer
groups, so the pub/sub fan-out is retained for the middleware layer while agents
move to the durable stream.
"""
from __future__ import annotations

import abc
import json
import logging
import uuid

import redis
from jsonschema import ValidationError

from agents.base.contract import Event
from agents.base.registry import SchemaNotFound, SchemaRegistry
from shared import metrics
from shared.cache import get_client
from shared.settings import get_settings

log = logging.getLogger("cavi.agent")


class BaseAgent(abc.ABC):
    #: name of the agent, e.g. "ledger" — set by each subclass
    name: str = "base"

    def __init__(self, *, bus=None, registry=None, event_store=None, settings=None) -> None:
        # All deps injectable so the runtime is testable without redis/psycopg;
        # production defaults connect lazily.
        self.bus = bus if bus is not None else get_client()
        self.registry = registry or SchemaRegistry()
        if event_store is None:
            from shared.events import PostgresEventStore

            event_store = PostgresEventStore()
        self.event_store = event_store

        settings = settings or get_settings()
        # Consumer-group identity. The group is the agent name so every replica
        # shares it (each message processed once fleet-wide); the consumer name
        # is unique per process so pending entries can be attributed and, when a
        # process dies, reclaimed by a sibling.
        self._group = self.name
        self._consumer = f"{self.name}-{uuid.uuid4().hex[:12]}"
        self._block_ms = settings.stream_block_ms
        self._batch = settings.stream_batch_size
        self._max_deliveries = settings.stream_max_deliveries
        self._reclaim_idle_ms = settings.stream_reclaim_idle_ms
        self._maxlen = settings.stream_maxlen
        # Loop guard — `stop()` flips it so `run()` returns cleanly (used in tests
        # and for graceful shutdown).
        self._running = True

    # --- contract each concrete agent fills in -------------------------------
    @property
    @abc.abstractmethod
    def subjects(self) -> list[str]:
        """Event subjects this agent consumes, e.g. ['ledger.entry']."""

    @abc.abstractmethod
    def handle(self, event: Event) -> None:
        """Process a validated inbound event (the business logic).

        Must be safe to re-run: with at-least-once delivery a crash after
        ``handle()`` but before ``XACK`` causes the same event to be redelivered.
        The financial paths are already idempotent (ledger writes dedupe on
        entry id; the event log dedupes on event id), so a replay is a no-op.
        """

    # --- runtime -------------------------------------------------------------
    def emit(self, event: Event) -> None:
        """Validate, durably record, then publish an event onto the bus.

        Durable-first: the event is written to the append-only ``event_log``
        *before* it is put on the bus, so an audit record and a replay source
        survive even if no consumer is connected. It is then ``XADD``ed to the
        subject's stream (durable at-least-once delivery to agent consumer
        groups) and ``publish``ed to the same pub/sub channel (fan-out to n8n's
        ``redisTrigger`` workflows, which can't read consumer groups).
        """
        self.registry.validate(event.subject, event.schema_version, event.payload)
        self.event_store.record_event(event)
        data = json.dumps(event.to_dict())
        self.bus.xadd(event.subject, {"data": data}, maxlen=self._maxlen, approximate=True)
        self.bus.publish(event.subject, data)
        metrics.REGISTRY.inc(metrics.EVENTS_EMITTED, agent=self.name, subject=event.subject)
        log.info(
            "%s emitted %s (%s)", self.name, event.subject, event.id,
            extra={
                "agent": self.name, "subject": event.subject, "event_id": event.id,
                "correlation_id": event.correlation_id, "tenant_id": event.tenant_id,
            },
        )

    def run(self) -> None:
        """Consume declared subjects via the agent's consumer group, forever.

        Each pass first reclaims messages a dead sibling left pending (so a crash
        doesn't strand in-flight work), then reads new messages. ``XREADGROUP``
        blocks up to ``stream_block_ms`` when a stream is empty, so the loop
        idles cheaply.
        """
        self._ensure_groups()
        log.info(
            "%s consuming %s via group '%s' as '%s'",
            self.name, self.subjects, self._group, self._consumer,
            extra={"agent": self.name, "group": self._group, "consumer": self._consumer},
        )
        while self._running:
            self._reclaim_pending()
            self._consume_once()

    def stop(self) -> None:
        """Ask `run()` to return after the current pass (graceful shutdown)."""
        self._running = False

    def _ensure_groups(self) -> None:
        """Create the consumer group on each subject stream (idempotent).

        ``id="$"`` means the group starts at the stream tail — it consumes
        messages emitted from now on. Anything older is the durable
        ``event_log``'s job to replay, not the stream's. ``mkstream`` creates the
        stream if this agent starts before any producer has emitted to it.
        ``BUSYGROUP`` just means the group already exists (a restart), which is
        exactly what we want: its pending entries are preserved for redelivery.
        """
        for subject in self.subjects:
            try:
                self.bus.xgroup_create(subject, self._group, id="$", mkstream=True)
            except redis.exceptions.ResponseError as exc:
                if "BUSYGROUP" not in str(exc):
                    raise

    def _consume_once(self) -> None:
        """Read one batch of new (never-delivered) messages and process them."""
        streams = {subject: ">" for subject in self.subjects}
        response = self.bus.xreadgroup(
            self._group, self._consumer, streams, count=self._batch, block=self._block_ms
        )
        for stream, entries in response or []:
            for msg_id, fields in entries:
                self._handle_delivery(stream, msg_id, fields)

    def _reclaim_pending(self) -> None:
        """Redeliver messages left pending by a crashed consumer, or poison them.

        A message is pending when it was delivered but never ``XACK``ed — its
        consumer died mid-``handle()``, or ``handle()`` raised and we deliberately
        left it for retry. We only touch entries idle longer than
        ``stream_reclaim_idle_ms`` (the retry backoff, and a guard against
        stealing work a live sibling is mid-flight on). Past
        ``stream_max_deliveries`` attempts the message is poison — dead-lettered
        and acked so it stops blocking redelivery of everything behind it.
        """
        for stream in self.subjects:
            pending = self.bus.xpending_range(
                stream, self._group, min="-", max="+",
                count=self._batch, idle=self._reclaim_idle_ms,
            )
            for entry in pending:
                msg_id = entry["message_id"]
                delivered = entry["times_delivered"]
                if delivered >= self._max_deliveries:
                    self._poison(stream, msg_id, delivered)
                    continue
                # Claim to ourselves (bumping the delivery count) and retry. The
                # min-idle guard makes the claim a no-op if a sibling grabbed it
                # first, so two consumers can't both process the same message.
                claimed = self.bus.xclaim(
                    stream, self._group, self._consumer,
                    min_idle_time=self._reclaim_idle_ms, message_ids=[msg_id],
                )
                for cid, fields in claimed:
                    self._handle_delivery(stream, cid, fields)

    def _handle_delivery(self, stream: str, msg_id: str, fields: dict) -> None:
        """Dispatch one stream message, acking only if it is terminally handled.

        Ack semantics are the heart of at-least-once processing:
          * ``handle()`` succeeds (or the event is schema-invalid and gets
            dead-lettered inside ``_dispatch``) -> terminally handled -> ``XACK``.
          * ``handle()`` raises -> *not* acked -> stays pending -> redelivered by
            ``_reclaim_pending`` after the backoff, until it succeeds or hits the
            poison cap.
        A frame we can't even parse into an ``Event`` can never succeed, so it is
        dead-lettered and acked immediately rather than retried forever.
        """
        raw = fields.get("data", "")
        try:
            event = Event.from_dict(json.loads(raw))
        except (ValueError, TypeError, KeyError) as exc:
            self._dead_letter_raw(stream, raw, f"unparseable stream frame: {exc}")
            self.bus.xack(stream, self._group, msg_id)
            return
        try:
            self._dispatch(event)
        except Exception:
            # A transient handler failure. Leave the message pending (no ack) so
            # it is redelivered; log loudly so a persistent failure is visible.
            metrics.REGISTRY.inc(metrics.EVENTS_RETRIED, agent=self.name, subject=event.subject)
            log.exception(
                "%s handler failed for %s on %s; leaving pending for retry",
                self.name, msg_id, stream,
                extra={
                    "agent": self.name, "subject": event.subject, "event_id": event.id,
                    "correlation_id": event.correlation_id, "tenant_id": event.tenant_id,
                    "stream_id": msg_id,
                },
            )
            return
        self.bus.xack(stream, self._group, msg_id)
        metrics.REGISTRY.inc(metrics.EVENTS_ACKED, agent=self.name, subject=event.subject)

    def _poison(self, stream: str, msg_id: str, delivered: int) -> None:
        """Dead-letter a message that has failed `handle()` too many times.

        Poison messages would otherwise be redelivered forever, starving every
        message behind them. We fetch the body, route it to the dead-letter path
        (Beacon + the durable deadletter log), then ``XACK`` so the group moves
        on. The financial event is never lost — it lives in ``event_log`` and now
        ``event_deadletter`` — it is just no longer retried automatically.
        """
        for _id, fields in self.bus.xrange(stream, min=msg_id, max=msg_id):
            raw = fields.get("data", "")
            error = f"poison: handler failed {delivered} times"
            try:
                self._dead_letter(Event.from_dict(json.loads(raw)), error)
            except (ValueError, TypeError, KeyError) as exc:
                self._dead_letter_raw(stream, raw, f"{error}; also unparseable: {exc}")
        self.bus.xack(stream, self._group, msg_id)
        metrics.REGISTRY.inc(metrics.EVENTS_POISONED, agent=self.name, subject=stream)

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

        Policy (chosen): dead-letter + alert. A schema-invalid event is
        republished to `deadletter.<subject>` with the error attached and a
        warning is logged, so nothing financial is lost and Beacon can pick it up
        — while the bus keeps flowing for every other event. A schema failure is
        terminal (retrying won't make it valid), so the caller acks it; only a
        raising ``handle()`` triggers redelivery.
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
        it at ERROR so the contract gap is visible. Like `emit`, the envelope is
        both ``XADD``ed (Beacon consumes the deadletter stream) and ``publish``ed
        (n8n's deadletter-escalation workflow subscribes to the pub/sub channel).
        """
        envelope = {**event.to_dict(), "error": error}
        try:
            self.registry.validate("deadletter.envelope", 1, envelope)
        except (SchemaNotFound, ValidationError) as exc:
            log.error("%s dead-letter envelope invalid for %s: %s", self.name, event.id, exc)
        self.event_store.record_deadletter(envelope)
        subject = f"deadletter.{event.subject}"
        data = json.dumps(envelope)
        self.bus.xadd(subject, {"data": data}, maxlen=self._maxlen, approximate=True)
        self.bus.publish(subject, data)
        metrics.REGISTRY.inc(metrics.DEADLETTERS, agent=self.name, subject=event.subject)
        log.warning(
            "%s dead-lettered %s: %s", self.name, event.id, error,
            extra={
                "agent": self.name, "subject": event.subject, "event_id": event.id,
                "correlation_id": event.correlation_id, "tenant_id": event.tenant_id,
                "error": error,
            },
        )

    def _dead_letter_raw(self, stream: str, raw: object, error: str) -> None:
        """Dead-letter a frame that never parsed into an `Event`.

        A stream frame we can't decode has no id/subject/correlation to trust, so
        it skips the envelope-schema check and is recorded with a synthetic id.
        This should never fire for our own producers (``emit`` always writes a
        valid Event) — it is defense against a corrupt or foreign frame wedging
        the group. Logged at ERROR because it means something put a bad frame on
        the bus.
        """
        envelope = {
            "id": str(uuid.uuid4()),
            "subject": stream,
            "source": self.name,
            "raw": raw,
            "error": error,
        }
        self.event_store.record_deadletter(envelope)
        data = json.dumps(envelope)
        self.bus.xadd(f"deadletter.{stream}", {"data": data}, maxlen=self._maxlen, approximate=True)
        self.bus.publish(f"deadletter.{stream}", data)
        metrics.REGISTRY.inc(metrics.DEADLETTERS, agent=self.name, subject=stream)
        log.error(
            "%s dead-lettered unparseable frame on %s: %s", self.name, stream, error,
            extra={"agent": self.name, "subject": stream, "error": error},
        )
