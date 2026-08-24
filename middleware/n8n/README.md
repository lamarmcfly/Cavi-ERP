# n8n Middleware

n8n is the routing and orchestration layer that sits between the agent fleet.
Agents publish `Event` envelopes onto Redis pub/sub; n8n workflows subscribe,
apply routing/branching/retry logic, and trigger downstream agents or external
systems (email, webhooks, third-party ERPs).

## Why a middleware instead of direct agent-to-agent calls?

* **Decoupling** — an agent only needs to know the *subject* it emits, not who
  consumes it. Add a consumer by adding a workflow, no code change.
* **Visibility** — n8n gives a visual audit trail of how a business event
  fanned out across the system.
* **Glue logic without redeploys** — conditional routing, rate limits, and
  retries live in workflows you can edit without shipping an agent.

## Layout

```
middleware/n8n/
└── workflows/   # exported workflow JSON, version-controlled
```

## Conventions

* One workflow per business flow (e.g. `sale-to-posting.json`:
  Forge → Ledger → Beacon).
* Workflows reference events by `subject` + `schema_version` so they break
  loudly if a contract changes — pair breaking changes with a Mapper step.
* Export workflows to `workflows/` and commit them; they are mounted read-only
  into the container by `docker-compose.yml`.

## Workflows in this directory

Each subscribes to a Redis channel (the event `subject`) and either re-publishes
a derived event or calls an external system. All ship **inactive** — review
credentials/env, then toggle Active in the n8n UI.

| Workflow | Trigger (subscribe) | Does | Output |
|----------|--------------------|------|--------|
| `sale-to-posting.json` | `forge.completed` | Revenue recognition — derives a balanced `ledger.entry` from a completed work order | publishes `ledger.entry` |
| `deadletter-escalation.json` | `deadletter.ledger.entry` | Escalates an unparseable financial event (CRITICAL) to a human | HTTP → Hermes gateway (Telegram) |
| `netsuite-sync.json` | `ledger.posted` | Files a **proposal** to write a posted journal entry into NetSuite — never posts directly. Turns the posting into a `forge.write.propose` that the Forge write agent holds for human approval | publishes `forge.write.propose` |
| `reconciliation-cadence.json` | schedule (hourly) | Ticker's clock for drift detection: tells Ledger to compare what Cavi believes the ERP holds against what it actually holds. The interval is the drift-detection window (PRD NFR4) — tune it with the design partner | publishes `ticker.reconciliation.due` |

### Required credentials / env

* **Redis** credential named `Cavi Redis` (host/port from `docker-compose.yml`).
* `HERMES_WEBHOOK_URL` — gateway webhook for `deadletter-escalation`.
* `netsuite-sync` no longer calls Vault or NetSuite itself — it only publishes
  a `forge.write.propose`. The Vault-signed NetSuite call now lives inside the
  Forge write agent's ERP writer, which runs only after approval. `VAULT_URL`
  and `NETSUITE_REST_URL` are configured for that agent, not this workflow.

### Design note — where revenue recognition lives

`sale-to-posting` derives the `ledger.entry` **in n8n**, which fully decouples
Forge from accounting (Forge would emit only `forge.completed`). The Python
`ForgeAgent` currently *also* derives the entry itself. Pick one home for that
logic — keeping both means two places to change the chart of accounts. The n8n
location is preferable when non-engineers tune the mapping; the agent location
when it must be unit-tested and versioned with code.

### Design note — the ERP write approval gate

Writing into an external system of record is the highest-risk action in the
product, so `netsuite-sync` is deliberately only the **first** step of a gated
pipeline, not a sync:

```
ledger.posted ──(n8n)──▶ forge.write.propose
                              │
                         Forge records it as forge.write.requested (pending)
                              │
             human reviews the diff_preview in Mission Control
                              │
                   forge.write.decision {approve|reject}
                              │
        approve ─▶ Forge executes via its Vault-signed, idempotency-keyed
                   ERP writer ─▶ forge.write.completed (carries NetSuite receipt)
```

No journal entry reaches NetSuite without a `forge.write.decision` approving it,
and every attempt carries a stable idempotency key so an approved-then-retried
write cannot double-post: the writer upserts by external id
(`PUT /record/v1/{module}/eid:{key}`), so NetSuite itself dedupes retries. The
same client also serves the dry-run — the `diff_preview` a reviewer approves is
computed against the record's *current* ERP state, never asserted by the
proposer. When `VAULT_URL` / `CAVI_VAULT_API_SECRET` / `NETSUITE_REST_URL` are
not configured on the Forge write agent, an approved write fails **closed**
(the writer refuses to run) rather than posting silently.

Undoing a committed write is the same shape: `forge.write.reverse` proposes a
compensating write linked to the completed original (`reverses` on the v2
requested event), and that reversal needs its own dry-run and human approval
before it executes — both actions end up on the hash-chained audit log. A
circuit breaker guards execution: repeated ERP failures trip it once, loudly
(dead-letter → Beacon), and approved writes wait — retryable, never dropped —
until the ERP recovers.

### Importing

```bash
# in the n8n UI: Workflows → Import from File → pick a .json
# or via CLI inside the container:
docker compose exec n8n n8n import:workflow --input=/workflows/sale-to-posting.json
```
