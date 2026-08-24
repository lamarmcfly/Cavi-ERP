# Cavi ERP

An event-driven ERP platform built from cooperating agents, routed by **n8n**
middleware, contracted by a **PostgreSQL** schema registry, and accelerated by
a **Redis** cache + event bus. It writes to an external ERP (NetSuite) only
through a **gated write-back pipeline**: propose → computed dry-run diff →
human approval → idempotent, Vault-signed commit → hash-chained audit trail,
with reversals and a circuit breaker.

Agents never call each other directly. Each emits versioned `Event` envelopes
onto the bus; n8n workflows route them; every event is validated against the
schema registry before it is handled. This keeps the agents independently
deployable and the contracts in one auditable place.

## The six agents

| Agent      | Role |
|------------|------|
| **Vault**  | Custodian of secrets & sensitive master data (credentials, vendor bank details, PII). Brokers reference tokens instead of raw secrets; runs as its own HTTP service (`/vend`, `/sign`) so raw ERP credentials never leave it. |
| **Ledger** | Double-entry accounting core and financial system of record. Postings must never be silently lost. Also runs scheduled Cavi-vs-ERP reconciliation and emits advisory drift events (never auto-corrects). |
| **Forge**  | Production & order fulfillment. Turns demand into work orders and tracks them to completion. Also owns the ERP write-back lifecycle: dry-run diff → approval gate → idempotent, Vault-signed commit, with a per-tenant circuit breaker. |
| **Ticker** | Time, pricing & scheduling. Real-time price/FX snapshots (cached), scheduled events like period close, inbound ERP webhook ingestion, and the reconciliation cadence (`ticker.reconciliation.due`). |
| **Mapper** | Anti-corruption layer. Translates between schema versions and foreign external formats. Holds the Cavi→NetSuite record mapping tables (`agents/mapper/netsuite_records.py`) — missing required fields block with every gap surfaced at once; undeclared fields block rather than silently drop. |
| **Beacon** | Notifications, alerting & observability. Natural sink for dead-lettered events; surfaces drift advisories and breaker trips. Audit export lives in `scripts/audit_export.py`. |

## Architecture

```
                    ┌──────────────────────────────────────┐
                    │          n8n middleware              │
                    │   (routes & orchestrates events)     │
                    └───────────────▲──────────────────────┘
                                    │  subscribe / trigger
            publish Event envelopes │
   ┌───────┬───────┬───────┬───────┴─┬───────┬───────┐
   │ Vault │ Ledger│ Forge │ Ticker  │ Mapper│ Beacon│   ← the agent fleet
   └───┬───┴───┬───┴───┬───┴────┬────┴───┬───┴───┬───┘
       │       │       │        │        │       │
       │       │       │ Forge write path only   │
       │       │       ▼        │        │       │
       │   ┌────────────────┐   │        │       │
       │   │ NetSuite (ext. │◄──┼── Vault-signed requests
       │   │ ERP, gated)    │   │   (Vault HTTP service :8088,
       │   └────────────────┘   │    /vend + /sign)
       └───────┴───────┴────┬───┴────────┴───────┘
                            │
              ┌─────────────┴─────────────┐
              │   Redis (cache + bus)     │
              └─────────────┬─────────────┘
                            │ durable record + contract enforcement
              ┌─────────────┴─────────────┐
              │  PostgreSQL               │
              │  • schema_registry        │
              │  • event_log (hash-chained│
              │    seq + chain_hash)      │
              │  • event_deadletter       │
              │  • ledger + tenant tables │
              └───────────────────────────┘
```

## ERP write-back (gated)

Nothing reaches the external ERP without an explicit human approval event.
The full lifecycle (all shipped — W1–W10, see `docs/PRD-writeback.md`):

1. **Propose** — a write is requested (`forge.write.requested`), never executed
   directly. The `netsuite-sync` n8n workflow is propose-only.
2. **Dry-run diff** — Forge fetches the current ERP record and computes a real
   before/after diff (`agents/forge/write.py`); a failed fetch dead-letters
   rather than fabricating a diff.
3. **Approval gate** — `execute()` refuses anything not in the APPROVED state
   (`forge.write.approved` / `forge.write.rejected`); batches guard the whole
   batch and halt on first failure.
4. **Idempotent commit** — every write carries an idempotency key
   (`cavi-{write_id}`) sent to the ERP; the NetSuite client
   (`agents/forge/netsuite.py`) signs requests via the Vault service so raw
   credentials never touch agent processes.
5. **Audit + reversal** — every event lands in the hash-chained `event_log`
   (`shared/audit.py`, migration `0004_audit_chain.sql`); committed writes can
   be reversed (`request_reversal`), never edited in place.
6. **Circuit breaker + metrics** — repeated ERP failures open a per-tenant
   breaker (`agents/forge/breaker.py`); Prometheus metrics in
   `shared/metrics.py` (`cavi_erp_writes_total`, `cavi_erp_breaker_opens_total`,
   `cavi_erp_drift_total`, …).

### How a write-back flows

```
ledger.posted ──► forge.write.propose ──► forge.write.requested (with dry-run diff)
                                              │
                                   human decision (n8n approval)
                                              │
                    forge.write.approved ─────┴──── forge.write.rejected
                              │
              Vault-signed NetSuite upsert (idempotency key)
                              │
                    forge.write.completed  →  hash-chained audit row
```

Reconciliation runs on the `ticker.reconciliation.due` cadence: Ledger compares
Cavi state against the ERP over managed fields only and emits
`ledger.reconciliation.completed` / `ledger.drift.detected` — advisory only,
surfaced by Beacon as warnings; nothing auto-corrects.

## Repository layout

```
cavi-erp/
├── agents/
│   ├── base/               # BaseAgent runtime, Event contract, SchemaRegistry client
│   ├── vault/              # agent + HTTP credential service (/vend, /sign)
│   ├── ledger/             # agent + query_agent + reconcile_agent
│   ├── forge/              # agent + write_agent, netsuite.py, write.py, breaker.py
│   ├── ticker/             # agent + webhook_agent (inbound ERP webhooks)
│   ├── mapper/             # agent + erp_agent, netsuite_records.py mappings
│   └── beacon/             # agent + report_agent
├── middleware/n8n/         # workflow JSON + docs (the router; ships inactive)
├── schema_registry/        # 24 versioned JSON Schemas + SQL migrations 0001–0004
├── cache/redis/            # redis.conf
├── shared/                 # settings, db, cache, audit chain, logging, metrics, health
├── scripts/                # bootstrap.py, migrate.py (tracked runner + rollback),
│                           # audit_export.py (--verify tamper check)
├── tests/                  # unit + contract suite (29 files, no infra required),
│                           # incl. write-back, audit-chain, breaker, reconciliation
├── docs/PRD-writeback.md   # write-back PRD / implementation log
├── deploy/k8s/             # example manifests + README
├── .github/workflows/ci.yml# ruff + mypy + pytest on every PR
├── Dockerfile              # multi-stage, non-root runtime image
├── docker-compose.yml      # postgres + redis + migrate + vault + n8n (+ agents profile)
├── pyproject.toml          # ruff / mypy / pytest config
├── requirements.txt / requirements-dev.txt
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
5. From `ledger.posted`, the gated write-back path above can propose the
   corresponding ERP write — but only a human approval lets it commit.

The `correlation_id` on every envelope ties all of these together so one sale is
traceable end to end.

## Getting started

```bash
# 1. Install deps
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt

# 2. Configure
cp .env.example .env        # edit secrets — see "Environment" below

# 3. Bring up infrastructure (Postgres + Redis + migrations + Vault + n8n)
docker compose up -d
# ...and the agent fleet itself:
docker compose --profile agents up -d

# 4. Or run migrations + schema bootstrap by hand
python -m scripts.migrate
python -m scripts.bootstrap

# 5. Run an agent (each is its own process). Entrypoints:
python -m agents.ledger.agent            # also: ledger.query_agent, ledger.reconcile_agent
python -m agents.forge.agent             # also: forge.write_agent (ERP write-back)
python -m agents.ticker.agent            # also: ticker.webhook_agent
python -m agents.mapper.agent            # also: mapper.erp_agent
python -m agents.beacon.agent            # also: beacon.report_agent
python -m agents.vault.service           # credential HTTP service (:8088)

# 6. Verify (the same gate CI runs)
ruff check .
mypy
pytest

# 7. Verify the audit chain end to end
python -m scripts.audit_export --verify   # exits non-zero on tamper
```

Container/K8s: `Dockerfile` builds a multi-stage, non-root image;
`deploy/k8s/` has example manifests (ConfigMap/Secret, migrate Job, Vault,
agent Deployments with `/healthz` `/readyz` `/metrics` probes).

## Environment

`.env.example` is the contract. The write path is **fail-closed** — with these
unset, approved writes refuse to post rather than degrade:

| Variable | Purpose |
|---|---|
| `CAVI_VAULT_API_SECRET` | Auth for the Vault HTTP service; empty ⇒ Vault refuses to sign |
| `CAVI_VAULT_TENANT_ALLOWLIST` | Tenants allowed to vend/sign credentials |
| `CAVI_VAULT_URL` | Where agents reach the Vault service |
| `CAVI_NETSUITE_REST_URL` | Target ERP REST endpoint for the Forge writer |
| `CAVI_WEBHOOK_SIGNING_SECRET` | Verifies inbound ERP webhooks (Ticker) |

## Design notes

* **Schema registry as the backbone.** The `(subject, version)` pair is the
  single contract. Producers and consumers evolve independently as long as both
  honor a registered schema. See `schema_registry/README.md`.
* **Two stores, two jobs.** Redis is fast + ephemeral (cache + pub/sub);
  Postgres is durable + authoritative (`event_log` is the source of truth).
* **Mapper is the escape hatch** for breaking schema changes — route old
  producers through it to coerce v1 → v2 rather than a flag-day migration
  (`forge.write.requested` v1→v3 is the canonical worked example).
* **Nothing reaches the ERP without an approval event.** The approval gate in
  `agents/forge/write.py` is load-bearing; there is no configuration that
  bypasses it.
* **The audit trail is tamper-evident.** `event_log` rows are hash-chained
  (`shared/audit.py`) and independently verifiable offline via
  `scripts/audit_export.py --verify`.

## Status

Working platform. The gated ERP write-back stack (W1–W10: approval gate,
ERP-side idempotency, NetSuite writer, computed dry-run diffs, partner record
mappings, reconciliation/drift detection, hash-chained audit, reversals,
circuit breaker, write metrics) shipped in PR #20, on top of the earlier
hardening waves (CI gate, fail-closed Vault credential auth, durable
event persistence, idempotent + pooled ledger writes, multi-tenant isolation,
reliability/dedup, observability, tracked migrations, production packaging).

Remaining before this is sellable as live: **sandbox validation against a live
NetSuite account, partner-gate sign-off (see `docs/PRD-writeback.md` §6), and a
provisioned tenant.** No live tenant exists yet, and n8n workflows ship
inactive by design.
