"""Digest stability and sensitivity.

This file protects Phase 2's tamper refusal. If the digest is unstable, an
identical plan gets refused and the parent cannot make progress. If it is
insensitive, a swapped plan executes under an approval granted for another.
"""

from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path

from conftest import REPO_ROOT, make_task

from subagents.digest import compute_digest

ROOT = "D:/ws"


def test_task_order_does_not_change_digest():
    a = make_task("alpha", reads=("D:/ws/a.py",), writes=("D:/ws/b.py",))
    b = make_task("beta", reads=("D:/ws/c.py",))
    assert compute_digest(ROOT, [a, b]) == compute_digest(ROOT, [b, a])


def test_reads_list_order_does_not_change_digest():
    one = make_task("t", reads=("D:/ws/a.py", "D:/ws/b.py"))
    # Bypass make_task's sorting to prove the digest sorts for itself.
    from subagents.models import Task

    other = Task("t", "do the thing", ("D:/ws/b.py", "D:/ws/a.py"), (), "m")
    assert compute_digest(ROOT, [one]) == compute_digest(ROOT, [other])


def test_writes_list_order_does_not_change_digest():
    from subagents.models import Task

    one = Task("t", "i", (), ("D:/ws/a.py", "D:/ws/b.py"), "m")
    other = Task("t", "i", (), ("D:/ws/b.py", "D:/ws/a.py"), "m")
    assert compute_digest(ROOT, [one]) == compute_digest(ROOT, [other])


def test_path_case_does_not_change_digest():
    """Windows paths are case-insensitive: the same file must digest the same."""
    a = make_task("t", reads=("D:/ws/README.md",))
    b = make_task("t", reads=("d:/ws/readme.md",))
    assert compute_digest(ROOT, [a]) == compute_digest(ROOT, [b])


def test_changed_read_path_changes_digest():
    a = make_task("t", reads=("D:/ws/a.py",))
    b = make_task("t", reads=("D:/ws/other.py",))
    assert compute_digest(ROOT, [a]) != compute_digest(ROOT, [b])


def test_changed_write_path_changes_digest():
    a = make_task("t", writes=("D:/ws/a.py",))
    b = make_task("t", writes=("D:/ws/other.py",))
    assert compute_digest(ROOT, [a]) != compute_digest(ROOT, [b])


def test_added_path_changes_digest():
    a = make_task("t", reads=("D:/ws/a.py",))
    b = make_task("t", reads=("D:/ws/a.py", "D:/ws/extra.py"))
    assert compute_digest(ROOT, [a]) != compute_digest(ROOT, [b])


def test_changed_instruction_changes_digest():
    a = make_task("t", instruction="rename the helper")
    b = make_task("t", instruction="delete the helper")
    assert compute_digest(ROOT, [a]) != compute_digest(ROOT, [b])


def test_changed_task_ref_changes_digest():
    assert compute_digest(ROOT, [make_task("alpha")]) != compute_digest(ROOT, [make_task("beta")])


def test_changed_model_changes_digest():
    a = make_task("t", model="gemini-3.8-flash-low")
    b = make_task("t", model="gemini-3.7-flash-medium")
    assert compute_digest(ROOT, [a]) != compute_digest(ROOT, [b])


def test_changed_workspace_root_changes_digest():
    t = make_task("t", reads=("D:/ws/a.py",))
    assert compute_digest("D:/ws", [t]) != compute_digest("D:/other", [t])


def test_dropped_task_changes_digest():
    a = make_task("alpha")
    b = make_task("beta")
    assert compute_digest(ROOT, [a, b]) != compute_digest(ROOT, [a])


# --------------------------------------------------------------------------
# The important one. An unsorted set reaching the canonical form is invisible
# in-process -- a single interpreter keeps a consistent iteration order -- and
# would only surface later as a mysterious Phase 2 refusal on an unmodified
# plan. Two subprocesses with different hash seeds is what catches it.
# --------------------------------------------------------------------------
_SNIPPET = textwrap.dedent(
    """
    import sys
    sys.path.insert(0, r"{repo}")
    from subagents.models import Task
    from subagents.digest import compute_digest

    tasks = [
        Task("zeta",  "i", ("D:/ws/z.py", "D:/ws/a.py"), ("D:/ws/w.py",), "m"),
        Task("alpha", "i", ("D:/ws/m.py",), ("D:/ws/n.py", "D:/ws/b.py"), "m"),
        Task("mid",   "i", (), (), "m"),
    ]
    sys.stdout.write(compute_digest("D:/ws", tasks))
    """
)


def _digest_in_subprocess(hash_seed: str) -> str:
    out = subprocess.run(
        [sys.executable, "-c", _SNIPPET.format(repo=str(REPO_ROOT))],
        capture_output=True,
        text=True,
        env={"PYTHONHASHSEED": hash_seed, "PATH": ""},
        check=True,
    )
    return out.stdout.strip()


def test_digest_stable_across_processes():
    """Differing PYTHONHASHSEED must not move the digest."""
    seeds = ["0", "1", "12345"]
    digests = {seed: _digest_in_subprocess(seed) for seed in seeds}
    assert len(set(digests.values())) == 1, f"digest varies with hash seed: {digests}"
    assert len(next(iter(digests.values()))) == 64
