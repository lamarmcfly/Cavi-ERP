-- Roll back 0004: drop the audit-chain columns. The chained history is lost on
-- rollback — export it first (scripts/audit_export.py) if it must be retained.
DROP INDEX IF EXISTS idx_event_log_chained;
DROP INDEX IF EXISTS idx_event_log_seq;
ALTER TABLE event_log DROP COLUMN IF EXISTS chain_hash;
ALTER TABLE event_log DROP COLUMN IF EXISTS seq;
