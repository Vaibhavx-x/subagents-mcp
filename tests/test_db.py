"""Schema and pragmas.

`PRAGMA foreign_keys` is OFF by default and is PER CONNECTION, so it is easy to
have a schema full of REFERENCES clauses that enforce nothing. Asserting the
pragma value alone would not catch a connection helper that set it on the wrong
handle, so the important test here forces an actual integrity violation.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from subagents.db import TABLES, connect, init_db


@pytest.fixture
def db(tmp_path: Path):
    conn = init_db(tmp_path / "t.db")
    yield conn
    conn.close()


def insert_plan(conn, plan_id="p1", digest="d"):
    conn.execute(
        "INSERT INTO plans (id, plan_digest, workspace_root, plan_json, wave_count,"
        " estimated_wall_s, worst_case_wall_s, created_at, expires_at, status)"
        " VALUES (?,?,?,?,?,?,?,?,?,?)",
        (plan_id, digest, "D:/ws", "[]", 1, 70, 610, "now", "later", "proposed"),
    )
    conn.commit()


def insert_run(conn, run_id, plan_id, task_ref):
    conn.execute(
        "INSERT INTO runs (id, plan_id, task_ref, wave_index, instruction,"
        " declared_reads, declared_writes, status) VALUES (?,?,?,?,?,?,?,?)",
        (run_id, plan_id, task_ref, 0, "i", "[]", "[]", "pending"),
    )
    conn.commit()


# ------------------------------------------------------------------ pragmas
def test_foreign_keys_pragma_on(db):
    assert db.execute("PRAGMA foreign_keys").fetchone()[0] == 1


def test_wal_mode(db):
    assert db.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"


def test_busy_timeout_set(db):
    assert db.execute("PRAGMA busy_timeout").fetchone()[0] == 5000


def test_fk_violation_is_actually_rejected(db):
    """The test that proves the pragma is doing work, not merely reporting 1."""
    with pytest.raises(sqlite3.IntegrityError):
        insert_run(db, "r1", "no-such-plan", "a")


def test_fk_enforced_on_a_second_connection(tmp_path: Path):
    """foreign_keys is per-connection: a reopened database must still enforce."""
    path = tmp_path / "t.db"
    init_db(path).close()
    conn = connect(path)
    try:
        with pytest.raises(sqlite3.IntegrityError):
            insert_run(conn, "r1", "no-such-plan", "a")
    finally:
        conn.close()


def test_cascade_delete_removes_runs(db):
    insert_plan(db)
    insert_run(db, "r1", "p1", "a")
    db.execute("DELETE FROM plans WHERE id='p1'")
    db.commit()
    assert db.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 0


# ------------------------------------------------------------------- schema
def test_schema_has_every_table(db):
    present = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    missing = set(TABLES) - present
    assert not missing, f"missing tables (Phase 2/3 would need a migration): {missing}"


def test_no_approvals_table(db):
    """Approval happens in the client's permission prompt, which this server
    never observes. A row claiming otherwise would be a lie in the audit trail."""
    present = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "approvals" not in present


def test_unique_plan_task_ref(db):
    insert_plan(db)
    insert_run(db, "r1", "p1", "dup")
    with pytest.raises(sqlite3.IntegrityError):
        insert_run(db, "r2", "p1", "dup")


def test_same_task_ref_across_plans_allowed(db):
    """(plan_id, task_ref) is scoped to the plan, not global."""
    insert_plan(db, "p1", "d1")
    insert_plan(db, "p2", "d2")
    insert_run(db, "r1", "p1", "shared")
    insert_run(db, "r2", "p2", "shared")
    assert db.execute("SELECT COUNT(*) FROM runs WHERE task_ref='shared'").fetchone()[0] == 2


def test_plan_status_is_constrained(db):
    with pytest.raises(sqlite3.IntegrityError):
        insert_plan(db, "bad", "d")
        db.execute("UPDATE plans SET status='nonsense' WHERE id='bad'")
        db.commit()


def test_file_hash_phase_is_constrained(db):
    insert_plan(db)
    insert_run(db, "r1", "p1", "a")
    with pytest.raises(sqlite3.IntegrityError):
        db.execute(
            "INSERT INTO file_hashes (run_id, path, phase, sha256, recorded_at)"
            " VALUES (?,?,?,?,?)",
            ("r1", "D:/ws/a.py", "midway", "abc", "now"),
        )
        db.commit()


def test_results_table_supports_per_worker_writes(db):
    """Phase 3 writes each result as it lands; collect() after a cancellation
    depends on rows existing before the run is finished."""
    insert_plan(db)
    insert_run(db, "r1", "p1", "a")
    db.execute(
        "INSERT INTO results (run_id, content, content_bytes, summary, created_at)"
        " VALUES (?,?,?,?,?)",
        ("r1", "full transcript", 15, "did the thing", "now"),
    )
    db.commit()
    assert db.execute("SELECT summary FROM results WHERE run_id='r1'").fetchone()[0] == "did the thing"


def test_init_db_is_idempotent(tmp_path: Path):
    path = tmp_path / "t.db"
    init_db(path).close()
    conn = init_db(path)
    try:
        present = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert set(TABLES) <= present
    finally:
        conn.close()
