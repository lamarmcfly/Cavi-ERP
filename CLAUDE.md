# Cavi-ERP

Event-driven ERP platform with a **gated write-back pipeline** to an external
ERP (NetSuite). Python, agent-per-process, routed by n8n, contracted by a
Postgres schema registry, Redis as cache + bus. Separate from **cavi-core**
(the Next.js agency-ops platform): cavi-core's own inventory adapter is
read-only and must stay that way — this repo is the only place ERP writes
live.

## Read first

- `README.md` — architecture, the six agents, the write-back lifecycle.
- `docs/PRD-writeback.md` — the write-back PRD / implementation log (W1–W10
  shipped; sandbox validation + partner gates remain). Subordinate to code.
- `middleware/n8n/README.md` — how routing and the approval workflow work.
- `schema_registry/README.md` — event contracts and the audit hash chain.

## Hard rules (non-negotiable)

1. **Never bypass the approval gate.** Nothing reaches the ERP without a
   `forge.write.approved` event. `WriteCoordinator.execute()` in
   `agents/forge/write.py` guards on APPROVED — do not add a path around it,
   and n8n workflows may only *propose* writes (`forge.write.propose`).
2. **Never fabricate a dry-run diff.** Diffs are computed from the record's
   current ERP state; a failed fetch dead-letters, it does not guess.
3. **Never break the audit chain.** `event_log` is hash-chained
   (`shared/audit.py`, migration 0004). No edits, deletes, or reordering of
   chained rows; corrections are reversals (`request_reversal`), which are new
   gated writes.
4. **Fail closed.** Missing Vault/NetSuite config means approved writes refuse
   to post. Do not add fallbacks that degrade to unsigned or unauthenticated
   calls. Raw ERP credentials live only in the Vault service
   (`agents/vault/service.py`); agents get Vault-signed requests, never keys.
5. **Every schema change is a new version** (`schema_registry/schemas/`),
   loaded via migration/bootstrap; breaking changes route old producers
   through Mapper. Never mutate an existing `<subject>.v<n>.json`.

## Verify before pushing (mirrors CI — `.github/workflows/ci.yml`)

```bash
ruff check .
mypy
pytest
```

## Entrypoints

`python -m` any of: `agents.vault.service` (credential HTTP service, :8088),
`agents.vault.agent`, `agents.ledger.agent`, `agents.ledger.query_agent`,
`agents.ledger.reconcile_agent`, `agents.forge.agent`,
`agents.forge.write_agent` (ERP write-back), `agents.ticker.agent`,
`agents.ticker.webhook_agent`, `agents.mapper.agent`,
`agents.mapper.erp_agent`, `agents.beacon.agent`, `agents.beacon.report_agent`.

Compose: `docker compose up -d` (infra + Vault + migrations),
`docker compose --profile agents up -d` (the fleet). Migrations:
`python -m scripts.migrate`. Audit verify:
`python -m scripts.audit_export --verify`.

## Branch / PR workflow

- Branch off `main` as `claude/<topic>`; never push to `main`.
- CI gate: ruff + mypy + pytest must pass.
- Keep `docs/PRD-writeback.md` reconciled when write-back behavior changes —
  the code wins on disagreement, and the PRD gets fixed in the same PR.
