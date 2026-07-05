# Incident response (on-call)

First move for any incident: check `/readyz` and `/metrics` on the affected
agent, and query the JSON logs by `correlation_id`.

## A dead-letter storm (Beacon paging)
**Symptom:** repeated `deadletter.*` alerts. **Cause:** a producer is emitting
events that fail their contract (bad payload or unregistered subject).
1. Find the failures: `SELECT subject, error, count(*) FROM event_deadletter
   GROUP BY subject, error ORDER BY 3 DESC;`
2. `cavi_deadletters_total{subject=...}` shows the rate; the log line's
   `correlation_id` traces the origin.
3. Fix the producer or register/deploy the missing schema
   (`scripts.bootstrap`). Beacon dedup (Redis, fleet-wide) already collapses the
   page storm — it won't re-page across restarts.

## Replay a quarantined event
The dead-letter envelope in `event_deadletter.raw` preserves the original event.
After fixing the contract, re-publish the `raw.payload` under its subject (via
the owning agent / a small replay script). Because `emit()`/posting are
idempotent on `id`/`entry_id`, replay is safe.

## Vault down / refusing to sign
- `503 "vault authentication not configured"` ⇒ `CAVI_VAULT_API_SECRET` is unset
  (fail-closed). Set it (and the matching value on n8n) and restart.
- `401` from n8n → Vault ⇒ the secrets don't match on both sides.
- Vault unreachable ⇒ ERP writes stall (by design — no signing, no writes). Check
  the Vault `/healthz` and the pod/container.

## Postgres down
Emits/posts fail loudly (the durable-first write can't complete) rather than
losing data silently. Restore Postgres; the connection pool reconnects. No event
is acknowledged as processed without its `event_log` row.

## Redis down
The bus stops flowing and Beacon dedup can't record — agents can't publish/
consume. Restore Redis; because events are in `event_log`, nothing is lost, and
because the bus uses Redis Streams + consumer groups, in-flight messages left
unacked when Redis went down are redelivered on recovery (an event is acked only
after `handle()` succeeds) rather than dropped.

## Escalation
Financial-impacting incidents (dead-lettered `ledger.*`, Vault compromise
suspicion) are CRITICAL — page a human immediately; Beacon already routes those
to Hermes.
