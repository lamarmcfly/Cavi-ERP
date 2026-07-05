# n8n Middleware

n8n is the routing and orchestration layer that sits between the six agents.
Agents publish `Event` envelopes onto Redis pub/sub; n8n workflows subscribe,
apply routing/branching/retry logic, and trigger downstream agents or external
systems (email, webhooks, third-party ERPs).

> **Note.** Agent-to-agent delivery moved to Redis Streams + consumer groups for
> at-least-once processing (see `docs/adr/0003-bus-durability.md`), but `emit()`
> still `publish()`es to the same pub/sub channel because n8n's `redisTrigger`
> node reads pub/sub, not consumer groups. These workflows are unaffected. If
> n8n later consumes Streams directly, the dual publish can be dropped.

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
| `netsuite-sync.json` | `ledger.posted` | Pushes a posted journal entry to NetSuite, getting the OAuth 1.0a `Authorization` header from Vault's `/sign` first (secrets never leave Vault) | HTTP → Vault `/sign` → HTTP → NetSuite REST |

### Required credentials / env

* **Redis** credential named `Cavi Redis` (host/port from `docker-compose.yml`).
* `HERMES_WEBHOOK_URL` — gateway webhook for `deadletter-escalation`.
* `VAULT_URL`, `NETSUITE_REST_URL` — endpoints for `netsuite-sync`. Run the
  Vault service with `python -m agents.vault.service` (default `:8080`); it
  exposes `POST /sign`, `POST /vend`, and `GET /healthz`. From the n8n
  container, set `VAULT_URL=http://host.docker.internal:8080`.

### Design note — where revenue recognition lives

`sale-to-posting` derives the `ledger.entry` **in n8n**, which fully decouples
Forge from accounting (Forge would emit only `forge.completed`). The Python
`ForgeAgent` currently *also* derives the entry itself. Pick one home for that
logic — keeping both means two places to change the chart of accounts. The n8n
location is preferable when non-engineers tune the mapping; the agent location
when it must be unit-tested and versioned with code.

### Importing

```bash
# in the n8n UI: Workflows → Import from File → pick a .json
# or via CLI inside the container:
docker compose exec n8n n8n import:workflow --input=/workflows/sale-to-posting.json
```
