"""Concurrent writers against one SQLite database.

WAL and `busy_timeout=5000` were configured in Phase 1 and never exercised by
more than one writer at a time, so "it is configured" was the only evidence
they worked. Phase 3 lands a whole wave of results at once; these tests are the
part of that which can be checked before the parallelism exists.

Threads, not processes: `sqlite3` releases the GIL around `execute`, so the
contention is real, and a thread can share the test's tmp_path fixture.
"""

from __future__ import annotations

import sqlite3
import threading

import pytest
from conftest import make_task, tasks_json
from test_execution import make_plan, make_result

from subagents.db import WRITE_ATTEMPTS, connect, init_db, write_transaction
from subagents.execution import persist_result

WORKERS = 8


def entry(ref: str) -> dict:
    return {"task_ref": ref, "instruction": "do it",
            "reads": ["README.md"], "writes": [f"out-{ref}.txt"]}


def test_every_concurrent_writer_lands(cfg, workspace):
    """The invariant Phase 3 depends on: N workers finish, N results exist.

    A dropped row here would not raise -- it would silently shorten collect().
    """
    refs = [f"t{i}" for i in range(WORKERS)]
    plan = make_plan(cfg, workspace, *(entry(r) for r in refs))

    errors: list[BaseException] = []
    start = threading.Barrier(WORKERS)

    def write(ref: str) -> None:
        try:
            start.wait(timeout=10)          # maximise the overlap
            persist_result(cfg, plan.plan_id, make_task(ref, writes=(f"out-{ref}.txt",)),
                           0, make_result(ref))
        except BaseException as exc:        # noqa: BLE001 -- recorded, then re-raised below
            errors.append(exc)

    threads = [threading.Thread(target=write, args=(r,)) for r in refs]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)

    assert not errors, f"concurrent write failed: {errors[0]!r}"
    conn = connect(cfg.db_path)
    try:
        assert conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == WORKERS
        assert conn.execute("SELECT COUNT(*) FROM results").fetchone()[0] == WORKERS
        stored = {r["task_ref"] for r in conn.execute("SELECT task_ref FROM runs")}
        assert stored == set(refs)
    finally:
        conn.close()


def test_a_write_is_visible_to_a_reader_immediately(cfg, workspace):
    """collect() after a cancellation reads from a different connection."""
    plan = make_plan(cfg, workspace, entry("a"), entry("b"))
    persist_result(cfg, plan.plan_id, make_task("a", writes=("out-a.txt",)), 0, make_result("a"))

    conn = connect(cfg.db_path)
    try:
        assert conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 1
    finally:
        conn.close()


def test_write_transaction_rolls_back_on_error(cfg):
    """A half-written result must not survive: collect() would report a run
    with no content behind its handle."""
    init_db(cfg.db_path).close()
    with pytest.raises(RuntimeError):
        with write_transaction(cfg.db_path) as conn:
            conn.execute(
                "INSERT INTO plans (id, plan_digest, workspace_root, plan_json,"
                " wave_count, estimated_wall_s, worst_case_wall_s, created_at,"
                " expires_at, status) VALUES ('x','d','w','{}',1,1,1,'t','t','proposed')"
            )
            raise RuntimeError("boom")

    conn = connect(cfg.db_path)
    try:
        assert conn.execute("SELECT COUNT(*) FROM plans WHERE id='x'").fetchone()[0] == 0
    finally:
        conn.close()


def test_a_held_write_lock_is_waited_out_not_failed(cfg, workspace):
    """The retry exists for the case busy_timeout does not cover.

    Here the lock is released after the first writer would have hit it, so the
    correct behaviour is to wait and succeed -- not to raise 'database is
    locked' and lose a finished worker's output.
    """
    plan = make_plan(cfg, workspace, entry("a"))
    holder_ready = threading.Event()
    release = threading.Event()

    def hold() -> None:
        conn = connect(cfg.db_path)
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("UPDATE plans SET status='executing' WHERE id=?", (plan.plan_id,))
            holder_ready.set()
            release.wait(timeout=10)
            conn.rollback()
        finally:
            conn.close()

    t = threading.Thread(target=hold)
    t.start()
    try:
        assert holder_ready.wait(timeout=10)
        done = threading.Event()

        def writer() -> None:
            persist_result(cfg, plan.plan_id, make_task("a", writes=("out-a.txt",)),
                           0, make_result("a"))
            done.set()

        w = threading.Thread(target=writer)
        w.start()
        assert not done.wait(timeout=0.5), "the writer did not actually contend for the lock"
        release.set()
        w.join(timeout=30)
        assert done.is_set(), "the writer never completed after the lock was released"
    finally:
        release.set()
        t.join(timeout=10)

    conn = connect(cfg.db_path)
    try:
        assert conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 1
    finally:
        conn.close()


def test_the_retry_fires_when_busy_timeout_is_exhausted(cfg, workspace, monkeypatch):
    """The previous test proves busy_timeout works, not that the retry does.

    busy_timeout absorbs the wait, so with it set to 5s the retry loop never
    runs and would be dead code nobody noticed. Dropping it to 50ms makes the
    lock produce a real SQLITE_BUSY, which only the retry can survive.
    """
    import subagents.db as db

    real_connect = db.connect

    def impatient(path):
        conn = real_connect(path)
        conn.execute("PRAGMA busy_timeout=50;")
        return conn

    plan = make_plan(cfg, workspace, entry("a"))
    monkeypatch.setattr(db, "connect", impatient)

    holder_ready = threading.Event()
    release = threading.Event()

    def hold() -> None:
        conn = real_connect(cfg.db_path)
        try:
            conn.execute("BEGIN IMMEDIATE")
            holder_ready.set()
            release.wait(timeout=10)
            conn.rollback()
        finally:
            conn.close()

    t = threading.Thread(target=hold)
    t.start()
    try:
        assert holder_ready.wait(timeout=10)
        threading.Timer(0.4, release.set).start()
        # Without the retry this raises OperationalError: database is locked.
        persist_result(cfg, plan.plan_id, make_task("a", writes=("out-a.txt",)),
                       0, make_result("a"))
    finally:
        release.set()
        t.join(timeout=10)

    conn = real_connect(cfg.db_path)
    try:
        assert conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 1
    finally:
        conn.close()


def test_retry_gives_up_rather_than_hanging_forever():
    """Bounded, so a wedged lock surfaces as an error rather than a hung tool."""
    assert WRITE_ATTEMPTS <= 10
