# Schema Registry

The contract layer of Cavi ERP. Every event that moves between agents is
validated against a versioned JSON Schema kept here.

## Layout

```
schema_registry/
├── schemas/        # <subject>.v<version>.json — the live contracts
└── migrations/     # SQL that mirrors the registry + event log into Postgres
```

## Naming

`<subject>.v<version>.json`, e.g. `ledger.entry.v1.json`. The pair
(`subject`, `version`) is the lookup key used by `agents/base/registry.py`
and the `schema_registry` table.

## Evolving a schema

1. Copy `foo.v1.json` to `foo.v2.json` and make your change.
2. Prefer **backward-compatible** changes (add optional fields). For breaking
   changes, register the new version and route old producers through **Mapper**
   to coerce v1 → v2.
3. Load the new schema into Postgres so the `event_log` foreign key accepts it.

## Why both files and a table?

The JSON files are the source of truth agents read at runtime (fast, local,
versioned in git). The `schema_registry` table lets Postgres enforce
referential integrity on the durable `event_log`. They are kept in sync by the
bootstrap script.
