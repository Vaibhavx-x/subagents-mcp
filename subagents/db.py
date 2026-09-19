"""SQLite access.

Every connection goes through `connect()`. There is no other sanctioned way to
open the database, because two of the three pragmas below are PER-CONNECTION:
a bare `sqlite3.connect` elsewhere would silently run without foreign keys and
the schema's referential integrity would become decorative.

The full Phase 2/3 schema is created now so those phases need no migration,
even though Phase 1 writes only `plans`.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS plans (
    id                TEXT PRIMARY KEY,
    plan_digest       TEXT NOT NULL,
    workspace_root    TEXT NOT NULL,
    plan_json         TEXT NOT NULL,
    scope_summary     TEXT,
    tier_summary      TEXT,
    wave_count        INTEGER NOT NULL,
    estimated_wall_s  INTEGER NOT NULL,
    worst_case_wall_s INTEGER NOT NULL,
    warnings          TEXT,
    created_at        TEXT NOT NULL,
    expires_at        TEXT NOT NULL,
    status            TEXT NOT NULL
        CHECK (status IN ('proposed','approved','executing','complete','partial','refused'))
);

CREATE TABLE IF NOT EXISTS runs (
    id               TEXT PRIMARY KEY,
    plan_id          TEXT NOT NULL REFERENCES plans(id) ON DELETE CASCADE,
    task_ref         TEXT NOT NULL,
    wave_index       INTEGER NOT NULL,
    instruction      TEXT NOT NULL,
    declared_reads   TEXT NOT NULL,
    declared_writes  TEXT NOT NULL,
    model_requested  TEXT,
    model_used       TEXT,
    status           TEXT NOT NULL,
    started_at       TEXT,
    finished_at      TEXT,
    tokens_in         INTEGER,
    tokens_out        INTEGER,
    thinking_tokens   INTEGER,
    cache_read_tokens INTEGER,
    exit_reason      TEXT,
    tainted          INTEGER NOT NULL DEFAULT 0,
    tainted_paths    TEXT,
    UNIQUE (plan_id, task_ref)
);

-- Written as each worker lands, never at the end. Writing at the end silently
-- defeats collect() after a cancellation, and only shows up on an overrun.
CREATE TABLE IF NOT EXISTS results (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id        TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    content       TEXT NOT NULL,
    content_bytes INTEGER NOT NULL,
    summary       TEXT NOT NULL,
    created_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS file_hashes (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id      TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    path        TEXT NOT NULL,
    phase       TEXT NOT NULL CHECK (phase IN ('pre','post')),
    sha256      TEXT,
    recorded_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS violations (
    id                       INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id                   TEXT REFERENCES runs(id) ON DELETE CASCADE,
    plan_id                  TEXT REFERENCES plans(id) ON DELETE CASCADE,
    attempted_action         TEXT NOT NULL,
    reason                   TEXT NOT NULL,
    blocked_at               TEXT NOT NULL,
    suggested_scope_addition TEXT
);

CREATE TABLE IF NOT EXISTS executions (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    plan_id           TEXT NOT NULL REFERENCES plans(id) ON DELETE CASCADE,
    started_at        TEXT NOT NULL,
    ended_at          TEXT,
    outcome           TEXT
        -- 'partial' was missing from the original schema sketch: an execution
        -- where some workers fail is the common case, not an edge case.
        CHECK (outcome IS NULL OR outcome IN
               ('complete','partial','cancelled_at_deadline',
                'refused_digest','refused_expiry')),
    workers_started   INTEGER NOT NULL DEFAULT 0,
    workers_completed INTEGER NOT NULL DEFAULT 0,
    waves_completed   INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_runs_plan     ON runs(plan_id);
CREATE INDEX IF NOT EXISTS idx_results_run   ON results(run_id);
CREATE INDEX IF NOT EXISTS idx_hashes_run    ON file_hashes(run_id);
"""

TABLES = ("plans", "runs", "results", "file_hashes", "violations", "executions")


def connect(db_path: str | Path) -> sqlite3.Connection:
    """Open a connection with all three pragmas applied.

    journal_mode is persistent per database; busy_timeout and foreign_keys are
    per connection and must be set every single time.
    """
    path = Path(db_path)
    if path.parent and str(path.parent) not in ("", "."):
        path.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA busy_timeout=5000;")
    conn.execute("PRAGMA foreign_keys=ON;")
    return conn


def init_db(db_path: str | Path) -> sqlite3.Connection:
    """Create the schema if absent and return an open connection."""
    conn = connect(db_path)
    conn.executescript(SCHEMA)
    conn.commit()
    return conn
