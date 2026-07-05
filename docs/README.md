# Cavi ERP — documentation

Operational + design docs for the event-sourced ERP-integration platform.

| Doc | What it covers |
|---|---|
| [ARCHITECTURE.md](ARCHITECTURE.md) | The six agents, the bus, the durable event log, the schema registry, and how an event flows end to end. |
| [SECURITY.md](SECURITY.md) | Credential handling (Vault), the fail-closed auth model, HMAC webhook verification, tenant isolation, secrets. |
| [OPERATIONS.md](OPERATIONS.md) | Deploy, migrate, bootstrap, health/metrics, and the day-to-day runbook (incl. the local-Postgres-on-5432 gotcha). |
| [INCIDENT_RESPONSE.md](INCIDENT_RESPONSE.md) | On-call playbook: dead-letter storms, a dependency down, and how to replay a quarantined event. |
| [DISASTER_RECOVERY.md](DISASTER_RECOVERY.md) | Backup/restore of the durable stores, RPO/RTO posture, rebuild order. |
| [adr/](adr/) | Architecture Decision Records — why the load-bearing choices were made. |

Repo-level docs: top-level [`README.md`](../README.md),
[`schema_registry/README.md`](../schema_registry/README.md),
[`middleware/n8n/README.md`](../middleware/n8n/README.md),
[`deploy/k8s/README.md`](../deploy/k8s/README.md).
