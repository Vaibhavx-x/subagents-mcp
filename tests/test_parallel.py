"""Fan-out: concurrency inside a wave, sequence between them, and what
happens when a worker fails or the whole call is cancelled.

Concurrency is asserted from recorded start/finish times rather than from a
counter a mock incremented, because the thing being tested is whether the
workers actually overlapped -- not whether the code intended them to.
"""

from __future__ import annotations

import asyncio
import time

import pytest
from conftest import task_entry
from test_execution import GOOD_SCOPE, make_plan, make_result, run

from subagents.db import connect
from subagents.execution import execute


def timing_runner(events: list, *, delay: float = 0.08, status_for=None, hang=()):
    """Records when each worker starts and stops, so overlap is measurable."""
    async def runner(task_ref, command, *, timeout_s, cwd, model, env=None):
        events.append(("start", task_ref, time.monotonic()))
        try:
            if task_ref in hang:
                await asyncio.sleep(3600)
            await asyncio.sleep(delay)
        finally:
            events.append(("end", task_ref, time.monotonic()))
        return make_result(task_ref, (status_for or {}).get(task_ref, "ok"))
    return runner


def peak_concurrency(events: list) -> int:
    """Maximum number of workers alive at the same instant."""
    timeline = sorted(((t, +1 if kind == "start" else -1) for kind, _, t in events),
                      key=lambda e: (e[0], -e[1]))
    live = peak = 0
    for _, delta in timeline:
        live += delta
        peak = max(peak, live)
    return peak


def independent(*refs) -> list[dict]:
    """Tasks with no read/write overlap, so they land in one wave."""
    return [task_entry(ref, reads=["README.md"], writes=[f"out-{ref}.txt"]) for ref in refs]


# ------------------------------------------------------------- real overlap
def test_workers_in_a_wave_actually_run_concurrently(cfg, workspace):
    plan = make_plan(cfg, workspace, *independent("a", "b", "c", "d"))
    assert len(plan.waves) == 1

    events: list = []
    run(execute(GOOD_SCOPE, plan.plan_id, plan.plan_digest, cfg,
                runner=timing_runner(events)))

    assert peak_concurrency(events) > 1, "the wave ran sequentially"


def test_a_wave_is_faster_than_the_sum_of_its_parts(cfg, workspace):
    """The entire justification for the phase, stated as wall clock."""
    plan = make_plan(cfg, workspace, *independent("a", "b", "c", "d"))
    delay = 0.15

    started = time.monotonic()
    run(execute(GOOD_SCOPE, plan.plan_id, plan.plan_digest, cfg,
                runner=timing_runner([], delay=delay)))
    elapsed = time.monotonic() - started

    assert elapsed < 4 * delay, f"took {elapsed:.2f}s; 4 workers ran end to end"


def test_concurrency_never_exceeds_max_parallel(cfg, workspace):
    """The ceiling is the only thing standing between a fan-out and a burst of
    six-figure-token workers hitting the provider at once."""
    from dataclasses import replace

    capped = replace(cfg, max_parallel=2)
    plan = make_plan(capped, workspace, *independent("a", "b", "c", "d", "e"))

    events: list = []
    run(execute(GOOD_SCOPE, plan.plan_id, plan.plan_digest, capped,
                runner=timing_runner(events)))

    assert peak_concurrency(events) <= 2


def test_max_parallel_of_one_is_still_sequential(cfg, workspace):
    from dataclasses import replace

    serial = replace(cfg, max_parallel=1)
    plan = make_plan(serial, workspace, *independent("a", "b", "c"))

    events: list = []
    run(execute(GOOD_SCOPE, plan.plan_id, plan.plan_digest, serial,
                runner=timing_runner(events)))

    assert peak_concurrency(events) == 1


def test_every_result_lands_under_concurrency(cfg, workspace):
    """The first genuine exercise of db.write_transaction: five workers
    finishing at once. A dropped row would not raise -- it would silently
    shorten collect()."""
    refs = ["a", "b", "c", "d", "e"]
    plan = make_plan(cfg, workspace, *independent(*refs))

    outcome = run(execute(GOOD_SCOPE, plan.plan_id, plan.plan_digest, cfg,
                          runner=timing_runner([])))

    assert outcome.outcome == "complete"
    conn = connect(cfg.db_path)
    try:
        stored = {r["task_ref"] for r in conn.execute(
            "SELECT task_ref FROM runs WHERE plan_id = ?", (plan.plan_id,))}
        assert stored == set(refs)
        assert conn.execute("SELECT COUNT(*) FROM results").fetchone()[0] == len(refs)
    finally:
        conn.close()


# ------------------------------------------------------------ wave sequencing
def test_a_later_wave_never_starts_before_the_earlier_one_has_exited(cfg, workspace):
    """The writer-before-reader contract. A reader that overlaps its writer
    sees a half-written file and cannot tell."""
    plan = make_plan(
        cfg, workspace,
        task_entry("writer", reads=["README.md"], writes=["pkg/config.py"]),
        task_entry("reader", reads=["pkg/config.py"], writes=["out.txt"]),
    )
    assert len(plan.waves) == 2

    events: list = []
    run(execute(GOOD_SCOPE, plan.plan_id, plan.plan_digest, cfg,
                runner=timing_runner(events)))

    writer_end = next(t for kind, ref, t in events if kind == "end" and ref == "writer")
    reader_start = next(t for kind, ref, t in events if kind == "start" and ref == "reader")
    assert reader_start >= writer_end


# --------------------------------------------------------- blocked propagation
def test_a_failed_writer_blocks_its_reader(cfg, workspace):
    """Otherwise the reader reads stale state and reports success on it, which
    is worse than failing: the parent gets a confident wrong answer."""
    plan = make_plan(
        cfg, workspace,
        task_entry("writer", reads=["README.md"], writes=["pkg/config.py"]),
        task_entry("reader", reads=["pkg/config.py"], writes=["out.txt"]),
    )

    events: list = []
    outcome = run(execute(GOOD_SCOPE, plan.plan_id, plan.plan_digest, cfg,
                          runner=timing_runner(events, status_for={"writer": "failed"})))

    assert outcome.blocked == [("reader", "writer")]
    assert not any(ref == "reader" for _, ref, _ in events), "the reader was spawned"
    assert outcome.outcome == "partial"


def test_a_blocked_task_is_recorded_with_its_reason(cfg, workspace):
    """Silence would be indistinguishable from 'not started yet'."""
    plan = make_plan(
        cfg, workspace,
        task_entry("writer", reads=["README.md"], writes=["pkg/config.py"]),
        task_entry("reader", reads=["pkg/config.py"], writes=["out.txt"]),
    )
    run(execute(GOOD_SCOPE, plan.plan_id, plan.plan_digest, cfg,
                runner=timing_runner([], status_for={"writer": "failed"})))

    conn = connect(cfg.db_path)
    try:
        row = conn.execute(
            "SELECT status, exit_reason FROM runs WHERE task_ref = 'reader'").fetchone()
    finally:
        conn.close()
    assert row["status"] == "blocked"
    assert "writer" in row["exit_reason"]


def test_an_unrelated_task_still_runs_when_another_fails(cfg, workspace):
    """Blocking must follow the dependency graph, not punish the whole plan."""
    plan = make_plan(
        cfg, workspace,
        task_entry("writer", reads=["README.md"], writes=["pkg/config.py"]),
        task_entry("reader", reads=["pkg/config.py"], writes=["out.txt"]),
        task_entry("loner", reads=["test_config.py"], writes=["other.txt"]),
    )

    events: list = []
    outcome = run(execute(GOOD_SCOPE, plan.plan_id, plan.plan_digest, cfg,
                          runner=timing_runner(events, status_for={"writer": "timeout"})))

    assert ("reader", "writer") in outcome.blocked
    assert any(ref == "loner" for kind, ref, _ in events if kind == "start")


# ------------------------------------------------------------- cancellation
def test_cancelling_keeps_what_finished_and_records_the_outcome(cfg, workspace):
    """A cancelled call is the graceful-degradation path, not a lost run."""
    plan = make_plan(
        cfg, workspace,
        task_entry("quick", reads=["README.md"], writes=["a.txt"]),
        task_entry("stuck", reads=["README.md"], writes=["b.txt"]),
    )

    async def main():
        task = asyncio.ensure_future(
            execute(GOOD_SCOPE, plan.plan_id, plan.plan_digest, cfg,
                    runner=timing_runner([], delay=0.05, hang=("stuck",)))
        )
        await asyncio.sleep(0.4)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(main())

    conn = connect(cfg.db_path)
    try:
        finished = {r["task_ref"] for r in conn.execute(
            "SELECT task_ref FROM runs WHERE status = 'ok'")}
        outcome = conn.execute(
            "SELECT outcome FROM executions WHERE plan_id = ?", (plan.plan_id,)
        ).fetchone()["outcome"]
    finally:
        conn.close()

    assert finished == {"quick"}, "a finished worker's result was lost"
    assert outcome == "cancelled_at_deadline"


def test_collect_after_a_cancellation_returns_the_finished_work(cfg, workspace):
    from subagents.execution import collect_plan

    plan = make_plan(
        cfg, workspace,
        task_entry("quick", reads=["README.md"], writes=["a.txt"]),
        task_entry("stuck", reads=["README.md"], writes=["b.txt"]),
    )

    async def main():
        task = asyncio.ensure_future(
            execute(GOOD_SCOPE, plan.plan_id, plan.plan_digest, cfg,
                    runner=timing_runner([], delay=0.05, hang=("stuck",)))
        )
        await asyncio.sleep(0.4)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(main())

    rows = collect_plan(cfg, plan.plan_id)
    assert [r["task_ref"] for r in rows] == ["quick"]
    assert rows[0]["status"] == "ok"


# ---------------------------------------------------------------- progress
def test_progress_is_reported_once_per_wave(cfg, workspace):
    plan = make_plan(
        cfg, workspace,
        task_entry("writer", reads=["README.md"], writes=["pkg/config.py"]),
        task_entry("reader", reads=["pkg/config.py"], writes=["out.txt"]),
    )

    seen: list = []
    run(execute(GOOD_SCOPE, plan.plan_id, plan.plan_digest, cfg,
                runner=timing_runner([]), progress=lambda *a: seen.append(a)))

    assert [done for done, total, _ in seen] == [1, 2]
    assert all(total == 2 for _, total, _ in seen)


def test_a_progress_callback_that_raises_does_not_fail_the_run(cfg, workspace):
    """Progress is decoration. Whether the client renders it is still unknown,
    and an execution must never depend on it."""
    plan = make_plan(cfg, workspace, *independent("a"))

    def explode(*_):
        raise RuntimeError("client hung up")

    outcome = run(execute(GOOD_SCOPE, plan.plan_id, plan.plan_digest, cfg,
                          runner=timing_runner([]), progress=explode))
    assert outcome.outcome == "complete"
