"""The cache key, and the conditions under which an entry may be served.

A cache is the easiest place in this project to tell a confident lie: it
returns a plausible answer without doing anything, and a wrong hit is
indistinguishable from a right one unless someone checks the file. So most of
what is asserted here is the *refusals* -- the cases where an entry exists and
must not be used anyway.
"""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

from conftest import make_task, task_entry
from test_execution import GOOD_SCOPE, make_plan, run
from test_taint_execution import writing_runner

from subagents import cache
from subagents.db import connect
from subagents.execution import execute

REPO_ROOT = Path(__file__).resolve().parent.parent


def key_for(workspace: Path, *, instruction="do the thing", model="m",
            reads=("README.md",), writes=("out.txt",), hashes=None):
    task = make_task("a", instruction=instruction, model=model,
                     reads=[str(workspace / r) for r in reads],
                     writes=[str(workspace / w) for w in writes])
    supplied = {os.path.normcase(str(workspace / r)): (hashes or {}).get(r, "h-" + r)
                for r in reads}
    return cache.compute_key(task, workspace, model, supplied)


# ------------------------------------------------------------------- the key
def test_the_key_changes_when_a_declared_read_changes_content(workspace):
    """The whole point. Same paths, same instruction, different input bytes --
    the recorded answer was about different inputs and must not be reused."""
    assert key_for(workspace) != key_for(workspace, hashes={"README.md": "different"})


def test_the_key_changes_on_instruction_model_paths_and_workspace(workspace, tmp_path):
    base = key_for(workspace)
    assert key_for(workspace, instruction="something else") != base
    assert key_for(workspace, model="other-model") != base
    assert key_for(workspace, reads=("README.md", "test_config.py")) != base
    assert key_for(workspace, writes=("elsewhere.txt",)) != base
    assert key_for(tmp_path) != base, "an entry must not travel between workspaces"


def test_the_key_ignores_task_ref(workspace):
    """The parent names tasks by intent. The same work re-proposed under a
    different name is the same work, and forcing a miss on a rename would make
    the cache useless in the exact loop it exists for."""
    common = dict(instruction="write the docs", model="m",
                  reads=(str(workspace / "README.md"),),
                  writes=(str(workspace / "out.txt"),))
    hashes = {os.path.normcase(str(workspace / "README.md")): "abc"}
    first = cache.compute_key(make_task("write-docs", **common), workspace, "m", hashes)
    second = cache.compute_key(make_task("docs", **common), workspace, "m", hashes)
    assert first == second


def test_the_key_is_stable_across_processes(workspace):
    """Computed in subprocesses with differing PYTHONHASHSEED.

    An unsorted set anywhere in the payload would produce a key that is
    perfectly stable within one process and never hits on another -- invisible
    to every in-process test, and the same trap test_digest.py guards for the
    plan digest.
    """
    script = textwrap.dedent(f"""
        import sys; sys.path.insert(0, r{str(REPO_ROOT)!r})
        from subagents.cache import compute_key
        from subagents.models import Task
        t = Task(task_ref="a", instruction="i",
                 reads=(r{str(workspace / "README.md")!r}, r{str(workspace / "z.py")!r}),
                 writes=(r{str(workspace / "out.txt")!r},), model="m")
        print(compute_key(t, r{str(workspace)!r}, "m",
                          {{p: "h" for p in t.reads_norm}}))
    """)
    keys = set()
    for seed in ("0", "1", "42"):
        env = {**os.environ, "PYTHONHASHSEED": seed}
        out = subprocess.run([sys.executable, "-c", script], capture_output=True,
                             text=True, env=env, timeout=60)
        assert out.returncode == 0, out.stderr
        keys.add(out.stdout.strip())
    assert len(keys) == 1, f"key varies with PYTHONHASHSEED: {keys}"


# ------------------------------------------------------------ what is cacheable
def test_a_task_with_no_declared_reads_is_never_cacheable(workspace):
    """No input fingerprint means nothing could ever invalidate the entry but
    the clock, and a cache whose only invalidation is a TTL eventually serves a
    stale answer confidently."""
    assert not cache.is_cacheable(make_task("a", writes=(str(workspace / "o.txt"),)))
    assert cache.is_cacheable(
        make_task("a", reads=(str(workspace / "README.md"),),
                  writes=(str(workspace / "o.txt"),))
    )


# --------------------------------------------------------------- record refuses
def seed(cfg, workspace, status="ok", writes=(("a.txt", "done\n"),)):
    """Run one task for real against a fake worker, leaving an entry behind."""
    plan = make_plan(cfg, workspace, task_entry("a", reads=["README.md"], writes=["a.txt"]))
    outcome = run(execute(GOOD_SCOPE, plan.plan_id, plan.plan_digest, cfg,
                          runner=writing_runner({"a": list(writes)},
                                                {"a": status})))
    return plan, outcome


def task_a(workspace):
    return make_task("a", instruction="do the thing",
                     reads=(str(workspace / "README.md"),),
                     writes=(str(workspace / "a.txt"),), model="m")


def test_a_successful_untainted_run_is_recorded(cfg, workspace):
    seed(cfg, workspace)
    assert cache.count(cfg) == 1


def test_a_failed_run_is_never_recorded(cfg, workspace):
    """A failure is not a result. Serving it back would turn one bad run into
    a permanent one."""
    seed(cfg, workspace, status="failed")
    assert cache.count(cfg) == 0


def test_a_timed_out_run_is_never_recorded(cfg, workspace):
    seed(cfg, workspace, status="timeout")
    assert cache.count(cfg) == 0


def test_a_tainted_run_is_never_recorded(cfg, workspace):
    """It already wrote somewhere the plan did not declare, so "nothing needs
    doing" is false about it -- and the undeclared change is exactly what a
    hit would skip re-doing and skip re-detecting."""
    seed(cfg, workspace, writes=(("a.txt", "done\n"), ("sneaky.txt", "oops\n")))
    assert cache.count(cfg) == 0


def test_the_cache_is_off_when_the_ttl_is_zero(cfg, workspace):
    off = replace(cfg, cache_ttl_s=0)
    seed(off, workspace)
    assert cache.count(off) == 0
    assert cache.lookup(off, task_a(workspace), workspace, "m") is None


# ---------------------------------------------------------------- lookup refuses
def test_a_recorded_run_is_found_again(cfg, workspace):
    seed(cfg, workspace)
    hit = cache.lookup(cfg, task_a(workspace), workspace, cfg.model)
    assert hit is not None
    assert "cache hit" in hit.describe()


def test_a_changed_declared_read_misses(cfg, workspace):
    seed(cfg, workspace)
    (workspace / "README.md").write_text("# something else entirely\n", encoding="utf-8")
    assert cache.lookup(cfg, task_a(workspace), workspace, cfg.model) is None


def test_a_deleted_output_misses(cfg, workspace):
    """The half that makes a hit honest. A hit asserts the declared outputs are
    already in place; it does not replay them. Delete one and the assertion is
    false, so the task has to run."""
    seed(cfg, workspace)
    (workspace / "a.txt").unlink()
    assert cache.lookup(cfg, task_a(workspace), workspace, cfg.model) is None


def test_an_edited_output_misses(cfg, workspace):
    seed(cfg, workspace)
    (workspace / "a.txt").write_text("someone else changed this\n", encoding="utf-8")
    assert cache.lookup(cfg, task_a(workspace), workspace, cfg.model) is None


def test_an_entry_whose_run_is_later_marked_tainted_is_not_served(cfg, workspace):
    """record() refuses a tainted run, but the runs row can be updated after
    the fact -- an escalation retry rewrites it in place. The entry must be
    re-checked against the run it points at, not trusted."""
    seed(cfg, workspace)
    conn = connect(cfg.db_path)
    try:
        conn.execute("UPDATE runs SET tainted = 1 WHERE task_ref = 'a'")
        conn.commit()
    finally:
        conn.close()
    assert cache.lookup(cfg, task_a(workspace), workspace, cfg.model) is None


# ------------------------------------------------------------------- the expiry
def expire_everything(cfg):
    past = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
    conn = connect(cfg.db_path)
    try:
        conn.execute("UPDATE cache_entries SET expires_at = ?", (past,))
        conn.commit()
    finally:
        conn.close()


def test_an_expired_entry_misses(cfg, workspace):
    seed(cfg, workspace)
    expire_everything(cfg)
    assert cache.lookup(cfg, task_a(workspace), workspace, cfg.model) is None


def test_prune_removes_expired_entries_and_counts_them(cfg, workspace):
    seed(cfg, workspace)
    expire_everything(cfg)
    assert cache.prune(cfg) == 1
    assert cache.count(cfg) == 0


def test_prune_leaves_live_entries_alone(cfg, workspace):
    seed(cfg, workspace)
    assert cache.prune(cfg) == 0
    assert cache.count(cfg) == 1


def test_prune_on_a_database_that_does_not_exist_is_not_an_error(cfg, tmp_path):
    assert cache.prune(replace(cfg, db_path=tmp_path / "nothing-here.db")) == 0


# ----------------------------------------------------------- the demo still runs
def test_the_taint_demos_two_plans_do_not_share_a_key(workspace):
    """bench/ab/demo_taint.py runs a plan, gets a taint, then re-proposes with
    the undeclared path DECLARED and expects it to really run. Declaring an
    extra write changes the key, so it does. If this ever stops holding the
    demo has to set SUBAGENTS_CACHE_TTL_S=0 instead of relying on it.
    """
    reads = (str(workspace / "README.md"),)
    hashes = {os.path.normcase(reads[0]): "abc"}
    before = make_task("t", reads=reads, writes=(str(workspace / "declared.txt"),))
    after = make_task("t", reads=reads,
                      writes=(str(workspace / "declared.txt"),
                              str(workspace / "surprise.txt")))
    assert (cache.compute_key(before, workspace, "m", hashes)
            != cache.compute_key(after, workspace, "m", hashes))
