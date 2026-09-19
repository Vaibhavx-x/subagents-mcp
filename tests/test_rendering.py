"""What the parent actually reads.

Detection is worthless if the verdict never reaches the human, and a summary
that quietly grows into a transcript defeats the project's whole argument. Both
are pinned here.
"""

from __future__ import annotations

import json

from test_execution import make_result

from subagents.execution import ExecutionOutcome
from subagents.hashing import TaintReport
from subagents.rendering import render_collection, render_execution


def outcome(**kw) -> ExecutionOutcome:
    base = dict(plan_id="p1", outcome="complete", results=[make_result("a")])
    base.update(kw)
    return ExecutionOutcome(**base)


# ------------------------------------------------------------------- taint
def test_a_taint_verdict_names_the_paths():
    """'1 path changed' tells a human nothing they can act on."""
    report = TaintReport(task_ref="a", attribution="task",
                         undeclared=["d:\\ws\\pkg\\sneaky.py"])
    text = render_execution(outcome(taints={"a": report}))

    assert "TAINTED" in text
    assert "sneaky.py" in text
    assert "REVIEW BEFORE TRUSTING" in text


def test_the_taint_notice_says_detected_not_prevented():
    """There is no containment. Wording that implies otherwise would be the
    overclaim this project has already corrected twice."""
    report = TaintReport(task_ref="a", attribution="task", undeclared=["x"])
    text = render_execution(outcome(taints={"a": report}))
    assert "detected, not prevented" in text


def test_a_clean_run_says_nothing_about_taint():
    text = render_execution(outcome(taints={"a": TaintReport("a", "task")}))
    assert "TAINTED" not in text
    assert "All workers completed" in text


def test_only_the_first_few_paths_are_listed():
    """A worker that touched two hundred files must not paste them into the
    parent's context -- that is the cost this project exists to avoid."""
    report = TaintReport(task_ref="a", attribution="wave",
                         undeclared=[f"p{i}" for i in range(50)])
    text = render_execution(outcome(taints={"a": report}))
    assert "and 45 more" in text
    assert text.count("\n") < 40


# ----------------------------------------------------------------- blocked
def test_blocked_tasks_are_listed_with_their_upstream():
    text = render_execution(outcome(outcome="partial", blocked=[("reader", "writer")]))
    assert "BLOCKED (never started)" in text
    assert "depends on writer" in text
    assert "NOT COMPLETE: reader" in text


def test_a_blocked_task_counts_as_not_complete_even_when_every_worker_passed():
    """The plan did not do what was approved, and saying 'all workers
    completed' would be true and misleading at once."""
    text = render_execution(outcome(outcome="partial", blocked=[("reader", "writer")]))
    assert "All workers completed" not in text


# ------------------------------------------------------------- cancellation
def test_a_cancelled_run_points_at_collect_rather_than_a_retry():
    """Re-running would redo finished work and spend the tokens twice."""
    text = render_execution(outcome(outcome="cancelled_at_deadline"))
    assert "CANCELLED" in text
    assert "collect(p1)" in text
    assert "rather than re-running" in text


def test_escalation_is_visible_in_the_output():
    text = render_execution(outcome(escalated=["a"]))
    assert "escalation model" in text


# ------------------------------------------------------------------ collect
def test_collect_surfaces_stored_taint():
    rows = [{
        "task_ref": "a", "wave_index": 0, "status": "ok", "exit_reason": "success",
        "tokens_in": 10, "tokens_out": 2, "summary": "did it", "content_bytes": 100,
        "tainted": 1, "tainted_paths": json.dumps(["d:\\ws\\stray.txt"]),
    }]
    text = render_collection("p1", rows)
    assert "TAINTED" in text and "stray.txt" in text


def test_collect_of_a_blocked_task_explains_itself():
    rows = [{
        "task_ref": "reader", "wave_index": 1, "status": "blocked",
        "exit_reason": "not run: depends on writer, which did not succeed",
        "tokens_in": None, "tokens_out": None, "summary": None, "content_bytes": None,
        "tainted": 0, "tainted_paths": None,
    }]
    text = render_collection("p1", rows)
    assert "never started" in text
    assert "depends on writer" in text
