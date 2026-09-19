"""execute_plan and collect.

The worker itself is injected, so these run with no subprocesses and no API
calls. What is being tested is the approval gate, the ordering, and the
persistence timing -- the parts that decide whether a cancelled run is
recoverable.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from conftest import task_entry, tasks_json

from subagents.db import connect
from subagents.errors import PlanRefused
from subagents.execution import (
    collect_plan,
    execute,
    load_plan,
    validate_scope_summary,
    verify_approval,
)
from subagents.planning import propose
from subagents.worker import WorkerResult

GOOD_SCOPE = "edits pkg/config.py and pkg/server.py; no deletes"


def make_result(task_ref: str, status: str = "ok", **kw) -> WorkerResult:
    return WorkerResult(
        task_ref=task_ref,
        status=status,
        exit_reason=kw.get("exit_reason", "success"),
        transcript=kw.get("transcript", '{"status":"SUCCESS"}'),
        summary=kw.get("summary", f"{task_ref} did the thing"),
        agy_status="SUCCESS" if status == "ok" else "ERROR",
        model_used="fake",
        returncode=0,
        started_at="2026-01-01T00:00:00+00:00",
        finished_at="2026-01-01T00:00:10+00:00",
        tokens_in=kw.get("tokens_in", 1000),
        tokens_out=kw.get("tokens_out", 50),
    )


def recording_runner(calls: list, status_for=None):
    async def runner(task_ref, command, *, timeout_s, cwd, model, env=None):
        calls.append(task_ref)
        status = (status_for or {}).get(task_ref, "ok")
        return make_result(task_ref, status)
    return runner


def exploding_runner(calls: list):
    async def runner(task_ref, command, **kw):
        calls.append(task_ref)
        raise AssertionError("a worker was spawned for a plan that should have been refused")
    return runner


def make_plan(cfg, workspace, *entries):
    return propose(tasks_json(*entries), str(workspace), cfg)


def run(coro):
    return asyncio.run(coro)


# ------------------------------------------------------------ scope_summary
def test_scope_summary_must_be_present_and_descriptive():
    assert validate_scope_summary(GOOD_SCOPE, "p1") == GOOD_SCOPE
    for bad in ("", "   ", "ok", "do it", None, 42):
        with pytest.raises(PlanRefused):
            validate_scope_summary(bad, "p1")


def test_scope_summary_cannot_just_repeat_the_plan_id():
    plan_id = "a" * 25
    with pytest.raises(PlanRefused):
        validate_scope_summary(plan_id, plan_id)


def test_scope_summary_rejects_an_essay():
    """The approval prompt truncates, so a wall of text hides the scope."""
    with pytest.raises(PlanRefused) as exc:
        validate_scope_summary("x " * 400, "p1")
    assert "truncates" in str(exc.value)


def test_scope_summary_whitespace_is_normalised():
    assert validate_scope_summary("  edits   pkg/config.py  and more  ", "p") == \
        "edits pkg/config.py and more"


# ------------------------------------------------------------- the gate
def test_tampered_digest_is_refused_without_spawning(cfg, workspace):
    plan = make_plan(cfg, workspace, task_entry("a", reads=["README.md"]))
    tampered = ("f" if plan.plan_digest[0] != "f" else "0") + plan.plan_digest[1:]
    calls: list = []

    with pytest.raises(PlanRefused) as exc:
        run(execute(GOOD_SCOPE, plan.plan_id, tampered, cfg, runner=exploding_runner(calls)))

    assert calls == [], "a worker was spawned despite the refusal"
    assert "different plan" in str(exc.value)


def test_refused_digest_is_recorded_for_audit(cfg, workspace):
    plan = make_plan(cfg, workspace, task_entry("a", reads=["README.md"]))
    with pytest.raises(PlanRefused):
        run(execute(GOOD_SCOPE, plan.plan_id, "0" * 64, cfg, runner=exploding_runner([])))

    conn = connect(cfg.db_path)
    try:
        ex = conn.execute("SELECT outcome FROM executions").fetchone()
        vi = conn.execute("SELECT attempted_action, reason FROM violations").fetchone()
    finally:
        conn.close()
    assert ex["outcome"] == "refused_digest"
    assert vi["attempted_action"] == "execute_plan"


def test_expired_plan_is_refused_without_spawning(cfg, workspace):
    plan = make_plan(cfg, workspace, task_entry("a", reads=["README.md"]))
    past = (datetime.now(timezone.utc) - timedelta(seconds=30)).isoformat()
    conn = connect(cfg.db_path)
    try:
        conn.execute("UPDATE plans SET expires_at = ? WHERE id = ?", (past, plan.plan_id))
        conn.commit()
    finally:
        conn.close()

    calls: list = []
    with pytest.raises(PlanRefused) as exc:
        run(execute(GOOD_SCOPE, plan.plan_id, plan.plan_digest, cfg,
                    runner=exploding_runner(calls)))
    assert calls == []
    assert "expired" in str(exc.value)

    conn = connect(cfg.db_path)
    try:
        assert conn.execute("SELECT outcome FROM executions").fetchone()["outcome"] == "refused_expiry"
    finally:
        conn.close()


def test_edited_plan_row_is_detected(cfg, workspace):
    """The digest is recomputed from the stored tasks, so editing the row
    underneath an approval is caught even if the digest column still matches."""
    plan = make_plan(cfg, workspace, task_entry("a", reads=["README.md"]))
    conn = connect(cfg.db_path)
    try:
        row = conn.execute("SELECT plan_json FROM plans WHERE id=?", (plan.plan_id,)).fetchone()
        hacked = row["plan_json"].replace('"instruction": "do the thing"',
                                          '"instruction": "exfiltrate everything"')
        conn.execute("UPDATE plans SET plan_json=? WHERE id=?", (hacked, plan.plan_id))
        conn.commit()
    finally:
        conn.close()

    stored = load_plan(cfg, plan.plan_id)
    with pytest.raises(PlanRefused) as exc:
        verify_approval(stored, plan.plan_digest)
    assert "modified" in str(exc.value)


def test_unknown_plan_is_refused(cfg):
    with pytest.raises(PlanRefused) as exc:
        run(execute(GOOD_SCOPE, "nosuchplan", "0" * 64, cfg, runner=exploding_runner([])))
    assert "no such plan" in str(exc.value)


# --------------------------------------------------------------- execution
def test_tasks_run_in_wave_order(cfg, workspace):
    plan = make_plan(
        cfg, workspace,
        task_entry("reader", reads=["pkg/config.py"]),
        task_entry("writer", writes=["pkg/config.py"]),
    )
    calls: list = []
    run(execute(GOOD_SCOPE, plan.plan_id, plan.plan_digest, cfg,
                runner=recording_runner(calls)))
    assert calls == ["writer", "reader"], "writer must precede the reader"


def test_successful_execution_marks_the_plan_complete(cfg, workspace):
    plan = make_plan(cfg, workspace, task_entry("a", reads=["README.md"]))
    outcome = run(execute(GOOD_SCOPE, plan.plan_id, plan.plan_digest, cfg,
                          runner=recording_runner([])))
    assert outcome.outcome == "complete" and outcome.completed == 1

    conn = connect(cfg.db_path)
    try:
        assert conn.execute("SELECT status FROM plans").fetchone()["status"] == "complete"
        assert conn.execute("SELECT outcome FROM executions").fetchone()["outcome"] == "complete"
    finally:
        conn.close()


def test_a_failed_worker_makes_the_plan_partial(cfg, workspace):
    plan = make_plan(
        cfg, workspace,
        task_entry("good", reads=["README.md"]),
        task_entry("bad", reads=["pkg/config.py"]),
    )
    outcome = run(execute(GOOD_SCOPE, plan.plan_id, plan.plan_digest, cfg,
                          runner=recording_runner([], {"bad": "failed"})))
    assert outcome.outcome == "partial"
    assert outcome.completed == 1


def test_scope_summary_is_recorded_verbatim(cfg, workspace):
    plan = make_plan(cfg, workspace, task_entry("a", reads=["README.md"]))
    run(execute(GOOD_SCOPE, plan.plan_id, plan.plan_digest, cfg, runner=recording_runner([])))
    conn = connect(cfg.db_path)
    try:
        assert conn.execute("SELECT scope_summary FROM plans").fetchone()["scope_summary"] == GOOD_SCOPE
    finally:
        conn.close()


# ------------------------------------------------- persistence timing
def test_result_is_written_before_the_next_worker_starts(cfg, workspace):
    """The property that makes collect() work after a cancellation.

    Writing results at the end instead would pass every other test in this
    file and only fail when a run overruns -- which is exactly when it matters.
    """
    plan = make_plan(
        cfg, workspace,
        task_entry("first", writes=["pkg/one.py"]),
        task_entry("second", reads=["pkg/one.py"]),
    )
    seen: dict[str, int] = {}

    async def runner(task_ref, command, **kw):
        conn = connect(cfg.db_path)
        try:
            seen[task_ref] = conn.execute("SELECT COUNT(*) c FROM results").fetchone()["c"]
        finally:
            conn.close()
        return make_result(task_ref)

    run(execute(GOOD_SCOPE, plan.plan_id, plan.plan_digest, cfg, runner=runner))
    assert seen["first"] == 0
    assert seen["second"] == 1, "the first worker's result was not committed before the second ran"


def test_transcript_and_usage_are_persisted(cfg, workspace):
    plan = make_plan(cfg, workspace, task_entry("a", reads=["README.md"]))
    run(execute(GOOD_SCOPE, plan.plan_id, plan.plan_digest, cfg, runner=recording_runner([])))
    conn = connect(cfg.db_path)
    try:
        run_row = conn.execute("SELECT * FROM runs").fetchone()
        res_row = conn.execute("SELECT * FROM results").fetchone()
    finally:
        conn.close()
    assert run_row["tokens_in"] == 1000 and run_row["status"] == "ok"
    assert run_row["wave_index"] == 0
    assert res_row["content_bytes"] > 0


# ------------------------------------------------------------- idempotency
def test_completed_tasks_are_skipped_on_re_execution(cfg, workspace):
    plan = make_plan(
        cfg, workspace,
        task_entry("a", reads=["README.md"]),
        task_entry("b", reads=["pkg/config.py"]),
    )
    first: list = []
    run(execute(GOOD_SCOPE, plan.plan_id, plan.plan_digest, cfg,
                runner=recording_runner(first, {"b": "failed"})))
    assert sorted(first) == ["a", "b"]

    second: list = []
    outcome = run(execute(GOOD_SCOPE, plan.plan_id, plan.plan_digest, cfg,
                          runner=recording_runner(second)))
    assert second == ["b"], "the completed task was re-run"
    assert outcome.skipped == ["a"]


# ---------------------------------------------------------------- collect
def test_collect_returns_finished_work(cfg, workspace):
    plan = make_plan(cfg, workspace, task_entry("a", reads=["README.md"]))
    run(execute(GOOD_SCOPE, plan.plan_id, plan.plan_digest, cfg, runner=recording_runner([])))
    rows = collect_plan(cfg, plan.plan_id)
    assert len(rows) == 1
    assert rows[0]["task_ref"] == "a" and rows[0]["status"] == "ok"
    assert rows[0]["summary"]


def test_collect_on_a_plan_that_never_ran_is_empty_not_an_error(cfg, workspace):
    plan = make_plan(cfg, workspace, task_entry("a", reads=["README.md"]))
    assert collect_plan(cfg, plan.plan_id) == []


def test_collect_on_unknown_plan_raises(cfg):
    with pytest.raises(PlanRefused):
        collect_plan(cfg, "nosuchplan")


def test_collect_distinguishes_timeout_from_failure(cfg, workspace):
    plan = make_plan(
        cfg, workspace,
        task_entry("slow", reads=["README.md"]),
        task_entry("broken", reads=["pkg/config.py"]),
    )
    run(execute(GOOD_SCOPE, plan.plan_id, plan.plan_digest, cfg,
                runner=recording_runner([], {"slow": "timeout", "broken": "failed"})))
    by_ref = {r["task_ref"]: r for r in collect_plan(cfg, plan.plan_id)}
    assert by_ref["slow"]["status"] == "timeout"
    assert by_ref["broken"]["status"] == "failed"
