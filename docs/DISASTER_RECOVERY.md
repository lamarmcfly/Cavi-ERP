# Disaster recovery

## What's authoritative
**Postgres is the source of truth.** Everything needed to rebuild state lives
there: `event_log` (the append-only audit trail), `event_deadletter`, the books
(`journal_entry`/`journal_line`), and `schema_registry` + `schema_migrations`.
Redis is a cache + bus + operational state (Beacon dedup) — **disposable**;
losing it costs in-flight delivery and dedup windows, not durable data.

## Backups
- **Postgres:** regular `pg_dump` (or a managed-service PITR). This single backup
  captures the audit log, the books, and the registry state. Test-restore
  periodically.
- **Vault credentials:** stored per tenant in the OS keyring (host) or seeded
  from env (containers) — **not** in Postgres. Back these up in your secret
  manager; they are not in the DB dump by design.
- **Schemas:** the `schema_registry/schemas/*.json` files are in git (the real
  source of truth); the table is rebuilt with `scripts.bootstrap`.

## RPO / RTO posture
- **RPO:** bounded by the Postgres backup cadence (events are written durably
  before publish, so a committed event is recoverable). Redis loss = ~0 durable
  data loss (only undelivered in-flight messages + dedup windows).
- **RTO:** restore Postgres → run `scripts.migrate` (schema) → `scripts.bootstrap`
  (registry) → bring Redis up → start agents. Health probes gate readiness.

## Rebuild order
1. Restore/stand up **Postgres** from backup.
2. `python -m scripts.migrate` — ensure schema is current.
3. `python -m scripts.bootstrap` — reload the schema registry table.
4. Stand up **Redis** (empty is fine).
5. Restore **Vault** credentials into the secret store.
6. Start **Vault**, then the bus agents (`--profile agents`); watch `/readyz`.

## Not yet covered (follow-ups)
Automated backup verification, cross-region replication, and a rehearsed
game-day runbook. In-flight messages are now recoverable: the bus uses Redis
Streams + consumer groups, so a consumer that dies mid-`handle()` leaves its
message pending and a restarted agent (or a sibling replica) redelivers it — an
event is only acked once processed. Streams persist across a Redis restart, so
in-flight work survives an outage rather than being lost.
