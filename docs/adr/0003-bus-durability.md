# ADR 0003 — Bus: Redis Streams + consumer groups

**Status:** accepted (implemented — A3b)

## Context
The bus was Redis pub/sub: at-most-once, no consumer groups, no acks, no
redelivery. The durable event log ([ADR 0001](0001-durable-event-log.md)) means a
committed event is never *lost*, but a consumer that crashed mid-`handle()`
didn't get the message redelivered — so at-least-once *processing* wasn't
guaranteed, and there was no backpressure or retry.

## Decision
`BaseAgent` consumes via **Redis Streams + consumer groups**: `XADD` on emit,
`XREADGROUP` per agent group, `XACK` only after `handle()` succeeds, with
reclaim-based retry and a poison dead-letter path. `fakeredis` implements
Streams, so the runtime stays testable without real infrastructure.

- **Group per agent, consumer per process.** The group name is the agent name,
  so every replica of one agent shares a group and each message is processed
  once fleet-wide. The consumer name is unique per process, so pending entries
  are attributable and a crashed process's in-flight work is reclaimable.
- **Ack on success only.** A raising `handle()` leaves the message unacked and
  therefore pending; `_reclaim_pending()` redelivers it (via `XCLAIM`) once it
  has been idle past `stream_reclaim_idle_ms` (the retry backoff). A
  schema-invalid event is terminal — it's dead-lettered and acked, not retried.
- **Poison cap.** After `stream_max_deliveries` failed attempts a message is
  dead-lettered and acked, so one bad event can't wedge the group behind it. The
  event is still in `event_log` (and now `event_deadletter`) — it is no longer
  retried *automatically*, not lost.
- **Groups start at the stream tail (`id="$"`).** Historical replay is the
  durable `event_log`'s job, not the stream's; the stream only needs enough
  history (`stream_maxlen`, approximate) to cover redelivery/reclaim.

## Pub/sub is retained for n8n
`emit()` (and the dead-letter path) **also** `publish()` to the same pub/sub
channel. n8n's `redisTrigger` node subscribes to pub/sub channels and cannot
read consumer groups, so the middleware fan-out (`sale-to-posting`,
`deadletter-escalation`, `netsuite-sync`) keeps working unchanged while the
Python agents move to the durable stream. If n8n later consumes Streams directly
(or is replaced), the `publish()` calls can be dropped.

## Consequences
- At-least-once delivery with redelivery of unacked messages; handlers must be
  (and already are, on the idempotent paths — ledger dedupes on entry id, the
  event log on event id) safe to re-run.
- In-flight messages survive a consumer crash and a Redis restart (Streams
  persist), improving the DR posture: a restarted agent's group still holds its
  pending entries.
- Every event now hits Redis twice (one `XADD`, one `PUBLISH`) for the n8n
  transition window — an accepted cost while both consumers coexist.
- Tuning lives in `Settings` (`stream_block_ms`, `stream_batch_size`,
  `stream_max_deliveries`, `stream_reclaim_idle_ms`, `stream_maxlen`).
