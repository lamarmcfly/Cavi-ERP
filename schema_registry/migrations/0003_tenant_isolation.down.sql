-- Rollback of 0003_tenant_isolation. Drops the tenant_id columns + indexes.
-- WARNING: destroys tenant scoping on the books — only for a controlled revert.
DROP INDEX IF EXISTS idx_event_log_tenant;
ALTER TABLE event_log DROP COLUMN IF EXISTS tenant_id;

DROP INDEX IF EXISTS idx_journal_line_tenant;
DROP INDEX IF EXISTS idx_journal_entry_tenant;
ALTER TABLE journal_line  DROP COLUMN IF EXISTS tenant_id;
ALTER TABLE journal_entry DROP COLUMN IF EXISTS tenant_id;
