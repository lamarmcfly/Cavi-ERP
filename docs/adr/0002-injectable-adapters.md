# ADR 0002 — Injectable adapters (dependency injection over ambient I/O)

**Status:** accepted

## Context
The agents touch Postgres, Redis, an OS keyring, and external ERPs. If those are
reached through module-level globals, the domain logic can't run without live
infrastructure, and CI would need a full stack to test anything.

## Decision
Every external dependency is a small Protocol injected into the domain object,
with a real implementation and an in-memory/stub one:
`EventStore`, `LedgerStore`, `DedupStore`, `ErpReader`, `ErpWriter`,
`CredentialStore`, and the bus/registry on `BaseAgent`. Production defaults
connect lazily; tests pass a stub. Pure domain logic and payload builders are
kept separate from the thin agent runtime.

## Consequences
- The whole test suite runs with **no Postgres/Redis** — fast, deterministic,
  and it covers the risky paths (idempotency races, dead-lettering, tenant
  isolation) via in-memory analogs of the real guards.
- Defaults that require infra (e.g. `UnconfiguredErpReader`/`Writer`) **fail
  loudly** rather than silently no-op, so a misconfigured deploy is obvious.
- A little more indirection; worth it for testability + swappable backends.
