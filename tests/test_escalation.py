"""One retry on the stronger model, and the cases where retrying is wrong.

The bench found `gemini-3.8-flash-low` fastest and no worse on tasks none of
the models failed, with escalation reserved for failures. What matters here is
mostly the *refusals*: every retry is another worker's worth of tokens and
another slice of a deadline that is already running.
"""

from __future__ import annotations

from dataclasses import replace

from conftest import make_task, task_entry
from test_execution import GOOD_SCOPE, make_plan, make_result, run
from test_parallel import timing_runner

from subagents.db import connect
from subagents.execution import execute, fits_remaining_deadline, should_escalate
from subagents.hashing import TaintReport


def model_recording_runner(calls: list, status_by_attempt: dict[str, list[str]]):
    """Records the model each attempt used and walks a scripted status list."""
    attempts: dict[str, int] = {}

    async def runner(task_ref, command, *, timeout_s, cwd, model, env=None):
        n = attempts.get(task_ref, 0)
        attempts[task_ref] = n + 1
        calls.append((task_ref, model))
        statuses = status_by_attempt.get(task_ref, ["ok"])
        status = statuses[min(n, len(statuses) - 1)]
        return make_result(task_ref, status)

    return runner


# ------------------------------------------------------------- the rule itself
def test_a_failure_escalates():
    assert should_escalate(make_result("a", "failed"), None)


def test_unparseable_output_escalates():
    """A different model may simply produce a well-formed answer."""
    assert should_escalate(make_result("a", "unparseable"), None)


def test_a_timeout_does_not_escalate():
    """It needed more time, not more reasoning. A retry buys another full
    budget against a client deadline that is already running down."""
    assert not should_escalate(make_result("a", "timeout"), None)


def test_a_spawn_error_does_not_escalate():
    """The binary is missing or unrunnable. No model changes that."""
    assert not should_escalate(make_result("a", "spawn_error"), None)


def test_a_tainted_run_does_not_escalate():
    """It already wrote somewhere it should not have. Running it again
    compounds the damage rather than correcting it."""
    tainted = TaintReport(task_ref="a", attribution="task", undeclared=["x"])
    assert tainted.tainted
    assert not should_escalate(make_result("a", "failed"), tainted)


def test_a_success_does_not_escalate():
    assert not should_escalate(make_result("a", "ok"), None)


# ------------------------------------------------------------ deadline maths
def test_a_retry_that_cannot_finish_in_time_is_refused(cfg):
    """Escalating into the last few seconds is the most likely way to turn a
    partial result into a cancelled one."""
    assert not fits_remaining_deadline(880.0, cfg, 900)


def test_a_retry_with_room_is_allowed(cfg):
    small = replace(cfg, worker_timeout_s=60)
    assert fits_remaining_deadline(10.0, small, 900)


def test_an_unknown_deadline_does_not_block_a_retry(cfg):
    """A client that never told us its limit gets the benefit of the doubt --
    propose_plan already warns separately when the plan will not fit."""
    assert fits_remaining_deadline(10_000.0, cfg, None)


# ---------------------------------------------------------- through execute()
def test_a_failed_worker_is_retried_on_the_escalation_model(cfg, workspace):
    plan = make_plan(cfg, workspace, task_entry("a", reads=["README.md"], writes=["a.txt"]))

    calls: list = []
    outcome = run(execute(GOOD_SCOPE, plan.plan_id, plan.plan_digest, cfg,
                          runner=model_recording_runner(calls, {"a": ["failed", "ok"]})))

    assert [model for _, model in calls] == [cfg.model, cfg.model_escalate]
    assert outcome.escalated == ["a"]
    assert outcome.outcome == "complete", "the retry succeeded, so the plan did"


def test_the_retry_replaces_the_failed_record_rather_than_adding_one(cfg, workspace):
    """One task, one row. Two would make collect() ambiguous about which
    attempt the summary belongs to."""
    plan = make_plan(cfg, workspace, task_entry("a", reads=["README.md"], writes=["a.txt"]))

    outcome = run(execute(GOOD_SCOPE, plan.plan_id, plan.plan_digest, cfg,
                          runner=model_recording_runner([], {"a": ["failed", "ok"]})))

    assert [r.task_ref for r in outcome.results] == ["a"]
    conn = connect(cfg.db_path)
    try:
        assert conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 1
        assert conn.execute("SELECT status FROM runs").fetchone()["status"] == "ok"
    finally:
        conn.close()


def test_escalation_happens_at_most_once(cfg, workspace):
    """A model that fails twice fails. Retrying in a loop is how a fan-out
    turns into unbounded spend."""
    plan = make_plan(cfg, workspace, task_entry("a", reads=["README.md"], writes=["a.txt"]))

    calls: list = []
    outcome = run(execute(GOOD_SCOPE, plan.plan_id, plan.plan_digest, cfg,
                          runner=model_recording_runner(calls, {"a": ["failed", "failed"]})))

    assert len(calls) == 2
    assert outcome.outcome == "partial"


def test_a_timeout_is_not_retried_through_execute(cfg, workspace):
    plan = make_plan(cfg, workspace, task_entry("a", reads=["README.md"], writes=["a.txt"]))

    calls: list = []
    run(execute(GOOD_SCOPE, plan.plan_id, plan.plan_digest, cfg,
                runner=model_recording_runner(calls, {"a": ["timeout", "ok"]})))

    assert len(calls) == 1, "a timed-out worker was retried"


def test_a_recovered_worker_unblocks_its_reader(cfg, workspace):
    """The point of escalating at all: the rest of the plan can proceed."""
    plan = make_plan(
        cfg, workspace,
        task_entry("writer", reads=["README.md"], writes=["pkg/config.py"]),
        task_entry("reader", reads=["pkg/config.py"], writes=["out.txt"]),
    )

    outcome = run(execute(GOOD_SCOPE, plan.plan_id, plan.plan_digest, cfg,
                          runner=model_recording_runner([], {"writer": ["failed", "ok"]})))

    assert outcome.blocked == []
    assert outcome.outcome == "complete"
    assert {r.task_ref for r in outcome.results} == {"writer", "reader"}


def test_escalation_is_skipped_when_the_deadline_cannot_fit_it(cfg, workspace):
    """Arithmetic on a real number read from the client config, not optimism."""
    tight = replace(cfg, worker_timeout_s=600)
    plan = make_plan(tight, workspace, task_entry("a", reads=["README.md"], writes=["a.txt"]))

    calls: list = []
    outcome = run(execute(GOOD_SCOPE, plan.plan_id, plan.plan_digest, tight,
                          runner=model_recording_runner(calls, {"a": ["failed", "ok"]}),
                          deadline_s=60))

    assert len(calls) == 1, "escalated with no room left in the deadline"
    assert outcome.outcome == "partial"


def test_a_clean_plan_never_escalates(cfg, workspace):
    plan = make_plan(cfg, workspace, task_entry("a", reads=["README.md"], writes=["a.txt"]))
    outcome = run(execute(GOOD_SCOPE, plan.plan_id, plan.plan_digest, cfg,
                          runner=timing_runner([])))
    assert outcome.escalated == []


# ------------------------------------------------------------- rate limiting
def test_a_rate_limited_worker_is_waited_out_not_escalated(cfg, workspace, monkeypatch):
    """A 429 is the provider pushing back, not a task the model got wrong.
    Escalating would send a MORE expensive request at a quota that just said no.
    """
    import subagents.execution as execution

    monkeypatch.setattr(execution, "RATE_LIMIT_BACKOFF_S", 0)
    plan = make_plan(cfg, workspace, task_entry("a", reads=["README.md"], writes=["a.txt"]))

    calls: list = []

    async def runner(task_ref, command, *, timeout_s, cwd, model, env=None):
        calls.append(model)
        if len(calls) == 1:
            return make_result(task_ref, "failed", exit_reason="rate_limited (RESOURCE_EXHAUSTED)")
        return make_result(task_ref, "ok")

    outcome = run(execute(GOOD_SCOPE, plan.plan_id, plan.plan_digest, cfg, runner=runner))

    assert calls == [cfg.model, cfg.model], "the retry used the escalation model"
    assert outcome.escalated == []
    assert outcome.outcome == "complete"


def test_rate_limit_retries_are_bounded(cfg, workspace, monkeypatch):
    """Retrying a quota forever is how a fan-out becomes unbounded spend."""
    import subagents.execution as execution

    monkeypatch.setattr(execution, "RATE_LIMIT_BACKOFF_S", 0)
    plan = make_plan(cfg, workspace, task_entry("a", reads=["README.md"], writes=["a.txt"]))

    calls: list = []

    async def runner(task_ref, command, *, timeout_s, cwd, model, env=None):
        calls.append(model)
        return make_result(task_ref, "failed", exit_reason="rate_limited (RESOURCE_EXHAUSTED)")

    outcome = run(execute(GOOD_SCOPE, plan.plan_id, plan.plan_digest, cfg, runner=runner))

    assert len(calls) == execution.RATE_LIMIT_ATTEMPTS
    assert outcome.outcome == "partial"


def test_a_rate_limit_is_not_mistaken_for_a_model_failure():
    """Read off the classified exit_reason, never a grep of the transcript --
    bench defect D2 was a 429 inside a conversation_id voiding a good run."""
    from subagents.worker import is_rate_limited

    assert is_rate_limited(make_result("a", "failed",
                                       exit_reason="rate_limited (RESOURCE_EXHAUSTED)"))
    assert not is_rate_limited(make_result("a", "failed", exit_reason="agy status ERROR"))
    assert not is_rate_limited(make_result("a", "ok"))


def test_an_exhausted_rate_limit_does_not_then_escalate():
    """Caught by the bounded-retry test: without this rule a worker that hit a
    quota three times answered by sending a MORE expensive request at the same
    quota. Six attempts where the design says three."""
    limited = make_result("a", "failed", exit_reason="rate_limited (RESOURCE_EXHAUSTED)")
    assert not should_escalate(limited, None)
