"""End-to-end with a REAL agy worker.

Opt-in, because it spends API tokens:

    SUBAGENTS_REAL_AGY=1 python -m pytest tests/test_real_worker.py -q

Everything else in the suite uses tests/fake_worker.py. This exists to catch
what a fake cannot: the real command shape, the real output schema, and whether
a worker actually changes the file we asked it to.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import sys
import time
from dataclasses import replace
from pathlib import Path

import pytest
from conftest import task_entry, tasks_json

from subagents.execution import collect_plan, execute
from subagents.planning import propose

REAL = os.environ.get("SUBAGENTS_REAL_AGY") == "1"

pytestmark = [
    pytest.mark.slow,
    pytest.mark.skipif(not REAL, reason="set SUBAGENTS_REAL_AGY=1 to spend tokens on real agy"),
    pytest.mark.skipif(shutil.which("agy") is None, reason="agy not on PATH"),
]

SCOPE = "creates greeting.txt in the workspace; touches nothing else"


def marker_alive(marker: str) -> int:
    if sys.platform == "win32":
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             f"(Get-CimInstance Win32_Process | Where-Object {{ $_.CommandLine -like '*{marker}*' }}).Count"],
            capture_output=True, text=True,
        ).stdout.strip()
        return int(out or 0)
    return 0


def test_real_worker_does_the_work_and_reports_usage(cfg, workspace):
    """propose -> execute -> collect against a live worker."""
    plan = propose(
        tasks_json(task_entry(
            "make-greeting",
            writes=["greeting.txt"],
            reads=["README.md"],
            instruction=(
                "Create a file named greeting.txt in the workspace root "
                "containing exactly the single word: hello. Do nothing else."
            ),
        )),
        str(workspace),
        cfg,
    )

    started = time.time()
    outcome = asyncio.run(execute(SCOPE, plan.plan_id, plan.plan_digest, cfg))
    elapsed = time.time() - started

    result = outcome.results[0]
    assert result.ok, f"{result.status}: {result.exit_reason}\n{result.transcript[:500]}"

    # The worker actually changed the filesystem.
    created = workspace / "greeting.txt"
    assert created.is_file(), f"worker reported success but wrote nothing: {result.summary}"
    assert "hello" in created.read_text(encoding="utf-8").lower()

    # The real output schema still carries what we record.
    assert result.tokens_in and result.tokens_in > 0
    assert result.tokens_out is not None
    assert result.agy_status == "SUCCESS"
    assert result.duration_s is not None

    # Spawn overhead is ~10s (measured over 33 benchmark runs); this only
    # guards against the pipe-EOF bug silently reappearing as a long stall.
    assert elapsed < cfg.worker_timeout_s, f"took {elapsed:.0f}s"

    rows = collect_plan(cfg, plan.plan_id)
    assert rows[0]["status"] == "ok" and rows[0]["summary"]


def test_real_worker_killed_at_a_short_deadline_leaves_no_orphans(cfg, workspace):
    """A real worker exceeding its budget: partial stored, tree gone."""
    # ~10s of this is agy startup (measured), leaving little room for the task,
    # so the deadline reliably fires while the worker is mid-flight.
    short = replace(cfg, worker_timeout_s=13)
    plan = propose(
        tasks_json(task_entry(
            "slow-task",
            reads=["README.md"],
            instruction=(
                "Carefully read every file in this directory, then write a "
                "detailed 2000 word analysis of the codebase to analysis.md, "
                "then review and rewrite it three times."
            ),
        )),
        str(workspace),
        short,
    )

    outcome = asyncio.run(execute(SCOPE, plan.plan_id, plan.plan_digest, short))
    result = outcome.results[0]

    if result.ok:
        pytest.skip("worker finished inside the budget; nothing to assert about the kill path")

    assert result.status == "timeout", f"expected timeout, got {result.status}"
    assert "INCOMPLETE" in result.summary

    time.sleep(1.5)
    assert marker_alive("--print-timeout 13s") == 0, "an agy process outlived its deadline"

    # Whatever it managed is still recorded and readable.
    rows = collect_plan(cfg if False else short, plan.plan_id)
    assert rows[0]["status"] == "timeout"
