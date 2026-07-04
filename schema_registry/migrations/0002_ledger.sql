-- Cavi ERP — Ledger journal tables (double-entry system of record).
-- Applied after 0001_init.sql.

CREATE TABLE IF NOT EXISTS journal_entry (
    entry_id    UUID        PRIMARY KEY,
    currency    CHAR(3)     NOT NULL,
    memo        TEXT,
    total_minor BIGINT      NOT NULL CHECK (total_minor > 0),
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

COMMENT ON TABLE journal_entry IS
    'Balanced journal entries. total_minor = total debits = total credits.';

CREATE TABLE IF NOT EXISTS journal_line (
    id           BIGSERIAL PRIMARY KEY,
    entry_id     UUID      NOT NULL REFERENCES journal_entry (entry_id),
    account      TEXT      NOT NULL,
    direction    TEXT      NOT NULL CHECK (direction IN ('debit', 'credit')),
    amount_minor BIGINT    NOT NULL CHECK (amount_minor > 0)
);

CREATE INDEX IF NOT EXISTS idx_journal_line_entry   ON journal_line (entry_id);
CREATE INDEX IF NOT EXISTS idx_journal_line_account ON journal_line (account);
