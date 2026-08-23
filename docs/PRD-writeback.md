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
- n8n routing with a `netsuite-sync` workflow that files write **proposals** (`forge.write.propose`) from `ledger.posted`; the Vault-signed NetSuite call itself lives in the Forge write agent's `NetSuiteClient` and runs only after approval.

**Known gaps this PRD exists to close**
1. ~~**The live `netsuite-sync` workflow bypasses the approval gate.**~~ **Closed (W1).** The workflow now files a `forge.write.propose` on `ledger.posted` and performs no ERP call; nothing reaches NetSuite without a `forge.write.decision` approving it.
2. ~~**No ERP-side idempotency.**~~ **Closed (W2).** `WriteOperation.idempotency_key` is a stable, namespaced key the writer contract requires be sent to the ERP; a lost-response retry is proven not to double-post.
3. ~~**Dry-run is a free-form string.**~~ **Closed (W4).** With an `ErpReader` configured, the `diff_preview` is derived from the record's current ERP state (`render_diff`); a caller-supplied string is ignored, and a failed dry-run fetch fails closed (the propose event is dead-lettered for Beacon, never recorded with a fabricated diff).
4. ~~**`event_log` is durable but not hash-chained.**~~ **Closed (W7).** Every recorded event carries `chain_hash = sha256(prev_hash + canonical(event))` (migration 0004; Postgres serializes appends with an advisory lock, so concurrent writers cannot fork the chain). Editing, deleting, or reordering any chained row breaks every hash after it. `scripts/audit_export.py` exports the log as self-verifying JSONL and `--verify` recomputes the chain — checkable by anyone holding the export, without trusting the database. The chain era starts at migration 0004; pre-chain rows stay explicitly unchained rather than pretending to notarize prior history.
5. ~~**No reconciliation or drift detection.**~~ **Closed (W6) at the platform level.** Scheduled passes (n8n `reconciliation-cadence` → `ticker.reconciliation.due`) drive `LedgerReconcileAgent`: every pass emits a `ledger.reconciliation.completed` heartbeat; discrepancies (missing-in-ERP, unexpected-in-ERP, field mismatch on managed fields only) additionally emit `ledger.drift.detected`, which Beacon surfaces as a WARNING a human disposes — never an auto-correction. Remaining for the Epic 4 gate: the production state sources (an `event_log` projection for the Cavi side, a Vault-signed NetSuite bulk query for the ERP side) — until they are wired, a due event dead-letters (fail closed) rather than reporting a pass that never ran.
6. ~~**The real NetSuite `ErpWriter` is not yet wired (W3).**~~ **Closed (W3).** `agents/forge/netsuite.py` implements both writer and reader: every request is Vault-signed (`/sign`; secrets never leave Vault), and writes are external-id upserts — a create keyed by its own idempotency key (NetSuite dedupes retries), an update addressed by `target_external_id`, the existing record's id (`forge.write.requested` v3), so an update can never upsert a duplicate under a fresh key. Every refusal — missing config, Vault denial, ERP rejection, transport failure, unsupported operation, an update without a target — fails closed. Wired into the `forge-write` compose service; sandbox validation against a live NetSuite account is the remaining Epic 1/3 gate work.
7. ~~**No reversal flow, circuit breaker, or partial-batch halt.**~~ **Closed (W8, W9).** A reversal is a *new* `forge.write` lifecycle — its own write_id, dry-run, human approval, and audit trail — linked to its COMPLETED original via the required-nullable `reverses` field (`forge.write.requested` v2; v1 producers coerce through Mapper). Only a completed write is reversible. The circuit breaker trips after consecutive ERP failures (writes stay APPROVED and retryable; the trip is escalated once via dead-letter/Beacon; one half-open probe after cooldown). `execute_batch` guards the whole batch on APPROVED before any ERP call, halts at the first failure, and reports exactly what committed / failed / was never attempted — a partial batch is loud, never silent.

---

## 5. Implementation plan (ordered)

Order is by risk: governance-critical fixes to paths that exist today come first.

- **W1 — Route ERP writes through the approval gate** (Epic 3 / FR4). **Done.** `netsuite-sync` now turns `ledger.posted` into a `forge.write.propose` (with diff preview); execution happens only after `forge.write.decision` approves, via the Forge write agent. The direct-post path is retired.
- **W2 — ERP-side idempotency keys** (Epic 3 / FR5). **Done.** `WriteOperation.idempotency_key` (`cavi-{write_id}`, stable across retries) is required by the `ErpWriter` contract; `test_retry_after_lost_response_does_not_double_post` proves exactly-once against an ERP that honors the key.
- **W3 — Real NetSuite `ErpWriter`** (Epics 1, 3). **Done (code).** `NetSuiteClient` in `agents/forge/netsuite.py`: Vault `/sign` integration, external-id upsert via NetSuite REST, fail-closed on missing config / auth errors / unsupported operations; wired into the `forge-write` compose service. Remaining for the Epic 1/3 gates: sandbox validation against a live NetSuite account, and governance-unit / rate-limit budgeting once partner volumes are known (open question §7).
- **W4 — True dry-run diffs** (Epic 3 / FR3). **Done.** With a reader configured, `request()` fetches the record's current ERP state and derives the diff (`render_diff`) — the caller's asserted preview is ignored; a failed fetch dead-letters the proposal (fail closed) rather than recording a fabricated diff.
- **W5 — Partner record-type mappings** (Epic 2 / FR2). **Done (reference tables).** `agents/mapper/netsuite_records.py`: declarative mappings for lot-numbered inventory item, inventory adjustment, and transfer order, registered on the Mapper ERP transformer. Missing required fields block with *every* gap surfaced at once (line-level included); an undeclared Cavi field blocks rather than silently drops — every field maps or is explicitly declared unmapped. Remaining for the Epic 2 gate: partner sign-off on the mapping table, custom fields, and chart-of-accounts targets (§7).
- **W6 — Reconciliation and drift** (Epic 4 / FR6). **Done (platform).** n8n `reconciliation-cadence` publishes `ticker.reconciliation.due` on the drift window (NFR4 knob); `LedgerReconcileAgent` compares expected vs actual by external id (managed fields only), always emits the `ledger.reconciliation.completed` heartbeat, and emits `ledger.drift.detected` on discrepancies — surfaced by Beacon as an advisory WARNING a human disposes. Unconfigured or failing state sources dead-letter the due event (fail closed). Remaining for the Epic 4 gate: production state sources (event_log projection + NetSuite bulk query) and the partner's reconciliation scope/window.
- **W7 — Hash-chained audit log** (Epic 5 / FR7). **Done.** `shared/audit.py` + migration 0004: chained `event_log` writes (advisory-locked in Postgres, mirrored in the in-memory store), tamper detection proven for edit/delete/reorder, and a self-verifying JSONL export (`scripts/audit_export.py`, `--verify` exits non-zero on any break).
- **W8 — Reversal / compensating writes** (Epic 5 / FR8). **Done.** `request_reversal` on the coordinator plus the `forge.write.reverse` trigger: only COMPLETED writes reversible, linkage via `reverses` on `forge.write.requested` v2, and the reversal passes through the same dry-run + approval gate — both actions land on the audit chain.
- **W9 — Circuit breaker and partial-batch halt** (Epic 6 / FR9). **Done.** `agents/forge/breaker.py` (closed/open/half-open, single-probe recovery, escalate-once on trip) wired into the write agent; `execute_batch` halts on first failure with a full committed/failed/not-attempted report and refuses a batch containing any unapproved write before touching the ERP.
- **W10 — Write-path observability** (Epic 5 / NFR6). **Done (metrics).** New counters on the existing scrapeable `/metrics` surface: write lifecycle stages, failures by reason, summed execute latency, breaker trips, reconciliation passes, drift by kind. A rendered dashboard over these lives in Mission Control (cavi-core), not this repo.

---

## 6. Epic list and gates

No epic is considered done, and no downstream epic starts trusting its output, until the gate criteria pass. Gates are the timeline mechanism referenced in the partner proposal.

### Epic 1: Secure ERP connection and credential brokering (Vault) — largely built
- Story 1.1: Store per-tenant ERP credentials with isolation. Acceptance: credentials are never readable across tenants; rotation is supported; access is logged. *(built; access-logging to verify)*
- Story 1.2: Establish and health-check the ERP connection. Acceptance: a broken or expired connection fails closed and escalates.
- Story 1.3: [Partner-specific auth model, e.g. token-based service account.]
- **Gate:** connection proven in a sandbox ERP with rotation and fail-closed behavior demonstrated.

### Epic 2: Schema and field mapping (Mapper)
- Story 2.1: Map core entities (item, lot, transfer, adjustment). Acceptance: each Cavi field resolves to a defined ERP target or an explicit unmapped state; no silent drops. *(done: W5 — an undeclared field blocks the mapping)*
- Story 2.2: Handle custom and required ERP fields. Acceptance: a missing required field blocks the write and surfaces the gap, rather than posting a partial record. *(done: W5 — all gaps surface at once, line-level included)*
- Story 2.3: [Partner-specific chart-of-accounts / item taxonomy mapping.]
- **Gate:** a full mapping table reviewed and signed off by the partner.

### Epic 3: Human-approved write pipeline with dry-run and idempotency (Forge + approval gate)
- Story 3.1: Generate a dry-run diff for any proposed write. Acceptance: the human sees a computed before-and-after before commit. *(done: W4)*
- Story 3.2: Route write batches through the approval gate. Acceptance: unapproved batches never commit — including every n8n path. *(done: state machine + W1 closed the netsuite-sync bypass)*
- Story 3.3: Apply idempotency keys to commits. Acceptance: a retried commit does not double-post; proven under forced retry against the ERP. *(done: W2)*
- **Gate:** end-to-end approved write into a sandbox ERP with a demonstrated safe retry.

### Epic 4: Reconciliation and drift detection (Ledger + Ticker)
- Story 4.1: Scheduled reconciliation of Cavi vs ERP state. Acceptance: discrepancies are detected and reported within the defined window. *(platform done: W6 — cadence, comparator, and events; production state sources remain)*
- Story 4.2: Drift surfacing to a human. Acceptance: drift raises an advisory flag a human disposes, not an automatic correction. *(done: W6 — `ledger.drift.detected` → Beacon WARNING; nothing auto-corrects)*
- Story 4.3: [Partner-specific reconciliation scope, e.g. which record types.]
- **Gate:** an injected discrepancy is detected and correctly surfaced. *(proven in-memory for all three kinds — `tests/test_ledger_reconcile.py`; gate closes end-to-end once production state sources land)*

### Epic 5: Audit, reversibility, and observability (Beacon)
- Story 5.1: Hash-chained write log. Acceptance: the log is tamper-evident and exportable. *(done: W7 — edit/delete/reorder detection proven; self-verifying export)*
- Story 5.2: Reversal / compensating action for a committed write. Acceptance: a reversal is itself recorded and auditable. *(done: W8 — a full second lifecycle, linked via `reverses`, through the same gate)*
- Story 5.3: Observability dashboard for write health. Acceptance: latency, failures, retries, and drift are visible. *(metrics done: W10 — exposed at `/metrics`; the rendered dashboard is Mission Control / cavi-core)*
- **Gate:** a committed write is audited and reversed end to end, with both actions on the chain. *(mechanism complete; run end-to-end in the sandbox alongside the Epic 3 gate)*

### Epic 6: Failure handling and fail-closed recovery
- Story 6.1: Partial-write detection and halt. Acceptance: a partial batch halts and escalates; no half-posted state is left unreported. *(done: W9 — `execute_batch` halts at first failure and reports committed / failed / not-attempted)*
- Story 6.2: Retry and circuit-breaker policy. Acceptance: repeated failures trip a breaker and escalate rather than hammering the ERP. *(done: W9 — escalates once on trip via dead-letter → Beacon; writes stay APPROVED and retryable)*
- Story 6.3: [Partner-specific recovery runbook.]
- **Gate:** a forced partial failure results in a clean halt, escalation, and a documented recovery path. *(halt + escalation proven in tests; the runbook is partner-facing gate work)*

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
