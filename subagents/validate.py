"""Validation of everything the model supplies.

`task_ref` values and declared paths are model-generated strings entering the
data layer, so they are validated before use and never interpolated into SQL.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

from .errors import PathOutsideWorkspace, PlanRefused

# Exactly the documented charset. Deliberately not stricter: a tighter regex
# (say, forbidding a leading hyphen) would reject valid parent output for no
# safety gain, and task_ref is never used as a path component or in SQL.
TASK_REF_RE = re.compile(r"^[a-z0-9-]{1,40}$")

MAX_TASK_REF_LEN = 40


def validate_task_ref(ref: object) -> str:
    if not isinstance(ref, str):
        raise PlanRefused("task_ref must be a string", f"got {type(ref).__name__}")
    if not ref:
        raise PlanRefused("task_ref must not be empty")
    if len(ref) > MAX_TASK_REF_LEN:
        raise PlanRefused(
            "task_ref too long",
            f"{ref[:MAX_TASK_REF_LEN]}... is {len(ref)} chars; limit is {MAX_TASK_REF_LEN}",
        )
    if not TASK_REF_RE.match(ref):
        raise PlanRefused(
            "task_ref has invalid characters",
            f"{ref!r} -- allowed charset is [a-z0-9-]",
        )
    return ref


def validate_unique_task_refs(refs: list[str]) -> None:
    seen: set[str] = set()
    dupes: list[str] = []
    for ref in refs:
        if ref in seen and ref not in dupes:
            dupes.append(ref)
        seen.add(ref)
    if dupes:
        raise PlanRefused(
            "duplicate task_ref within plan",
            ", ".join(sorted(dupes)),
        )


def _normcase(p: Path) -> str:
    return os.path.normcase(str(p))


def is_inside(candidate: Path, root: Path) -> bool:
    """Case-insensitive containment test, correct on Windows.

    Path.is_relative_to is case-SENSITIVE, so 'd:/projects/x' would fail
    against a root of 'D:/Projects/x' for any path component resolve() could
    not case-normalise from disk.
    """
    c, r = _normcase(candidate), _normcase(root)
    return c == r or c.startswith(r + os.sep)


def resolve_workspace_root(raw: str, allowed_roots: tuple[Path, ...]) -> Path:
    if not isinstance(raw, str) or not raw.strip():
        raise PlanRefused("workspace_root is required")
    if "\x00" in raw:
        raise PlanRefused("workspace_root contains a null byte")

    root = Path(raw).resolve()
    if not root.is_dir():
        raise PlanRefused("workspace_root is not an existing directory", str(root))

    for allowed in allowed_roots:
        if is_inside(root, allowed):
            return root

    raise PlanRefused(
        "workspace_root is not in the server's allowlist",
        f"{root} -- allowed: {', '.join(str(a) for a in allowed_roots) or '(none configured)'}",
    )


def resolve_declared_path(declared: object, root: Path, *, field: str, task_ref: str) -> Path:
    """Resolve one declared path and prove it lands inside `root`.

    Two rules that are easy to get wrong, both covered by tests:

    * A relative path is resolved against `root`, NOT the process CWD.
      Path('bench/run.sh').resolve() silently uses os.getcwd().
    * Containment is checked AFTER resolve(), which follows symlinks. Checking
      the literal string first would let a symlink out of the workspace.
    """
    if not isinstance(declared, str) or not declared.strip():
        raise PlanRefused(f"{task_ref}: {field} entry must be a non-empty string")
    if "\x00" in declared:
        raise PlanRefused(f"{task_ref}: {field} entry contains a null byte")

    p = Path(declared)
    if not p.is_absolute():
        p = root / p

    # strict=False (the default): a declared path may not exist yet, because a
    # worker may be about to create it. resolve() still normalises the existing
    # ancestor chain, including its case.
    resolved = p.resolve()

    if not is_inside(resolved, root):
        raise PathOutsideWorkspace(
            f"{task_ref}: {field} path escapes the workspace",
            f"{declared!r} resolves to {resolved}, which is outside {root}",
        )
    return resolved
