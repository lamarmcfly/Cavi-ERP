-- Cavi ERP — tenant isolation (H1)
-- Adds tenant_id to the books and the audit log so tenants' data never
-- commingles. The journal tables ENFORCE it (NOT NULL); event_log carries it
-- for tenant-scoped audit queries but stays nullable so non-tenant-scoped
-- system events can still be logged.
-- Idempotent: ADD COLUMN IF NOT EXISTS + guarded NOT NULL, safe to re-run.

-- --- Financial books: enforce tenant scoping ---
ALTER TABLE journal_entry ADD COLUMN IF NOT EXISTS tenant_id TEXT;
ALTER TABLE journal_line  ADD COLUMN IF NOT EXISTS tenant_id TEXT;

-- Backfill any pre-existing rows (there are none in a fresh DB) before enforcing.
UPDATE journal_entry SET tenant_id = 'unknown' WHERE tenant_id IS NULL;
UPDATE journal_line  SET tenant_id = 'unknown' WHERE tenant_id IS NULL;

ALTER TABLE journal_entry ALTER COLUMN tenant_id SET NOT NULL;
ALTER TABLE journal_line  ALTER COLUMN tenant_id SET NOT NULL;

CREATE INDEX IF NOT EXISTS idx_journal_entry_tenant ON journal_entry (tenant_id);
CREATE INDEX IF NOT EXISTS idx_journal_line_tenant  ON journal_line  (tenant_id);

-- --- Audit log: tenant-scoped querying (nullable — system events may have none) ---
ALTER TABLE event_log ADD COLUMN IF NOT EXISTS tenant_id TEXT;
CREATE INDEX IF NOT EXISTS idx_event_log_tenant ON event_log (tenant_id);
