"""The cache inside a real execution, and the things it must not break.

test_cache.py proves the key and the validity check. This proves the wave loop
uses them without disturbing anything the previous four phases established:
blocking, taint, escalation, `collect`, the estimator, and the honesty of the
counters.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from conftest import task_entry
from test_execution import GOOD_SCOPE, make_plan, make_result, recording_runner, run
from test_taint_execution import writing_runner

from subagents import cache, execution
from subagents.db import completed_worker_durations, connect
from subagents.execution import collect_plan, execute
from subagents.rendering import render_execution

DOC = task_entry("a", reads=["README.md"], writes=["a.txt"])


def warm(cfg, workspace, *entries, writes=None):
    """Execute once for real, so the work is on record."""
    entries = entries or (DOC,)
    plan = make_plan(cfg, workspace, *entries)
    outcome = run(execute(GOOD_SCOPE, plan.plan_id, plan.plan_digest, cfg,
                          runner=writing_runner(writes or {"a": [("a.txt", "done\n")]})))
    assert outcome.outcome == "complete", outcome.outcome
    return plan


def row(cfg, plan_id, task_ref):
    conn = connect(cfg.db_path)
    try:
        return conn.execute(
            "SELECT r.*, res.summary FROM runs r LEFT JOIN results res ON res.run_id = r.id"
            " WHERE r.plan_id = ? AND r.task_ref = ?", (plan_id, task_ref)
        ).fetchone()
    finally:
        conn.close()


# -------------------------------------------------------------- the actual case
def test_the_same_work_re_proposed_under_a_new_plan_id_is_not_run_again(cfg, workspace):
    """The loop the cache exists for. A plan cancelled at the deadline, or
    tainted, comes back as a NEW plan id -- so `_already_complete`, which is
    scoped to one plan, cannot help and every finished task is spawned again.
    """
    warm(cfg, workspace)

    again = make_plan(cfg, workspace, DOC)
    spawned: list[str] = []
    outcome = run(execute(GOOD_SCOPE, again.plan_id, again.plan_digest, cfg,
                          runner=recording_runner(spawned)))

    assert spawned == [], "a worker was spawned for work already on record"
    assert set(outcome.cached) == {"a"}
    assert outcome.outcome == "complete"


def test_a_cache_hit_is_recorded_as_a_readable_run(cfg, workspace):
    warm(cfg, workspace)
    again = make_plan(cfg, workspace, DOC)
    run(execute(GOOD_SCOPE, again.plan_id, again.plan_digest, cfg,
                runner=recording_runner([])))

    stored = row(cfg, again.plan_id, "a")
    assert stored["status"] == "ok"
    assert "cache hit" in stored["exit_reason"]
    assert stored["summary"], "the cached transcript's summary came across"


def test_a_cache_hit_records_zero_tokens_not_the_original_runs(cfg, workspace):
    """Copying the source run's counts would inflate every total in this
    database with spend that did not happen. Zero is a measurement."""
    warm(cfg, workspace)
    again = make_plan(cfg, workspace, DOC)
    run(execute(GOOD_SCOPE, again.plan_id, again.plan_digest, cfg,
                runner=recording_runner([])))

    stored = row(cfg, again.plan_id, "a")
    assert stored["tokens_in"] == 0 and stored["tokens_out"] == 0
    assert stored["thinking_tokens"] == 0 and stored["cache_read_tokens"] == 0


def test_a_cache_hit_does_not_feed_the_p90_estimator(cfg, workspace):
    """A cached task has no duration. Writing "now to now" would push a stream
    of ~0s samples into `completed_worker_durations` and drag every future
    estimate toward zero -- the estimator would end up predicting how fast a
    lookup is rather than how long work takes."""
    warm(cfg, workspace)
    before = completed_worker_durations(cfg.db_path)

    for _ in range(5):
        again = make_plan(cfg, workspace, DOC)
        run(execute(GOOD_SCOPE, again.plan_id, again.plan_digest, cfg,
                    runner=recording_runner([])))

    assert completed_worker_durations(cfg.db_path) == before


# ------------------------------------------------------------------- no taint
def test_a_cache_hit_gets_no_taint_verdict_at_all(cfg, workspace):
    """Not a clean one. Nothing ran, so nothing was observed, and reporting
    "no undeclared changes" for a task that never executed would be a false
    assurance of exactly the kind this project refuses everywhere else."""
    warm(cfg, workspace)
    again = make_plan(cfg, workspace, DOC)
    outcome = run(execute(GOOD_SCOPE, again.plan_id, again.plan_digest, cfg,
                          runner=recording_runner([])))

    assert "a" not in outcome.taints
    assert outcome.tainted_refs == []
    assert row(cfg, again.plan_id, "a")["tainted"] == 0


def test_a_wave_of_nothing_but_hits_takes_no_snapshot(cfg, workspace, monkeypatch):
    """Hashing a whole tree costs real time. If nothing is going to run, there
    is nothing to compare and the walk should not happen."""
    warm(cfg, workspace)

    def explode(*a, **kw):
        raise AssertionError("snapshot taken for a wave in which nothing ran")

    again = make_plan(cfg, workspace, DOC)
    monkeypatch.setattr(execution, "snapshot", explode)
    outcome = run(execute(GOOD_SCOPE, again.plan_id, again.plan_digest, cfg,
                          runner=recording_runner([])))
    assert set(outcome.cached) == {"a"}


# ----------------------------------------------------------- dependencies hold
def test_a_cached_writer_does_not_block_its_reader(cfg, workspace):
    """A hit verified every declared write is already in place before serving,
    so a downstream reader has real state to read. Blocking it would be wrong
    and would make the cache useless on any multi-wave plan."""
    writer = task_entry("writer", reads=["README.md"], writes=["mid.txt"])
    reader = task_entry("reader", reads=["mid.txt"], writes=["final.txt"])

    first = make_plan(cfg, workspace, writer, reader)
    run(execute(GOOD_SCOPE, first.plan_id, first.plan_digest, cfg,
                runner=writing_runner({"writer": [("mid.txt", "middle\n")],
                                       "reader": [("final.txt", "end\n")]})))

    again = make_plan(cfg, workspace, writer, reader)
    spawned: list[str] = []
    outcome = run(execute(GOOD_SCOPE, again.plan_id, again.plan_digest, cfg,
                          runner=recording_runner(spawned)))

    assert outcome.blocked == [], "a cached writer blocked its reader"
    assert set(outcome.cached) == {"writer", "reader"}
    assert spawned == []


def test_a_failed_upstream_still_blocks_even_when_the_task_is_cached(cfg, workspace):
    """Blocking is checked BEFORE the cache, and must stay that way. The
    cached run saw the state its writer produced last time; if the writer has
    just failed, that state is not what a reader would find now."""
    writer = task_entry("writer", reads=["README.md"], writes=["mid.txt"])
    reader = task_entry("reader", reads=["mid.txt"], writes=["final.txt"])

    first = make_plan(cfg, workspace, writer, reader)
    run(execute(GOOD_SCOPE, first.plan_id, first.plan_digest, cfg,
                runner=writing_runner({"writer": [("mid.txt", "middle\n")],
                                       "reader": [("final.txt", "end\n")]})))

    # Now the writer's input changes, so only the writer misses -- and it fails.
    (workspace / "README.md").write_text("# changed\n", encoding="utf-8")
    again = make_plan(cfg, workspace, writer, reader)
    outcome = run(execute(GOOD_SCOPE, again.plan_id, again.plan_digest, cfg,
                          runner=recording_runner([], {"writer": "failed"})))

    assert outcome.blocked == [("reader", "writer")]
    assert "reader" not in outcome.cached


# ---------------------------------------------------------------- the reporting
def test_the_report_separates_cached_tasks_from_workers(cfg, workspace):
    other = task_entry("b", reads=["test_config.py"], writes=["b.txt"])
    warm(cfg, workspace)

    again = make_plan(cfg, workspace, DOC, other)
    outcome = run(execute(GOOD_SCOPE, again.plan_id, again.plan_digest, cfg,
                          runner=writing_runner({"b": [("b.txt", "beta\n")]})))

    text = render_execution(outcome)
    assert "CACHED" in text
    assert "1 served from cache" in text
    assert "[a] cache hit" in text
    assert "[b] ok" in text, "the task that really ran is still reported as a worker"


def test_collect_shows_a_cached_run_for_what_it_is(cfg, workspace):
    warm(cfg, workspace)
    again = make_plan(cfg, workspace, DOC)
    run(execute(GOOD_SCOPE, again.plan_id, again.plan_digest, cfg,
                runner=recording_runner([])))

    rows = collect_plan(cfg, again.plan_id)
    assert len(rows) == 1
    assert rows[0]["status"] == "ok"
    assert "cache hit" in rows[0]["exit_reason"]


def test_the_execution_row_does_not_count_cached_tasks_as_workers_started(cfg, workspace):
    """`workers_started` is an audit number. A lookup is not a worker."""
    warm(cfg, workspace)
    again = make_plan(cfg, workspace, DOC)
    run(execute(GOOD_SCOPE, again.plan_id, again.plan_digest, cfg,
                runner=recording_runner([])))

    conn = connect(cfg.db_path)
    try:
        exec_row = conn.execute(
            "SELECT workers_started, workers_completed FROM executions WHERE plan_id = ?",
            (again.plan_id,),
        ).fetchone()
    finally:
        conn.close()
    assert exec_row["workers_started"] == 0
    assert exec_row["workers_completed"] == 0


# ------------------------------------------------------------------- the forecast
def test_propose_plan_forecasts_the_hits_and_labels_it_a_forecast(cfg, workspace):
    from subagents.planning import render

    warm(cfg, workspace)
    again = make_plan(cfg, workspace, DOC)

    assert again.cached_refs == ("a",)
    text = render(again)
    assert "cache          : 1 of 1 task(s) already done" in text
    assert "forecast only" in text
    assert "CACHED, will not run" in text


def test_a_cold_plan_says_nothing_about_the_cache(cfg, workspace):
    from subagents.planning import render

    plan = make_plan(cfg, workspace, DOC)
    assert plan.cached_refs == ()
    assert "cache " not in render(plan)


def test_the_estimate_covers_only_what_will_actually_spawn(cfg, workspace):
    """A plan that will run one worker must not warn about a deadline for two.

    The wall-clock figure only moves when the cached task crossed a batch
    boundary -- two workers under max_parallel=4 run at once and cost the same
    wall clock as one, so `worker_count` is the number that always reflects it.
    """
    other = task_entry("b", reads=["test_config.py"], writes=["b.txt"])
    cold = make_plan(cfg, workspace, DOC, other)
    assert cold.estimate.worker_count == 2

    warm(cfg, workspace)
    warm_plan = make_plan(cfg, workspace, DOC, other)
    assert warm_plan.estimate.worker_count == 1
    assert warm_plan.estimate.expected_s == cold.estimate.expected_s, (
        "same batch, same wall clock -- dropping a parallel worker saves no time"
    )


def test_the_estimate_shortens_when_a_cached_task_crossed_a_batch_boundary(cfg, workspace):
    """Serialised, the saving is real and the estimate has to show it."""
    serial = replace(cfg, max_parallel=1)
    other = task_entry("b", reads=["test_config.py"], writes=["b.txt"])
    cold = make_plan(serial, workspace, DOC, other)

    warm(serial, workspace)
    warm_plan = make_plan(serial, workspace, DOC, other)
    assert warm_plan.estimate.expected_s < cold.estimate.expected_s
    assert warm_plan.estimate.worst_case_s < cold.estimate.worst_case_s


def test_the_plan_digest_is_unchanged_by_a_warm_cache(cfg, workspace):
    """Otherwise a cache warming up between propose and execute would
    invalidate the approval the human just gave."""
    cold = make_plan(cfg, workspace, DOC)
    warm(cfg, workspace)
    assert make_plan(cfg, workspace, DOC).plan_digest == cold.plan_digest


def test_disabling_the_cache_restores_the_old_behaviour_exactly(cfg, workspace):
    warm(cfg, workspace)
    off = replace(cfg, cache_ttl_s=0)

    again = make_plan(off, workspace, DOC)
    spawned: list[str] = []
    outcome = run(execute(GOOD_SCOPE, again.plan_id, again.plan_digest, off,
                          runner=recording_runner(spawned)))

    assert again.cached_refs == ()
    assert spawned == ["a"]
    assert outcome.cached == {}
