# Operations runbook

## Local / dev bring-up
```bash
cp .env.example .env          # set a real CAVI_POSTGRES_PASSWORD + CAVI_VAULT_API_SECRET
docker compose up -d          # infra + Vault + one-shot migrate/bootstrap
docker compose --profile agents up -d   # + the six bus agents
```
The `migrate` service runs `scripts.migrate` then `scripts.bootstrap` once, then
exits; agents wait for it to complete.

## Migrations
```bash
python -m scripts.migrate            # apply all pending (tracked in schema_migrations)
python -m scripts.migrate --status   # applied vs pending
python -m scripts.migrate --rollback # revert the most recent (needs a .down.sql)
```
Migrations live in `schema_registry/migrations/` as `NNNN_name.sql` (+ optional
`NNNN_name.down.sql`). Each applies in its own transaction; the runner is
idempotent.

## Schema registry
```bash
python -m scripts.bootstrap          # upsert every schema_registry/schemas/*.json
```

> **Gotcha (local):** a local `postgres` install often already listens on 5432,
> shadowing the container, so host-run tooling fails with `role "cavi" does not
> exist`. Run through the container instead:
> `docker compose run --rm -e CAVI_POSTGRES_HOST=postgres migrate python -m scripts.bootstrap`,
> or remap the container to 5433 (`CAVI_POSTGRES_PORT=5433`).

## Health & metrics
Each agent (when `CAVI_HEALTH_PORT` is set) serves:
- `GET /healthz` — liveness.
- `GET /readyz` — readiness (503 if a dependency check fails).
- `GET /metrics` — Prometheus text: `cavi_events_emitted_total`,
  `cavi_events_dispatched_total`, `cavi_deadletters_total` (labeled by
  `agent`+`subject`).

Vault serves `/healthz` on its service port (8080 in-network, 8088 on host).

## Logs
JSON, one object per line, with `correlation_id` / `tenant_id` / `agent` /
`subject` — query by `correlation_id` to trace an event across agents.

## CI / verify
```bash
pip install -r requirements-dev.txt
ruff check . && mypy && pytest      # the same gate CI runs
```
Note: CI runs the bare `pytest` console script; `pythonpath=["."]` in
`pyproject.toml` makes `agents`/`shared` importable there (don't rely on
`python -m pytest` as a proxy).

## Verify a flow end to end
`docker compose --profile agents up -d`, publish a `forge.workorder` (or drive an
agent), then check: an `event_log` row was written, a `ledger.posted` followed,
and — for a malformed event — a Beacon alert reached Hermes.
