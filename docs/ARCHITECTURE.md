# Architecture

Cavi ERP is an **event-sourced ERP-integration platform**: six single-purpose
agents cooperate over a message bus, never calling each other directly. Every
inter-agent message is a versioned `Event` validated against a central schema
registry; every event is durably recorded before it is published.

## The six agents

| Agent | Responsibility |
|---|---|
| **Vault** | Custodian of ERP credentials. Vends short-lived, scoped tokens and signs requests **in-process** (OAuth 1.0a TBA for NetSuite); raw secrets never leave it. Exposes an authenticated HTTP surface (`/vend`, `/sign`). |
| **Ledger** | Double-entry accounting core (post balanced journal entries; idempotent, tenant-scoped) **and** an external-ERP read path (`ledger.query.*`). |
| **Forge** | ERP write lifecycle: propose → approve/reject → execute, with an enforced approval gate. |
| **Ticker** | Pricing/FX **and** inbound ERP webhook ingestion (normalize → `ticker.event.received`). |
| **Mapper** | Anti-corruption layer: schema-version coercion and cross-ERP schema transforms. |
| **Beacon** | Notifications + reporting: classifies failures into alerts, deduplicates fleet-wide, delivers via Hermes, and rolls up KPIs. |

## The moving parts

```
producers ──emit(Event)──▶ [validate vs schema registry] ──▶ event_log (Postgres, durable)
                                                            └▶ Redis bus (publish)
                                                                     │
consumers ◀── run() loop ── _dispatch ── [validate] ──┬── handle()  (business logic)
                                                       └── _dead_letter ─▶ event_deadletter + deadletter.<subject>
```

- **Schema registry** (`schema_registry/`) — the `(subject, version)` pair is the
  single contract. JSON Schema files are the source of truth agents read at
  runtime; `scripts/bootstrap.py` mirrors them into the `schema_registry` table
  so Postgres can enforce the `event_log` foreign key.
- **Durable event log** — `BaseAgent.emit()` writes to `event_log` **before**
  publishing to Redis (Redis pub/sub is fire-and-forget). Dead-letters are
  written to `event_deadletter`. This is the audit trail and the replay source.
- **The bus** — Redis pub/sub today (at-most-once delivery; the durable log
  mitigates loss). Migrating to Redis Streams + consumer groups for true
  at-least-once processing is a planned change (see [adr/0003](adr/0003-bus-durability.md)).
- **Envelope** — every `Event` carries `id`, `subject`, `schema_version`,
  `source`, `correlation_id`, and **`tenant_id`** (tenant isolation is envelope
  metadata, not per-payload).

## Dependency injection everywhere

External systems are injected behind Protocols so the domain logic is testable
with no live infrastructure: `ErpReader`/`ErpWriter` (ERP calls), `EventStore`
(Postgres vs in-memory), `LedgerStore`, `DedupStore` (Redis vs in-memory),
`CredentialStore`. Production wires the real implementation; tests inject a stub.
This is why the suite runs with no Postgres/Redis.

## Data stores

- **Postgres** — the system of record: `event_log`, `event_deadletter`,
  `schema_registry`, and the double-entry books (`journal_entry`/`journal_line`,
  tenant-scoped). Accessed through a pooled `shared.db.connection()`.
- **Redis** — cache + bus + shared operational state (Beacon dedup).

## Observability

Structured JSON logs (`shared/logging.py`), an in-process metrics registry
(`shared/metrics.py`, counters per agent+subject), and a health surface
(`shared/health.py`: `/healthz` `/readyz` `/metrics`). `run_agent()`
(`agents/base/runtime.py`) wires all three into each agent's entrypoint.
