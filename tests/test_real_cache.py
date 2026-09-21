"""The cache against a REAL agy worker.

Opt-in, because it spends API tokens:

    SUBAGENTS_REAL_AGY=1 python -m pytest tests/test_real_cache.py -q

A fake worker can prove the wave loop calls the cache. It cannot prove a hit
is *correct*, because a fake and a cache agreeing with each other proves only
that two things I wrote share an assumption. What needs a real worker is the
third case below: change one declared read and exactly one task must run
again. If the key were on the instruction string rather than on content, that
test passes with a fake and fails in production.

Budget: 5 real workers across the three tests. Each test gets its own
throwaway workspace and its own database, so none of them share an entry.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import time
from pathlib import Path

import pytest
from conftest import task_entry, tasks_json

from subagents.execution import execute
from subagents.planning import propose

REAL = os.environ.get("SUBAGENTS_REAL_AGY") == "1"

pytestmark = [
    pytest.mark.slow,
    pytest.mark.skipif(not REAL, reason="set SUBAGENTS_REAL_AGY=1 to spend tokens on real agy"),
    pytest.mark.skipif(shutil.which("agy") is None, reason="agy not on PATH"),
]

SCOPE = "writes summary.txt from README.md in the throwaway workspace; no deletes"

TASK = task_entry(
    "summarise",
    reads=["README.md"],
    writes=["summary.txt"],
    instruction=(
        "Read README.md in the workspace root and write a two-sentence summary "
        "of it to summary.txt in the workspace root. Do nothing else."
    ),
)


def run_plan(cfg, workspace: Path):
    plan = propose(tasks_json(TASK), str(workspace), cfg)
    began = time.time()
    outcome = asyncio.run(execute(SCOPE, plan.plan_id, plan.plan_digest, cfg))
    return plan, outcome, time.time() - began


def tokens_of(outcome) -> int:
    return sum((r.tokens_in or 0) for r in outcome.results)


def test_a_real_run_is_recorded_and_the_re_proposal_costs_nothing(cfg, workspace):
    """The loop the feature exists for, end to end with a live worker.

    Spends one worker. The second execution must spend none -- a different
    plan id, the same work, and the declared output already in place.
    """
    (workspace / "README.md").write_text(
        "# demo\n\nA tiny project used to check that the orchestrator's reuse\n"
        "path works against a real worker rather than a fake one.\n",
        encoding="utf-8",
    )

    _, first, first_s = run_plan(cfg, workspace)
    assert first.completed == 1, first.outcome
    assert (workspace / "summary.txt").is_file(), "the real worker did not write the file"
    assert first.cached == {}, "nothing should have been on record yet"
    spent = tokens_of(first)
    assert spent > 0, "a real worker reported no input tokens"

    plan, second, second_s = run_plan(cfg, workspace)
    assert plan.cached_refs == ("summarise",), "propose_plan did not forecast the hit"
    assert set(second.cached) == {"summarise"}
    assert second.results == [], "a worker was spawned for work already on record"
    assert tokens_of(second) == 0
    # agy costs ~9.7s of process startup alone, so any real spawn is nowhere
    # near this. The assertion is deliberately loose: what matters is the order
    # of magnitude, not a stopwatch.
    assert second_s < first_s / 2


def test_changing_a_declared_read_makes_exactly_that_task_run_again(cfg, workspace):
    """The test a fake cannot stand in for.

    If the key were on the instruction string -- the obvious implementation --
    this passes against a fake and silently serves stale work in production.
    Spends two workers: one to warm, one after the input changes.
    """
    (workspace / "README.md").write_text(
        "# demo one\n\nThe first version of this file, which the worker reads.\n",
        encoding="utf-8",
    )
    _, first, _ = run_plan(cfg, workspace)
    assert first.completed == 1

    (workspace / "README.md").write_text(
        "# demo two\n\nA completely different file. The recorded answer was\n"
        "about different input and must not be reused for this one.\n",
        encoding="utf-8",
    )

    plan, second, _ = run_plan(cfg, workspace)
    assert plan.cached_refs == (), "a changed input was forecast as a hit"
    assert second.cached == {}
    assert second.completed == 1, "the task did not actually run again"


def test_deleting_the_output_makes_the_task_run_again(cfg, workspace):
    """A hit asserts the declared outputs are already in place. Remove one and
    the assertion is false, whatever the inputs say.

    Spends two workers: one to warm, one to rebuild what was deleted.
    """
    (workspace / "README.md").write_text(
        "# demo\n\nA file to summarise, so the output can then be deleted.\n",
        encoding="utf-8",
    )
    _, first, _ = run_plan(cfg, workspace)
    assert first.completed == 1

    (workspace / "summary.txt").unlink()

    plan, second, _ = run_plan(cfg, workspace)
    assert plan.cached_refs == (), "a missing output was forecast as a hit"
    assert second.completed == 1
    assert (workspace / "summary.txt").is_file(), "the output was not rebuilt"
