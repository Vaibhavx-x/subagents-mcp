"""SQLite access.

Every connection goes through `connect()`. There is no other sanctioned way to
open the database, because two of the three pragmas below are PER-CONNECTION:
a bare `sqlite3.connect` elsewhere would silently run without foreign keys and
the schema's referential integrity would become decorative.

The full Phase 2/3 schema is created now so those phases need no migration,
even though Phase 1 writes only `plans`.
"""

from __future__ import annotations

import logging
import random
import sqlite3
import time
from datetime import datetime
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

log = logging.getLogger("subagents")

# busy_timeout covers a writer that arrives while another holds the lock. It
# does NOT cover every case: SQLite returns SQLITE_BUSY immediately, without
# invoking the busy handler, when a deferred transaction that has already read
# tries to upgrade to a write and the snapshot has moved on. Starting every
# write with BEGIN IMMEDIATE removes that case, and the retry below is the
# backstop for the rest.
WRITE_ATTEMPTS = 6
WRITE_BACKOFF_S = 0.05

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

-- A key and a pointer, never a copy. The transcript stays in `results` and
-- the hashes stay in `file_hashes`, so this table cannot drift out of
-- agreement with the audit trail -- it is an index over it, not a second copy
-- of it. ON DELETE CASCADE means pruning a plan prunes its entries.
CREATE TABLE IF NOT EXISTS cache_entries (
    key_hash   TEXT PRIMARY KEY,
    run_id     TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    task_ref   TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_runs_plan     ON runs(plan_id);
CREATE INDEX IF NOT EXISTS idx_results_run   ON results(run_id);
CREATE INDEX IF NOT EXISTS idx_hashes_run    ON file_hashes(run_id);
CREATE INDEX IF NOT EXISTS idx_cache_expiry  ON cache_entries(expires_at);
"""

TABLES = ("plans", "runs", "results", "file_hashes", "violations", "executions",
          "cache_entries")


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
    conn.execute("PRAGMA foreign_keys=OFF;")
    return conn


def init_db(db_path: str | Path) -> sqlite3.Connection:
    """Create the schema if absent and return an open connection."""
    conn = connect(db_path)
    conn.executescript(SCHEMA)
    conn.commit()
    return conn


def _is_locked(exc: sqlite3.OperationalError) -> bool:
    text = str(exc).lower()
    return "locked" in text or "busy" in text


@contextmanager
def write_transaction(db_path: str | Path) -> Iterator[sqlite3.Connection]:
    """Open a write transaction, retrying while the database is locked.

    Every writer takes the lock up front (BEGIN IMMEDIATE) rather than
    discovering the contention halfway through, so a retry always happens
    before anything has been written -- which is what makes retrying safe here.
    The body is not idempotent (it inserts a results row), so it must never be
    re-run after a partial write.

    Phase 3 runs a whole wave of workers whose results land at once; until then
    this path is exercised only by the concurrency tests.
    """
    conn = connect(db_path)
    try:
        for attempt in range(WRITE_ATTEMPTS - 1):
            try:
                conn.execute("BEGIN IMMEDIATE")
                break
            except sqlite3.OperationalError as exc:
                if not _is_locked(exc) or attempt == WRITE_ATTEMPTS - 1:
                    raise
                delay = WRITE_BACKOFF_S * (2 * attempt) + random.uniform(0, 0.02)
                log.warning("database locked, retrying in %.2fs (attempt %d)",
                            delay, attempt + 1)
                time.sleep(delay)
        try:
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
    finally:
        conn.close()


def completed_worker_durations(db_path: str | Path, limit: int = 200) -> list[float]:
    """Wall-clock seconds for workers that actually finished, newest first.

    `ok` only, and the reason is not fussiness. A timed-out worker ran for
    exactly its budget, so feeding those back into an estimate makes the
    estimate converge on whatever the timeout happens to be -- the estimator
    would end up predicting its own configuration rather than the work.

    SQL lives here because every other query does; the policy about what to do
    with these numbers lives in estimate.py.
    """
    path = Path(db_path)
    if not path.is_file():
        return []
    conn = connect(path)
    try:
        rows = conn.execute(
            "SELECT started_at, finished_at FROM runs"
            " WHERE status = 'ok' AND started_at IS NOT NULL AND finished_at IS NOT NULL"
            " ORDER BY finished_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
    except sqlite3.OperationalError:
        # No schema yet: a fresh install has no history and that is not an error.
        return []
    finally:
        conn.close()

    out: list[float] = []
    for started, finished in rows:
        try:
            seconds = (
                datetime.fromisoformat(finished) - datetime.fromisoformat(started)
            ).total_seconds()
        except (TypeError, ValueError):
            continue
        if seconds >= 0:
            out.append(seconds)
    return out
