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

Both stores also maintain the **hash chain** (see ``shared/audit.py``): each
recorded event's ``chain_hash`` covers the previous event's hash plus its own
canonical content, making the log tamper-evident, not merely append-only.
Postgres serializes chain writes with a transaction-scoped advisory lock so
concurrent agents cannot fork the chain.

The chain covers the envelope **as persisted** (`chain_envelope`), not the
in-flight object: ``correlation_id`` is a UUID column, so a free-form
correlation string persists as NULL — hashing the original string would make
`audit_export --verify` flag every such event as tampered when it re-reads
the stored row. Both stores hash the same normalized form so their chains
agree.
"""
from __future__ import annotations

import uuid
from typing import Mapping, Protocol

from agents.base.contract import Event
from shared.audit import GENESIS_HASH, chain_hash

#: pg_advisory_xact_lock key serializing event_log chain appends ("caviaud").
_CHAIN_LOCK_KEY = 0x63617669_617564


class EventStore(Protocol):
    def record_event(self, event: Event) -> None: ...
    def record_deadletter(self, envelope: Mapping) -> None: ...


def chain_envelope(event: Event) -> dict:
    """The event envelope exactly as the audit store persists it — the form
    the hash chain covers. ``correlation_id`` is normalized the way the UUID
    column stores it (a non-UUID correlation persists as NULL), so verifying
    an export against the stored rows always reproduces the recorded hashes."""
    correlation = _as_uuid(event.correlation_id)
    return {
        **event.to_dict(),
        "correlation_id": str(correlation) if correlation else None,
    }


class InMemoryEventStore:
    """Volatile store for tests. Idempotent on event id, mirroring Postgres —
    including the hash chain (`chain_hashes[i]` covers `events[i]`)."""

    def __init__(self) -> None:
        self.events: list[Event] = []
        self.chain_hashes: list[str] = []
        self.deadletters: list[dict] = []
        self._event_ids: set[str] = set()
        self._deadletter_ids: set[str] = set()

    def record_event(self, event: Event) -> None:
        if event.id in self._event_ids:
            return
        self._event_ids.add(event.id)
        prev = self.chain_hashes[-1] if self.chain_hashes else GENESIS_HASH
        self.chain_hashes.append(chain_hash(prev, chain_envelope(event)))
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
            # Serialize chain appends: without this, two concurrent writers
            # could both read the same tip and fork the chain. The lock is
            # transaction-scoped, released automatically at commit/rollback.
            conn.execute("SELECT pg_advisory_xact_lock(%s)", (_CHAIN_LOCK_KEY,))
            row = conn.execute(
                "SELECT chain_hash FROM event_log "
                "WHERE chain_hash IS NOT NULL ORDER BY seq DESC LIMIT 1"
            ).fetchone()
            prev = row[0] if row else GENESIS_HASH
            conn.execute(
                "INSERT INTO event_log "
                "(id, subject, schema_version, source, correlation_id, tenant_id, "
                " payload, chain_hash) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s) ON CONFLICT (id) DO NOTHING",
                (
                    _as_uuid(event.id),
                    event.subject,
                    event.schema_version,
                    event.source,
                    _as_uuid(event.correlation_id),
                    event.tenant_id,
                    Json(event.payload),
                    chain_hash(prev, chain_envelope(event)),
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
