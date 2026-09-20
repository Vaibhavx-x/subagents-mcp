"""Taint detection: what changed versus what was declared.

This is the only control that survives the human approval, because there is no
filesystem containment to lean on. Every test here describes a way a worker can
do something other than what the plan said, and asserts we can see it.
"""

from __future__ import annotations

import os
from pathlib import Path

from conftest import make_task

from subagents.hashing import IgnoreSpec, compare, sha256_file, snapshot


def norm(path) -> str:
    return os.path.normcase(str(path))


def declared_of(*tasks) -> set[str]:
    out: set[str] = set()
    for task in tasks:
        out.update(task.reads)
        out.update(task.writes)
    return out


def snap(root: Path, *tasks, ignore=None):
    return snapshot(root, declared_of(*tasks), ignore=ignore)


# ------------------------------------------------------------------ the good case
def test_a_worker_that_stays_in_scope_is_clean(workspace):
    task = make_task("a", reads=(str(workspace / "README.md"),),
                     writes=(str(workspace / "out.txt"),))
    before = snap(workspace, task)
    (workspace / "out.txt").write_text("done\n", encoding="utf-8")
    after = snap(workspace, task)

    report = compare(before, after, task)
    assert not report.tainted
    assert report.writes_changed == [norm(workspace / "out.txt")]
    assert report.describe() == ""


def test_a_write_target_that_never_changed_is_surfaced(workspace):
    """Not taint, but it usually means the worker did nothing."""
    task = make_task("a", reads=(str(workspace / "README.md"),),
                     writes=(str(workspace / "out.txt"),))
    before = snap(workspace, task)
    after = snap(workspace, task)

    report = compare(before, after, task)
    assert not report.tainted
    assert report.writes_unchanged == [norm(workspace / "out.txt")]
    assert report.writes_changed == []


# ------------------------------------------------------------------- the taints
def test_an_undeclared_write_inside_the_root_is_caught(workspace):
    """The realistic failure: the worker touches something nobody approved."""
    task = make_task("a", reads=(str(workspace / "README.md"),),
                     writes=(str(workspace / "out.txt"),))
    before = snap(workspace, task)
    (workspace / "out.txt").write_text("done\n", encoding="utf-8")
    (workspace / "pkg" / "secret_sidecar.py").write_text("oops\n", encoding="utf-8")
    after = snap(workspace, task)

    report = compare(before, after, task)
    assert report.tainted
    assert norm(workspace / "pkg" / "secret_sidecar.py") in report.undeclared
    assert "undeclared" in report.describe()


def test_modifying_a_path_declared_read_only_is_caught(workspace):
    task = make_task("a", reads=(str(workspace / "pkg" / "config.py"),),
                     writes=(str(workspace / "out.txt"),))
    before = snap(workspace, task)
    (workspace / "pkg" / "config.py").write_text("def fetch_cfg():\n    return {'x': 1}\n",
                                                 encoding="utf-8")
    after = snap(workspace, task)

    report = compare(before, after, task)
    assert report.tainted
    assert report.reads_modified == [norm(workspace / "pkg" / "config.py")]
    assert "read-only" in report.describe()


def test_a_deleted_declared_path_is_missing_not_silently_clean(workspace):
    """A vanished file compares equal to nothing if you are not looking."""
    task = make_task("a", reads=(str(workspace / "README.md"),),
                     writes=(str(workspace / "pkg" / "server.py"),))
    before = snap(workspace, task)
    (workspace / "pkg" / "server.py").unlink()
    after = snap(workspace, task)

    report = compare(before, after, task)
    assert report.tainted
    assert report.missing == [norm(workspace / "pkg" / "server.py")]


def test_an_undeclared_deletion_is_caught(workspace):
    task = make_task("a", reads=(str(workspace / "README.md"),),
                     writes=(str(workspace / "out.txt"),))
    before = snap(workspace, task)
    (workspace / "test_config.py").unlink()
    after = snap(workspace, task)

    report = compare(before, after, task)
    assert report.tainted
    assert norm(workspace / "test_config.py") in report.undeclared


def test_a_same_size_rewrite_is_still_detected(workspace):
    """Length-preserving edits are exactly what a careful attacker writes, and
    exactly what a formatter does by accident."""
    target = workspace / "pkg" / "config.py"
    original = target.read_text(encoding="utf-8")
    task = make_task("a", reads=(str(target),), writes=(str(workspace / "out.txt"),))

    before = snap(workspace, task)
    replaced = original.replace("return {}", "return[]").ljust(len(original))
    assert len(replaced) == len(original)
    target.write_text(replaced, encoding="utf-8")
    after = snap(workspace, task)

    report = compare(before, after, task)
    assert report.reads_modified == [norm(target)], "a declared path is content-hashed"


# --------------------------------------------------------------- the ignore list
def test_the_servers_own_database_and_log_do_not_taint_every_run(workspace, cfg):
    """The trap that would make the signal worthless.

    The server writes subagents.db and subagents.log inside the workspace while
    the run is happening. Counting those as taint marks every run dirty, and a
    detector that always fires is one nobody reads.
    """
    from dataclasses import replace

    local = replace(cfg, db_path=workspace / "subagents.db",
                    log_file=workspace / "subagents.log")
    ignore = IgnoreSpec.for_config(local)
    task = make_task("a", reads=(str(workspace / "README.md"),),
                     writes=(str(workspace / "out.txt"),))

    (workspace / "subagents.db").write_text("x", encoding="utf-8")
    (workspace / "subagents.log").write_text("start\n", encoding="utf-8")
    before = snap(workspace, task, ignore=ignore)

    (workspace / "subagents.db").write_text("xx", encoding="utf-8")
    (workspace / "subagents.db-wal").write_text("wal", encoding="utf-8")
    (workspace / "subagents.log").write_text("start\nmore\n", encoding="utf-8")
    after = snap(workspace, task, ignore=ignore)

    assert not compare(before, after, task).tainted


def test_ignored_directories_are_not_walked(workspace):
    """Pruned rather than filtered: .git on a real repo is most of the tree."""
    (workspace / ".git").mkdir()
    (workspace / ".git" / "index").write_text("churn", encoding="utf-8")
    task = make_task("a", reads=(str(workspace / "README.md"),), writes=())

    shot = snapshot(workspace, declared_of(task))
    assert not any(".git" in path for path in shot.manifest)


# ------------------------------------------------------- attribution honesty
def test_a_sibling_tasks_declared_path_is_not_charged_to_this_task(workspace):
    """Parallel workers share a snapshot window. A path another task in the
    wave legitimately declared must not be reported as this one's taint."""
    mine = make_task("a", reads=(str(workspace / "README.md"),),
                     writes=(str(workspace / "a.txt"),))
    sibling = make_task("b", reads=(str(workspace / "README.md"),),
                        writes=(str(workspace / "b.txt"),))

    before = snap(workspace, mine, sibling)
    (workspace / "a.txt").write_text("mine\n", encoding="utf-8")
    (workspace / "b.txt").write_text("theirs\n", encoding="utf-8")
    after = snap(workspace, mine, sibling)

    report = compare(before, after, mine, wave_declared=declared_of(mine, sibling))
    assert not report.tainted


def test_wave_attribution_says_so_in_the_wording(workspace):
    """The failure mode is a report that looks per-worker and silently is not."""
    task = make_task("a", reads=(str(workspace / "README.md"),),
                     writes=(str(workspace / "out.txt"),))
    before = snap(workspace, task)
    (workspace / "stray.txt").write_text("who wrote this\n", encoding="utf-8")
    after = snap(workspace, task)

    per_task = compare(before, after, task, attribution="task")
    per_wave = compare(before, after, task, attribution="wave")
    assert "by this task" in per_task.describe()
    assert "in this wave" in per_wave.describe()


# ----------------------------------------------------------------- mechanics
def test_hashing_an_absent_file_is_a_value_not_an_error(tmp_path):
    """A declared write target legitimately does not exist yet."""
    assert sha256_file(tmp_path / "nope.txt") is None


def test_a_created_declared_write_is_a_change_not_a_crash(workspace):
    task = make_task("a", reads=(str(workspace / "README.md"),),
                     writes=(str(workspace / "brand_new.txt"),))
    before = snap(workspace, task)
    assert before.declared[norm(workspace / "brand_new.txt")] is None

    (workspace / "brand_new.txt").write_text("hello\n", encoding="utf-8")
    after = snap(workspace, task)

    report = compare(before, after, task)
    assert not report.tainted
    assert report.writes_changed == [norm(workspace / "brand_new.txt")]


def test_declared_paths_are_excluded_from_the_manifest(workspace):
    """Otherwise every declared write is reported twice, once as itself and
    once as an undeclared stranger."""
    task = make_task("a", reads=(str(workspace / "README.md"),),
                     writes=(str(workspace / "pkg" / "config.py"),))
    shot = snap(workspace, task)
    assert norm(workspace / "pkg" / "config.py") not in shot.manifest
    assert norm(workspace / "README.md") not in shot.manifest


# ------------------------------------------- churn from processes that are not ours
def test_another_processs_log_does_not_taint_the_run(workspace):
    """Measured 2026-09-20, and it made every task in a real plan dirty.

    A SECOND MCP server registered in the same client (probe/probe_server.py)
    appended to probe/probe.log inside the workspace while a fan-out ran. All
    three workers came back tainted for a file none of them touched. We cannot
    tell which process wrote a file -- only that it changed -- so logs are
    excluded by pattern.
    """
    task = make_task("a", reads=(str(workspace / "README.md"),),
                     writes=(str(workspace / "out.txt"),))
    (workspace / "probe").mkdir()
    (workspace / "probe" / "probe.log").write_text("line 1\n", encoding="utf-8")

    before = snap(workspace, task)
    (workspace / "out.txt").write_text("done\n", encoding="utf-8")
    (workspace / "probe" / "probe.log").write_text("line 1\nline 2\n", encoding="utf-8")
    after = snap(workspace, task)

    assert not compare(before, after, task).tainted


def test_extra_ignore_globs_come_from_config(workspace, cfg):
    """An escape hatch, because the default list cannot know about your build."""
    from dataclasses import replace

    local = replace(cfg, taint_ignore=("*.generated.ts",))
    ignore = IgnoreSpec.for_config(local)
    task = make_task("a", reads=(str(workspace / "README.md"),), writes=())

    before = snap(workspace, task, ignore=ignore)
    (workspace / "api.generated.ts").write_text("export {};\n", encoding="utf-8")
    after = snap(workspace, task, ignore=ignore)

    assert not compare(before, after, task).tainted


def test_a_real_source_file_is_still_caught_with_globs_active(workspace, cfg):
    """The ignore list must not become a hole big enough to hide a write in."""
    ignore = IgnoreSpec.for_config(cfg)
    task = make_task("a", reads=(str(workspace / "README.md"),), writes=())

    before = snap(workspace, task, ignore=ignore)
    (workspace / "pkg" / "injected.py").write_text("print('hi')\n", encoding="utf-8")
    after = snap(workspace, task, ignore=ignore)

    report = compare(before, after, task)
    assert report.tainted
    assert any("injected.py" in p for p in report.undeclared)


def test_an_undeclared_rewrite_with_identical_metadata_is_the_known_blind_spot(workspace):
    """The manifest's limit, written down rather than implied.

    Undeclared paths are tracked by (size, mtime_ns), so a rewrite that keeps
    both identical is invisible. In practice mtime_ns moves on every write, and
    forging it takes deliberate effort -- but a declared path would have been
    content-hashed and caught, and the difference between the two levels is
    exactly this case.
    """
    import os as _os

    stray = workspace / "pkg" / "untouched.py"
    stray.write_text("x = 1\n", encoding="utf-8")
    stat = _os.stat(stray)

    task = make_task("a", reads=(str(workspace / "README.md"),), writes=())
    before = snap(workspace, task)

    stray.write_text("x = 2\n", encoding="utf-8")          # same length
    _os.utime(stray, ns=(stat.st_atime_ns, stat.st_mtime_ns))
    after = snap(workspace, task)

    assert not compare(before, after, task).tainted, (
        "if this now fails the manifest got stronger -- update the docs, not the test"
    )
