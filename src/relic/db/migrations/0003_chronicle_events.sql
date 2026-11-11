-- Canonical User-runtime Chronicle events
-- Version: 0003
-- Schema: chronicle-event/v2

CREATE TABLE IF NOT EXISTS chronicle_events (
    event_id            TEXT PRIMARY KEY,
    runtime_scope_id    TEXT NOT NULL,
    event_type          TEXT NOT NULL,
    event_category      TEXT NOT NULL,
    trace_id            TEXT NOT NULL,
    run_id              TEXT,
    session_id          TEXT,
    parent_event_id     TEXT,
    agent_id            TEXT,
    hermes_profile_id   TEXT,
    actor_type          TEXT,
    actor_id            TEXT,
    source_module       TEXT,
    target_module       TEXT,
    timestamp           TEXT NOT NULL,
    duration_ms         REAL,
    input_refs          TEXT DEFAULT '[]',
    output_refs         TEXT DEFAULT '[]',
    payload_redacted    INTEGER DEFAULT 0,
    payload_hash        TEXT,
    payload             TEXT DEFAULT '{}',
    sensitivity         TEXT NOT NULL DEFAULT 'safe',
    retention_policy    TEXT NOT NULL DEFAULT 'standard_365d',
    tags                TEXT DEFAULT '[]',
    severity            TEXT NOT NULL DEFAULT 'info',
    validation_status   TEXT,
    error_code          TEXT,
    retry_count         INTEGER DEFAULT 0,
    schema_version      TEXT NOT NULL DEFAULT 'chronicle-event/v2',
    created_at          TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_chronicle_events_scope ON chronicle_events(runtime_scope_id);
CREATE INDEX IF NOT EXISTS idx_chronicle_events_scope_timestamp ON chronicle_events(runtime_scope_id, timestamp);
CREATE INDEX IF NOT EXISTS idx_chronicle_events_trace ON chronicle_events(trace_id);
CREATE INDEX IF NOT EXISTS idx_chronicle_events_session ON chronicle_events(session_id);
CREATE INDEX IF NOT EXISTS idx_chronicle_events_run ON chronicle_events(run_id);
CREATE INDEX IF NOT EXISTS idx_chronicle_events_type ON chronicle_events(event_type);
CREATE INDEX IF NOT EXISTS idx_chronicle_events_category ON chronicle_events(event_category);
CREATE INDEX IF NOT EXISTS idx_chronicle_events_parent ON chronicle_events(parent_event_id);
CREATE INDEX IF NOT EXISTS idx_chronicle_events_retention ON chronicle_events(retention_policy);

INSERT OR REPLACE INTO schema_version (version, applied_at) VALUES ('0003', CURRENT_TIMESTAMP);
