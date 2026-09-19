"""Canonicalisation and the plan digest.

execute_plan recomputes this and refuses on mismatch, so it is the only thing
standing between an approval granted for one plan and a different plan running
under it. Two properties matter, and both are tested:

* STABLE -- key order, whitespace, and the order of tasks or of paths within a
  task must not change the digest. Otherwise a re-serialised but identical plan
  is refused, and the parent cannot make progress.
* SENSITIVE -- any change to a declared path, instruction, model or the
  workspace root must change it. Otherwise a swapped plan executes.

Wave indices are deliberately excluded: they are derived by the scheduler, and
the digest should cover the plan a human approved, not the scheduler's output.
"""

from __future__ import annotations

import hashlib
import json
import os

from .models import Task


def canonical_form(workspace_root: str, tasks: list[Task] | tuple[Task, ...]) -> dict:
    """Build the canonical dict.

    Every collection is sorted. Never iterate a set into this structure without
    sorted() -- set iteration order can vary between processes, which would
    make the digest unstable in a way that is invisible in-process and only
    shows up as a mysterious refusal in Phase 2.
    """
    return {
        "workspace_root": os.path.normcase(workspace_root),
        "tasks": [
            {
                "task_ref": t.task_ref,
                "instruction": t.instruction,
                "reads": list(t.reads_norm),
                "writes": list(t.writes_norm),
                "model": t.model,
            }
            for t in sorted(tasks, key=lambda t: t.task_ref)
        ],
    }


def canonical_json(workspace_root: str, tasks: list[Task] | tuple[Task, ...]) -> str:
    return json.dumps(
        canonical_form(workspace_root, tasks),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )


def compute_digest(workspace_root: str, tasks: list[Task] | tuple[Task, ...]) -> str:
    return hashlib.sha256(canonical_json(workspace_root, tasks).encode("utf-8")).hexdigest()
