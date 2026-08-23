# Cavi-ERP Write-Back PRD

**Product:** Cavi-ERP (this repository, `lamarmcfly/Cavi-ERP`)
**Scope:** system-of-record write-back to an external ERP (initial target: NetSuite)
**Author:** Lamar Martin, GrayMar Strategies LLC
**Status:** Draft, reconciled against the codebase as of this commit
**Relationship to cavi-core:** distinct from the read-only inventory adapter in cavi-core. Cavi-ERP is the write-capable platform. Phase 1 of the design-partner engagement (live operations pilot: inventory, POS sync, front office) runs entirely on cavi-core and is out of scope here. This PRD covers Phase 2 only.

> This PRD stays subordinate to the code. If it disagrees with the code, the code wins, and the PRD gets fixed. Section 4 records what the code already implements so the epics describe the real remaining work, not a from-scratch build.

---

## 1. Goals and background context

**Goals**
- Let a supervised human commit Cavi-managed operational data into an external ERP as the system of record
- Preserve every Cavi governance guarantee for writes: human approval, fail-closed defaults, full audit, no silent changes
- Ship a generalized platform, with the design partner's use case as the first reference implementation, not a one-off custom fork

**Background**
- Cavi-core already reads inventory and syncs POS. It is not an ERP.
- Design partner intent: become system of record over time. This PRD covers the write path that makes that possible.
- The first reference implementation is a supplement retailer: lot- and expiry-tracked inventory, branch transfers, physical-count adjustments. This sets the priority order for schema mapping (Epic 2), not the boundary of the platform.
- Write-back into someone's books is the highest-risk action in the product. The central design principle is that a write is never trusted by default and never irreversible without a record.
- The engagement timeline is governed by the milestone gates in Section 6, not a go-live date. No epic's output is trusted downstream until its gate passes. This is the same gate mechanism referenced in the partner proposal.

**Non-goals (this phase)**
- General availability. This is a supervised design-partner phase.
- Auto-committing writes without human approval.
- ERP targets beyond the initial one. [Confirm target: NetSuite assumed.]
- Replacing the cavi-core read-only adapter.

---

## 2. Requirements

### Functional
- FR1: Broker and store ERP credentials per tenant with isolation and rotation (Vault).
- FR2: Map Cavi entities to the target ERP schema, including custom fields (Mapper). A missing required field blocks the write; no silent drops.
- FR3: Generate a dry-run preview of any proposed write, showing a before-and-after diff computed from current ERP state, before anything commits (Forge).
- FR4: Require explicit human approval on every write through the write lifecycle gate before commit. In this repository the gate is the `forge.write` state machine (see Section 5, "Approval gate"); the cavi-core autonomy gate (Marlo) is the policy/UI layer that produces the approval.
- FR5: Commit writes idempotently, so a retry never double-posts. The idempotency key must travel to the ERP itself (NetSuite external id / idempotency key), not only dedupe inbound events.
- FR6: Reconcile Cavi state against ERP state on a schedule and surface drift (Ledger reconciles, Ticker schedules, Beacon surfaces).
- FR7: Record every write in a hash-chained, exportable audit log (event_log + chaining; Beacon exports).
- FR8: Support reversal or compensating actions for a committed write. A reversal is itself a `forge.write` lifecycle with its own approval and audit trail.
- FR9: Fail closed. Any ambiguity, auth failure, or partial write halts and escalates to a human rather than guessing.
- FR10: [Design-partner-specific requirement.]

### Non-functional
- NFR1: Security posture on a SOC 2 Type II trajectory; credential brokering is the highest-sensitivity surface.
- NFR2: Idempotency and exactly-once commit semantics under retry and partial failure.
- NFR3: Multi-tenant isolation (RLS-consistent with cavi-core; enforced today via NOT NULL `tenant_id` on the books and tenant-scoped audit queries).
- NFR4: Reconciliation freshness target. [Define acceptable drift-detection window.]
- NFR5: Model and vendor agnostic. Do not hard-couple to a named AI vendor; do not name vendors in customer-facing surfaces.
- NFR6: Observability on every write path (latency, failure, retry, drift counts).
- NFR7: [Availability expectation. No contractual uptime SLA is offered at pilot stage.]

---

## 3. Technical assumptions and constraints

- Event-sourced architecture: agents never call each other directly; each emits versioned `Event` envelopes onto the Redis bus, n8n routes them, and every event validates against the Postgres schema registry before it is handled. Writes are events, enabling replay, audit, and compensation.
- Governance carries over from cavi-core: GREEN / YELLOW / RED autonomy zones, human approval gate, auditable log. No write commits without a human-signed approval, consistent with the Cavi "human in command" boundary.
- [Deployment model: GrayMar-hosted vs client-tenant OAuth. Decide with partner.]
- [Target ERP API constraints: NetSuite SuiteTalk / REST record limits, governance units, rate limits. Fill during scoping.]

### Agent roster (as the code defines it)

The roster below matches `README.md` and `agents/`. Where this PRD adds a duty, it is listed as an addition, not a redefinition.

| Agent | Role in code | Duty added by this PRD |
|---|---|---|
| **Vault** | Custodian of secrets and sensitive master data; brokers reference tokens; HTTP `/vend` and `/sign` surface | ERP credential brokering for write-back (largely built) |
| **Ledger** | Double-entry accounting core; financial system of record | Reconciliation of Cavi state vs ERP state (Epic 4) |
| **Forge** | Production and order fulfillment; owns the ERP write lifecycle state machine | Real dry-run diffs; NetSuite writer with idempotency keys |
| **Ticker** | Time, pricing, and scheduling | Drift-check cadence (Epic 4) |
| **Mapper** | Anti-corruption layer; version coercion and cross-ERP transforms | Partner record-type mapping tables (Epic 2) |
| **Beacon** | Notifications, alerting, observability; dead-letter sink | Audit export; drift surfacing; write-path dashboards |

### Approval gate (Marlo boundary)

Marlo is a cavi-core concept and does not exist in this repository. The enforceable gate here is the `forge.write` state machine (`agents/forge/write.py`): a write can only execute from APPROVED, and REJECTED/COMPLETED are terminal. The integration contract with cavi-core is therefore: **an approval is a `forge.write.approved` event carrying the `write_id`, emitted only on behalf of an authorized human reviewer.** Mission Control / Marlo is the surface where that human acts; this platform never trusts anything but the event.

---

## 4. Current state: what the code already implements

Audit of the repository at the time of this PRD. Epics in Section 6 build on this; they do not restate it as new work.

**Built and tested**
- `forge.write` lifecycle state machine with the approval-gate and decide-once invariants; four registered event schemas (`forge.write.requested/approved/rejected/completed`); a failed ERP call leaves the write APPROVED (retryable), never falsely COMPLETED. Default `UnconfiguredErpWriter` fails loudly.
- Vault: per-tenant credential store with rotate/revoke; HTTP service where `/vend` returns only non-secret metadata and `/sign` produces request-specific OAuth 1.0a headers — raw secrets never leave Vault.
- Mapper: `(source_schema, target_schema)` ERP transforms with stable `input_hash` fingerprints; unregistered mappings surface as explicit `mapper.transform.failed` events.
- Ledger: balanced double-entry postings with inbound `entry_id` idempotency; tenant isolation enforced (NOT NULL `tenant_id`, migration 0003).
- Durable `event_log` (source of truth) and `event_deadletter` with Beacon as the dead-letter sink; fleet-wide Redis-backed alert dedup; structured JSON logs, metrics, and health endpoints; tracked migrations with rollback; hardened packaging (non-root image, compose, k8s).
- n8n routing with a working `netsuite-sync` workflow: Vault-signed POST to NetSuite REST.

**Known gaps this PRD exists to close**
1. **The live `netsuite-sync` workflow bypasses the approval gate.** It triggers on `ledger.posted` and posts directly to NetSuite — no `forge.write` lifecycle, no human approval, no idempotency key. This contradicts FR4/FR5 and is the first thing to fix.
2. **No ERP-side idempotency.** `ErpWriter.apply` sends no idempotency key; a timeout-then-retry where the ERP actually committed can double-post today.
3. **Dry-run is a free-form string.** `diff_preview` is supplied by the requester; nothing computes a real before/after from ERP state.
4. **`event_log` is durable but not hash-chained.** Auditable, not yet tamper-evident.
5. **No reconciliation, drift detection, reversal flow, circuit breaker, or partial-batch halt.** Greenfield.

---

## 5. Implementation plan (ordered)

Order is by risk: governance-critical fixes to paths that exist today come first.

- **W1 — Route ERP writes through the approval gate** (Epic 3 / FR4). Rework `netsuite-sync`: `ledger.posted` produces a `forge.write.requested` (with diff preview), execution happens only on `forge.write.approved`, and the write completes via the Vault-signed call, emitting `forge.write.completed`. Retire the direct post path.
- **W2 — ERP-side idempotency keys** (Epic 3 / FR5). Carry `write_id` to NetSuite as the external id / idempotency key in the writer; prove no double-post under forced retry.
- **W3 — Real NetSuite `ErpWriter`** (Epics 1, 3). Production writer replacing `UnconfiguredErpWriter`: Vault `/sign` integration, NetSuite REST, governance-unit and rate-limit aware, fail-closed on auth errors.
- **W4 — True dry-run diffs** (Epic 3 / FR3). Fetch current ERP record state, compute before/after, attach a structured diff to `forge.write.requested`; free-form `diff_preview` becomes derived, not asserted.
- **W5 — Partner record-type mappings** (Epic 2 / FR2). Mapping tables for the reference implementation, in priority order: lot-numbered inventory item, inventory adjustment, transfer order; missing required ERP fields block the write with a surfaced gap.
- **W6 — Reconciliation and drift** (Epic 4 / FR6). Scheduled (Ticker) comparison of Cavi state vs ERP state (Ledger), advisory-only drift flags surfaced by Beacon — a human disposes, never an automatic correction.
- **W7 — Hash-chained audit log** (Epic 5 / FR7). `prev_hash` chaining on `event_log` writes plus an export and a chain-verification tool.
- **W8 — Reversal / compensating writes** (Epic 5 / FR8). Reversal modeled as a new `forge.write` lifecycle referencing the original `write_id`; both actions on the chain.
- **W9 — Circuit breaker and partial-batch halt** (Epic 6 / FR9). Repeated failures trip a breaker and escalate; a partial batch halts, escalates, and leaves no unreported half-posted state.
- **W10 — Write-path observability** (Epic 5 / NFR6). Extend the existing metrics/health layer with write latency, failure, retry, and drift counts; dashboard for write health.

---

## 6. Epic list and gates

No epic is considered done, and no downstream epic starts trusting its output, until the gate criteria pass. Gates are the timeline mechanism referenced in the partner proposal.

### Epic 1: Secure ERP connection and credential brokering (Vault) — largely built
- Story 1.1: Store per-tenant ERP credentials with isolation. Acceptance: credentials are never readable across tenants; rotation is supported; access is logged. *(built; access-logging to verify)*
- Story 1.2: Establish and health-check the ERP connection. Acceptance: a broken or expired connection fails closed and escalates.
- Story 1.3: [Partner-specific auth model, e.g. token-based service account.]
- **Gate:** connection proven in a sandbox ERP with rotation and fail-closed behavior demonstrated.

### Epic 2: Schema and field mapping (Mapper)
- Story 2.1: Map core entities (item, lot, transfer, adjustment). Acceptance: each Cavi field resolves to a defined ERP target or an explicit unmapped state; no silent drops.
- Story 2.2: Handle custom and required ERP fields. Acceptance: a missing required field blocks the write and surfaces the gap, rather than posting a partial record.
- Story 2.3: [Partner-specific chart-of-accounts / item taxonomy mapping.]
- **Gate:** a full mapping table reviewed and signed off by the partner.

### Epic 3: Human-approved write pipeline with dry-run and idempotency (Forge + approval gate)
- Story 3.1: Generate a dry-run diff for any proposed write. Acceptance: the human sees a computed before-and-after before commit. *(W4)*
- Story 3.2: Route write batches through the approval gate. Acceptance: unapproved batches never commit — including every n8n path. *(state machine built; W1 closes the bypass)*
- Story 3.3: Apply idempotency keys to commits. Acceptance: a retried commit does not double-post; proven under forced retry against the ERP. *(W2)*
- **Gate:** end-to-end approved write into a sandbox ERP with a demonstrated safe retry.

### Epic 4: Reconciliation and drift detection (Ledger + Ticker)
- Story 4.1: Scheduled reconciliation of Cavi vs ERP state. Acceptance: discrepancies are detected and reported within the defined window.
- Story 4.2: Drift surfacing to a human. Acceptance: drift raises an advisory flag a human disposes, not an automatic correction.
- Story 4.3: [Partner-specific reconciliation scope, e.g. which record types.]
- **Gate:** an injected discrepancy is detected and correctly surfaced.

### Epic 5: Audit, reversibility, and observability (Beacon)
- Story 5.1: Hash-chained write log. Acceptance: the log is tamper-evident and exportable. *(W7)*
- Story 5.2: Reversal / compensating action for a committed write. Acceptance: a reversal is itself recorded and auditable. *(W8)*
- Story 5.3: Observability dashboard for write health. Acceptance: latency, failures, retries, and drift are visible. *(W10; base layer built)*
- **Gate:** a committed write is audited and reversed end to end, with both actions on the chain.

### Epic 6: Failure handling and fail-closed recovery
- Story 6.1: Partial-write detection and halt. Acceptance: a partial batch halts and escalates; no half-posted state is left unreported.
- Story 6.2: Retry and circuit-breaker policy. Acceptance: repeated failures trip a breaker and escalate rather than hammering the ERP.
- Story 6.3: [Partner-specific recovery runbook.]
- **Gate:** a forced partial failure results in a clean halt, escalation, and a documented recovery path.

---

## 7. Open questions for the design partner

- Confirmed target ERP and version. [NetSuite assumed.]
- Deployment model: GrayMar-hosted vs client-tenant.
- Which record types are in scope for write-back in the first phase. [Working assumption from the reference use case: lot-numbered items, inventory adjustments, transfer orders; journal entries via the existing Ledger path.]
- Acceptable reconciliation window and drift tolerance (fills NFR4).
- Volume expectations (writes per day, peak batch size) — informs NetSuite governance-unit budgeting.
- [Others surfaced during scoping.]

Commercial terms (plan, fees, founding-partner discount, IP ownership) live in the partner proposal, not in this repository.

---

## 8. Sources of truth

- **This repository:** the code is the authoritative signal for what is live. Anchors: `README.md`, `agents/`, `schema_registry/` (the event contracts), `middleware/n8n/workflows/`.
- **cavi-core (separate repo):** live capability and price surfaces referenced by the engagement — `lib/marketplace/systems.ts`, `lib/pricing/plans.ts`, `lib/services/service-cards.ts`, `CAVI_STATUS.md`, `foundation/CAVI_ROADMAP.md`. Those files do not exist here; do not cite them as paths in this repo.
- If this PRD disagrees with the code, the code wins.
