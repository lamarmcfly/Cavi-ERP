# Deploying Cavi ERP on Kubernetes

`cavi-erp.example.yaml` is a **starting point**, not a production Helm chart. It
shows the shape: config in a ConfigMap, secrets in a Secret, a one-shot migration
Job, the Vault service, and one bus agent (ledger) with health probes.

## Prereqs
- A built image pushed to a registry your cluster can pull; set `image:` to it.
- Postgres + Redis reachable at the hosts in the ConfigMap (managed services, or
  add StatefulSets — not included here).

## Apply
```bash
# 1. Edit the Secret (never commit real values) and image references.
kubectl apply -f deploy/k8s/cavi-erp.example.yaml
# 2. The migrate Job runs scripts.migrate + scripts.bootstrap once.
kubectl -n cavi-erp wait --for=condition=complete job/cavi-erp-migrate
```

## Add the other agents
Copy the `ledger` Deployment block, change `metadata.name`, the `app:` label, and
the `command` (`agents.forge.agent`, `agents.ticker.agent`, `agents.mapper.agent`,
`agents.beacon.agent`). Each exposes `/healthz` `/readyz` `/metrics` on
`CAVI_HEALTH_PORT` via `run_agent`.

## Probes
- **Liveness** → `/healthz` (process up).
- **Readiness** → `/readyz` (dependencies reachable; returns 503 when not).
- **Metrics** → `/metrics` (Prometheus text) for a scrape annotation / ServiceMonitor.

## Follow-ups (not in this example)
Postgres/Redis StatefulSets or managed-service wiring, a real Helm chart with
values, HPA, NetworkPolicies, and a PodSecurity context beyond `runAsNonRoot`.
