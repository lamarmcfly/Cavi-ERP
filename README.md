# Cavi ERP

An event-driven ERP built from six cooperating agents, routed by **n8n**
middleware, contracted by a **PostgreSQL** schema registry, and accelerated by
a **Redis** cache + event bus.

Agents never call each other directly. Each emits versioned `Event` envelopes
onto the bus; n8n workflows route them; every event is validated against the
schema registry before it is handled. This keeps the agents independently
deployable and the contracts in one auditable place.

## The six agents

| Agent      | Role |
|------------|------|
| **Vault**  | Custodian of secrets & sensitive master data (credentials, vendor bank details, PII). Brokers reference tokens instead of raw secrets. |
| **Ledger** | Double-entry accounting core and financial system of record. Postings must never be silently lost. |
| **Forge**  | Production & order fulfillment. Turns demand into work orders and tracks them to completion. |
| **Ticker** | Time, pricing & scheduling. Real-time price/FX snapshots (cached) and scheduled events like period close. |
| **Mapper** | Anti-corruption layer. Translates between schema versions and foreign external formats. |
| **Beacon** | Notifications, alerting & observability. Natural sink for dead-lettered events. |

## Architecture

```
                    ┌──────────────────────────────────────┐
                    │          n8n middleware              │
                    │   (routes & orchestrates events)     │
                    └───────────────▲──────────────────────┘
                                    │  subscribe / trigger
            publish Event envelopes │
   ┌───────┬───────┬───────┬───────┴─┬───────┬───────┐
   │ Vault │ Ledger│ Forge │ Ticker  │ Mapper│ Beacon│   ← the six agents
   └───┬───┴───┬───┴───┬───┴────┬────┴───┬───┴───┬───┘
       │       │       │        │        │       │
       └───────┴───────┴────┬───┴────────┴───────┘
                            │
              ┌─────────────┴─────────────┐
              │   Redis (cache + bus)     │
              └─────────────┬─────────────┘
                            │ durable record + contract enforcement
              ┌─────────────┴─────────────┐
              │  PostgreSQL               │
              │  • schema_registry        │
              │  • event_log              │
              │  • event_deadletter       │
              └───────────────────────────┘
```

## Repository layout

```
cavi-erp/
├── agents/
│   ├── base/            # BaseAgent runtime, Event contract, SchemaRegistry client
│   ├── vault/  ledger/  forge/  ticker/  mapper/  beacon/
├── middleware/n8n/      # workflow JSON + docs (the router)
├── schema_registry/     # versioned JSON Schemas + SQL migrations (the contract)
├── cache/redis/         # redis.conf
├── shared/              # settings, db, cache helpers
├── scripts/bootstrap.py # load schemas into Postgres
├── tests/               # contract smoke tests (no infra required)
├── docker-compose.yml   # postgres + redis + n8n
├── requirements.txt
└── .env.example
```

## How an event flows (worked example: a sale)

1. **Forge** completes a work order and `emit()`s a `forge.completed` event.
   `emit()` validates the payload against `forge.completed.v1` first.
2. An **n8n** workflow subscribed to `forge.completed` fans it out: it triggers
   **Ledger** (post revenue) and **Beacon** (notify).
3. **Ledger** receives the event, its `BaseAgent` runtime re-validates it against
   the registry, then `handle()` posts a balanced `ledger.entry`.
4. If the event ever fails validation, the dispatch policy routes it to a
   dead-letter subject that **Beacon** watches — nothing financial vanishes.

The `correlation_id` on every envelope ties all of these together so one sale is
traceable end to end.

## Getting started

```bash
# 1. Install deps
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt

# 2. Configure
cp .env.example .env        # edit secrets

# 3. Bring up infrastructure (Postgres + Redis + n8n)
docker compose up -d

# 4. Load the schema contracts into Postgres
python -m scripts.bootstrap

# 5. Run an agent (each is its own process)
python -m agents.ledger.agent

# 6. Verify the scaffold
pytest
```

## Design notes

* **Schema registry as the backbone.** The `(subject, version)` pair is the
  single contract. Producers and consumers evolve independently as long as both
  honor a registered schema. See `schema_registry/README.md`.
* **Two stores, two jobs.** Redis is fast + ephemeral (cache + pub/sub);
  Postgres is durable + authoritative (`event_log` is the source of truth).
* **Mapper is the escape hatch** for breaking schema changes — route old
  producers through it to coerce v1 → v2 rather than a flag-day migration.

## Status

**Enterprise hardening in progress — not yet live** (no production tenant).

**Shipped:** all six agents emit their canonical contracts; a CI gate
(ruff / mypy / pytest); fail-closed Vault credential auth; a durable
`event_log` / `event_deadletter` audit trail (durable-first emit); idempotent,
pooled, **tenant-isolated** ledger writes; fleet-wide durable Beacon dedup;
**at-least-once processing** on the bus (Redis Streams + consumer groups —
`XACK` only after `handle()`, reclaim-based retry, poison dead-letter cap; see
[docs/adr/0003](docs/adr/0003-bus-durability.md)); structured JSON logs + metrics
+ `/healthz` `/readyz` `/metrics`; a tracked migration runner (`scripts.migrate`);
and a multi-stage **non-root** image with compose (`--profile agents`) + example
k8s manifests.

**Open:** real ERP adapters (injectable + stubbed today); a hashed dependency
lockfile; migrating n8n's `redisTrigger` workflows off pub/sub so `emit()` can
drop its dual `publish()`.

Full documentation: **[docs/](docs/README.md)** — architecture, security,
operations runbook, incident response, disaster recovery, and ADRs.
