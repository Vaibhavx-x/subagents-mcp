"""Taint as it lands in the database, not as the hashing module computes it.

test_hashing.py proves the comparison is right. This proves the verdict
survives the round trip through a real execution and is readable afterwards --
which is what anyone auditing a run will actually look at.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from conftest import task_entry
from test_execution import GOOD_SCOPE, make_plan, make_result, run

from subagents.db import connect
from subagents.execution import execute


def writing_runner(writes: dict[str, list[tuple[str, str]]], status_for=None):
    """A worker that actually touches the filesystem, the way a real one does.

    `writes` maps task_ref -> [(relative path, content)], including paths the
    task never declared -- which is the whole point.
    """
    async def runner(task_ref, command, *, timeout_s, cwd, model, env=None):
        for rel, content in writes.get(task_ref, []):
            target = Path(cwd) / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
        return make_result(task_ref, (status_for or {}).get(task_ref, "ok"))
    return runner


def run_row(cfg, task_ref: str):
    conn = connect(cfg.db_path)
    try:
        return conn.execute(
            "SELECT status, tainted, tainted_paths FROM runs WHERE task_ref = ?",
            (task_ref,),
        ).fetchone()
    finally:
        conn.close()


def test_an_in_scope_worker_is_recorded_clean(cfg, workspace):
    plan = make_plan(cfg, workspace, task_entry("a", reads=["README.md"], writes=["a.txt"]))

    outcome = run(execute(GOOD_SCOPE, plan.plan_id, plan.plan_digest, cfg,
                          runner=writing_runner({"a": [("a.txt", "done\n")]})))

    assert outcome.tainted_refs == []
    row = run_row(cfg, "a")
    assert row["tainted"] == 0 and row["tainted_paths"] is None


def test_an_undeclared_write_is_recorded_as_taint_with_its_path(cfg, workspace):
    """The failure this project exists to catch: the worker did something
    nobody approved, and said it succeeded."""
    plan = make_plan(cfg, workspace, task_entry("a", reads=["README.md"], writes=["a.txt"]))

    outcome = run(execute(
        GOOD_SCOPE, plan.plan_id, plan.plan_digest, cfg,
        runner=writing_runner({"a": [("a.txt", "done\n"), ("pkg/sneaky.py", "oops\n")]}),
    ))

    assert outcome.tainted_refs == ["a"]
    row = run_row(cfg, "a")
    assert row["tainted"] == 1
    assert row["status"] == "ok", "taint is recorded alongside the status, not instead of it"

    paths = json.loads(row["tainted_paths"])
    assert any(path.endswith(os.path.normcase("pkg\\sneaky.py").replace("\\", os.sep))
               or "sneaky" in path for path in paths)


def test_a_worker_that_edits_a_file_it_declared_read_only_is_tainted(cfg, workspace):
    plan = make_plan(cfg, workspace,
                     task_entry("a", reads=["pkg/config.py"], writes=["a.txt"]))

    outcome = run(execute(
        GOOD_SCOPE, plan.plan_id, plan.plan_digest, cfg,
        runner=writing_runner({"a": [("a.txt", "x\n"), ("pkg/config.py", "rewritten\n")]}),
    ))

    assert outcome.tainted_refs == ["a"]
    assert "read-only" in outcome.taints["a"].describe()


def test_taint_does_not_fire_on_the_servers_own_database_churn(cfg, workspace):
    """The run writes subagents.db and subagents.log while it is happening. If
    those counted, every run would be tainted and the signal would be worth
    nothing."""
    from dataclasses import replace

    local = replace(cfg, db_path=workspace / "subagents.db",
                    log_file=workspace / "subagents.log")
    plan = make_plan(local, workspace, task_entry("a", reads=["README.md"], writes=["a.txt"]))

    outcome = run(execute(GOOD_SCOPE, plan.plan_id, plan.plan_digest, local,
                          runner=writing_runner({"a": [("a.txt", "done\n")]})))

    assert outcome.tainted_refs == [], outcome.taints["a"].describe()


def test_a_tainted_sibling_does_not_taint_a_clean_task(cfg, workspace):
    """Under parallelism an undeclared change belongs to the wave, and the
    report says so -- but a path a sibling legitimately declared is still not
    charged to anyone."""
    plan = make_plan(
        cfg, workspace,
        task_entry("a", reads=["README.md"], writes=["a.txt"]),
        task_entry("b", reads=["README.md"], writes=["b.txt"]),
    )

    outcome = run(execute(
        GOOD_SCOPE, plan.plan_id, plan.plan_digest, cfg,
        runner=writing_runner({"a": [("a.txt", "x\n")], "b": [("b.txt", "y\n")]}),
    ))

    assert outcome.tainted_refs == []
    assert outcome.taints["a"].attribution == "wave"


def test_a_single_task_wave_gets_task_level_attribution(cfg, workspace):
    """With one worker there is exactly one possible author, and the report
    should not hedge about it."""
    plan = make_plan(cfg, workspace, task_entry("a", reads=["README.md"], writes=["a.txt"]))

    outcome = run(execute(GOOD_SCOPE, plan.plan_id, plan.plan_digest, cfg,
                          runner=writing_runner({"a": [("a.txt", "x\n"), ("stray.txt", "?\n")]})))

    report = outcome.taints["a"]
    assert report.attribution == "task"
    assert "by this task" in report.describe()


def test_a_failed_worker_still_gets_a_taint_verdict(cfg, workspace):
    """A worker that failed may still have written something first -- that is
    precisely when you want to know what it touched."""
    plan = make_plan(cfg, workspace, task_entry("a", reads=["README.md"], writes=["a.txt"]))

    outcome = run(execute(
        GOOD_SCOPE, plan.plan_id, plan.plan_digest, cfg,
        runner=writing_runner({"a": [("stray.txt", "half done\n")]},
                              status_for={"a": "timeout"}),
    ))

    assert outcome.tainted_refs == ["a"]
    assert run_row(cfg, "a")["tainted"] == 1


def test_an_escalation_retry_is_also_taint_checked(cfg, workspace):
    """The wave's snapshot closes before escalation runs.

    Without a snapshot pair around the retry, a worker that failed and then
    wrote undeclared files on its second attempt would be reported clean --
    a hole in the only control that survives the human approval.
    """
    from test_execution import make_result

    plan = make_plan(cfg, workspace, task_entry("a", reads=["README.md"], writes=["a.txt"]))
    attempts = {"n": 0}

    async def runner(task_ref, command, *, timeout_s, cwd, model, env=None):
        attempts["n"] += 1
        if attempts["n"] == 1:
            return make_result(task_ref, "failed")
        # The retry succeeds -- and strays.
        (Path(cwd) / "a.txt").write_text("done\n", encoding="utf-8")
        (Path(cwd) / "stray_on_retry.txt").write_text("oops\n", encoding="utf-8")
        return make_result(task_ref, "ok")

    outcome = run(execute(GOOD_SCOPE, plan.plan_id, plan.plan_digest, cfg, runner=runner))

    assert attempts["n"] == 2, "the failure was not escalated"
    assert outcome.tainted_refs == ["a"], "the retry's undeclared write went unnoticed"
    assert any("stray_on_retry" in p for p in outcome.taints["a"].paths)
    assert run_row(cfg, "a")["tainted"] == 1


def test_a_clean_escalation_retry_clears_the_earlier_verdict(cfg, workspace):
    """The retry replaces the attempt, so its verdict must replace the old one
    rather than leaving a stale taint attached to a run that did not cause it."""
    from test_execution import make_result

    plan = make_plan(cfg, workspace, task_entry("a", reads=["README.md"], writes=["a.txt"]))
    attempts = {"n": 0}

    async def runner(task_ref, command, *, timeout_s, cwd, model, env=None):
        attempts["n"] += 1
        if attempts["n"] == 1:
            return make_result(task_ref, "unparseable")
        (Path(cwd) / "a.txt").write_text("done\n", encoding="utf-8")
        return make_result(task_ref, "ok")

    outcome = run(execute(GOOD_SCOPE, plan.plan_id, plan.plan_digest, cfg, runner=runner))

    assert outcome.tainted_refs == []
    assert outcome.taints["a"].attribution == "task", "a retry runs alone"
