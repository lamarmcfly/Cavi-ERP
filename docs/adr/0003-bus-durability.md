# ADR 0003 — Bus: pub/sub now, Redis Streams next

**Status:** proposed

## Context
The bus is Redis pub/sub: at-most-once, no consumer groups, no acks, no
redelivery. The durable event log ([ADR 0001](0001-durable-event-log.md)) means a
committed event is never *lost*, but a consumer that crashes mid-`handle()`
doesn't get the message redelivered — so at-least-once *processing* isn't
guaranteed, and there's no backpressure or retry.

## Decision (proposed)
Migrate `BaseAgent`'s consumption from `pubsub` to **Redis Streams + consumer
groups**: `XADD` on emit, `XREADGROUP` per agent group, `XACK` after successful
`handle()`, with retry/backoff and a dead-letter path for poison messages.
`fakeredis` supports Streams, so it stays testable.

## Consequences
- At-least-once delivery with redelivery of unacked messages; handlers must be
  (and already are, on the idempotent paths) safe to re-run.
- In-flight messages survive a Redis restart (Streams persist), improving the DR
  posture.
- A core-runtime change to `run()`/`emit()` — landed as its own milestone
  (tracked separately) rather than bundled with the reliability slice that added
  durable Beacon dedup.
