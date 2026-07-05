"""Durable event persistence — the append-only audit trail behind the bus.

Every event an agent emits is recorded in ``event_log`` (and every dead-letter in
``event_deadletter``) *before* it is published to Redis. Redis pub/sub is
fire-and-forget: with no durable record, an event with no connected subscriber is
lost outright — unacceptable for financial events. The store closes that gap and
gives Postgres the referential-integrity anchor the ``event_log`` foreign key
needs (see ``schema_registry/migrations/0001_init.sql``).

The store is an injected dependency so ``BaseAgent`` is testable without a
database: ``InMemoryEventStore`` for tests, ``PostgresEventStore`` in production.
Both are idempotent on the event id (``ON CONFLICT DO NOTHING``), so an
at-least-once redelivery never double-writes the log.
"""
from __future__ import annotations

import uuid
from typing import Mapping, Protocol

from agents.base.contract import Event


class EventStore(Protocol):
    def record_event(self, event: Event) -> None: ...
    def record_deadletter(self, envelope: Mapping) -> None: ...


class InMemoryEventStore:
    """Volatile store for tests. Idempotent on event id, mirroring Postgres."""

    def __init__(self) -> None:
        self.events: list[Event] = []
        self.deadletters: list[dict] = []
        self._event_ids: set[str] = set()
        self._deadletter_ids: set[str] = set()

    def record_event(self, event: Event) -> None:
        if event.id in self._event_ids:
            return
        self._event_ids.add(event.id)
        self.events.append(event)

    def record_deadletter(self, envelope: Mapping) -> None:
        key = str(envelope.get("id"))
        if key in self._deadletter_ids:
            return
        self._deadletter_ids.add(key)
        self.deadletters.append(dict(envelope))


def _as_uuid(value: object) -> uuid.UUID | None:
    """Coerce a correlation/id value to a UUID for the UUID columns, or None if
    it isn't one (correlation_id is a free-form string on the envelope)."""
    if not value:
        return None
    try:
        return uuid.UUID(str(value))
    except (ValueError, AttributeError, TypeError):
        return None


class PostgresEventStore:
    """Writes to ``event_log`` / ``event_deadletter``. Imports the db helper
    lazily so importing this module (e.g. for ``InMemoryEventStore`` in tests)
    never requires psycopg."""

    def record_event(self, event: Event) -> None:
        from psycopg.types.json import Json

        from shared.db import connection

        with connection() as conn:
            conn.execute(
                "INSERT INTO event_log "
                "(id, subject, schema_version, source, correlation_id, tenant_id, payload) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s) ON CONFLICT (id) DO NOTHING",
                (
                    _as_uuid(event.id),
                    event.subject,
                    event.schema_version,
                    event.source,
                    _as_uuid(event.correlation_id),
                    event.tenant_id,
                    Json(event.payload),
                ),
            )

    def record_deadletter(self, envelope: Mapping) -> None:
        from psycopg.types.json import Json

        from shared.db import connection

        with connection() as conn:
            conn.execute(
                "INSERT INTO event_deadletter (id, subject, source, raw, error) "
                "VALUES (%s, %s, %s, %s, %s) ON CONFLICT (id) DO NOTHING",
                (
                    _as_uuid(envelope.get("id")),
                    envelope.get("subject"),
                    envelope.get("source"),
                    Json(dict(envelope)),
                    envelope.get("error"),
                ),
            )
