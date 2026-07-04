-- Cavi ERP — initial schema registry + event log
-- Run against the PostgreSQL instance defined in .env (CAVI_POSTGRES_*).

CREATE TABLE IF NOT EXISTS schema_registry (
    subject        TEXT        NOT NULL,
    version        INTEGER     NOT NULL,
    json_schema    JSONB       NOT NULL,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (subject, version)
);

COMMENT ON TABLE schema_registry IS
    'Versioned JSON Schemas — the contract every inter-agent event must honor.';

-- Append-only event log: the durable record behind the Redis pub/sub bus.
CREATE TABLE IF NOT EXISTS event_log (
    id             UUID        PRIMARY KEY,
    subject        TEXT        NOT NULL,
    schema_version INTEGER     NOT NULL,
    source         TEXT        NOT NULL,
    correlation_id UUID,
    payload        JSONB       NOT NULL,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    FOREIGN KEY (subject, schema_version) REFERENCES schema_registry (subject, version)
);

CREATE INDEX IF NOT EXISTS idx_event_log_subject       ON event_log (subject);
CREATE INDEX IF NOT EXISTS idx_event_log_correlation   ON event_log (correlation_id);

-- Dead-letter quarantine: events that failed validation, kept for replay.
CREATE TABLE IF NOT EXISTS event_deadletter (
    id             UUID        PRIMARY KEY,
    subject        TEXT        NOT NULL,
    source         TEXT        NOT NULL,
    raw            JSONB       NOT NULL,
    error          TEXT        NOT NULL,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);
