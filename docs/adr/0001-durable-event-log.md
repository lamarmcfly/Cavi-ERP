# ADR 0001 — Durable-first event log

**Status:** accepted

## Context
Agents communicate over Redis pub/sub, which is fire-and-forget (at-most-once).
An event published with no connected subscriber is lost. For financial events
that is unacceptable, and the README claimed Postgres was the "source of truth"
while nothing actually wrote to `event_log`.

## Decision
`BaseAgent.emit()` writes the event to the append-only `event_log` **before**
publishing to Redis; `_dead_letter()` writes to `event_deadletter` before
publishing the quarantine message. Writes are idempotent on the event id
(`ON CONFLICT DO NOTHING`). Persistence is an injected `EventStore`
(Postgres in prod, in-memory in tests).

## Consequences
- A committed event has a durable audit record + replay source even if the bus
  drops it. Redis becomes a transport, not a system of record.
- Emit now depends on Postgres availability (fails loudly rather than losing
  data). Acceptable: no event is considered processed without its log row.
- True at-least-once *processing* still needs consumer acks/redelivery — see
  [ADR 0003](0003-bus-durability.md).
