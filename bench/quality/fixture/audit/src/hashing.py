"""What actually changed on disk, compared with what was declared.

There is no filesystem containment (NOTES.md section 15): `--add-dir` is
additive scope and `--sandbox` restricts terminal commands only, so a worker
can write anywhere it likes. Detection is therefore the only control we have
after the human approves, and this module is all of it.

Two levels, because they carry different evidential weight:

- **declared paths** get sha256 of their content. They are known before the
  worker starts, so both sides of the comparison are real hashes.
- **everything else under the workspace root** gets a manifest of
  `(size, mtime_ns)`. Cheap enough to walk on every run, which is what makes
  it worth doing at all.

The manifest can over-report: a rewrite with byte-identical content still moves
mtime and is reported as a change. That is the correct direction for a detector
-- a false "this changed" costs a human a glance, a false "nothing changed" is
the failure this module exists to prevent.

What it cannot see: anything outside the workspace root. That is stated in the
README and must not be quietly upgraded into a containment claim.

What it cannot tell: **which process** changed a file. Anything writing inside
the root during a run looks exactly like a worker -- an editor saving, a watcher
rebuilding, another MCP server appending to its log. `DEFAULT_IGNORE_GLOBS`
covers the common cases; the rest is the reader's judgement, which is why the
report names paths instead of just counting them.
"""

from __future__ import annotations

import hashlib
import logging
import os
from dataclasses import dataclass, field
from fnmatch import fnmatch
from pathlib import Path

from .models import Task

log = logging.getLogger("subagents.hashing")

# Directories never worth walking: large, churning, and never the thing a
# worker was asked to touch.
IGNORED_DIRS = frozenset({
    ".git", "__pycache__", ".venv", "venv", "node_modules", ".pytest_cache",
    ".mypy_cache", ".ruff_cache", ".tox", ".idea", ".vscode",
})

# Patterns whose changes are never a worker's deliverable. Logs are the case
# that matters: measured 2026-09-20, a SECOND MCP server registered in the same
# client (`probe/probe_server.py`) appended to `probe/probe.log` inside the
# workspace while a fan-out was running, and every task in the plan came back
# tainted for a file no worker touched. We cannot tell which process wrote a
# file -- only that it changed -- so patterns like this are the only defence
# against a detector that fires on every run and is therefore ignored.
DEFAULT_IGNORE_GLOBS = ("*.log", "*.log.*", "*.tmp", "*.swp", "*.pyc", "*~")

_READ_CHUNK = 1 << 20


@dataclass(frozen=True)
class IgnoreSpec:
    """Paths whose churn is ours, not the worker's.

    The server writes its database and its log INSIDE the workspace root while
    the run is happening. Without these exclusions every run reports itself as
    tainted, which would train the reader to ignore the signal entirely.
    """

    files: frozenset[str] = frozenset()
    dirs: frozenset[str] = IGNORED_DIRS
    globs: tuple[str, ...] = DEFAULT_IGNORE_GLOBS

    @classmethod
    def for_config(cls, config) -> "IgnoreSpec":
        own: set[str] = set()
        for path in (config.db_path, config.log_file):
            base = str(Path(path))
            # SQLite in WAL mode keeps two sidecars beside the database, and
            # both change on every write.
            own.update({base, base + "-wal", base + "-shm", base + "-journal"})
        extra = tuple(getattr(config, "taint_ignore", ()) or ())
        return cls(
            files=frozenset(os.path.normcase(p) for p in own),
            globs=DEFAULT_IGNORE_GLOBS + extra,
        )

    def skips_file(self, norm_path: str) -> bool:
        if norm_path in self.files:
            return True
        name = os.path.basename(norm_path)
        return any(fnmatch(name, pattern) or fnmatch(norm_path, pattern)
                   for pattern in self.globs)

    def skips_dir(self, name: str) -> bool:
        return name in self.dirs


@dataclass(frozen=True)
class Snapshot:
    root: str
    declared: dict[str, str | None]        # normcased path -> sha256, None if absent
    manifest: dict[str, tuple[int, int]]   # normcased path -> (size, mtime_ns)
    walk_errors: tuple[str, ...] = ()


@dataclass
class TaintReport:
    task_ref: str
    attribution: str                       # "task" | "wave"
    reads_modified: list[str] = field(default_factory=list)
    writes_changed: list[str] = field(default_factory=list)
    writes_unchanged: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    undeclared: list[str] = field(default_factory=list)

    @property
    def tainted(self) -> bool:
        """An undeclared change, a read that was written to, or a vanished path.

        A declared write that changed is the worker doing its job, and is not
        taint. A declared write that did NOT change is suspicious but not taint
        either -- it usually means the worker did nothing, which the status
        field already has to answer for.
        """
        return bool(self.reads_modified or self.undeclared or self.missing)

    @property
    def paths(self) -> list[str]:
        return sorted({*self.reads_modified, *self.undeclared, *self.missing})

    def describe(self) -> str:
        if not self.tainted:
            return ""
        parts: list[str] = []
        if self.reads_modified:
            parts.append(f"modified {len(self.reads_modified)} path(s) it declared read-only")
        if self.undeclared:
            scope = "in this wave" if self.attribution == "wave" else "by this task"
            parts.append(f"{len(self.undeclared)} undeclared path(s) changed {scope}")
        if self.missing:
            parts.append(f"{len(self.missing)} declared path(s) no longer exist")
        return "; ".join(parts)


def sha256_file(path: Path) -> str | None:
    """Hash a file, or None if it does not exist.

    A declared `write` target legitimately may not exist before the run, so
    absence is a value here rather than an error.
    """
    digest = hashlib.sha256()
    try:
        with open(path, "rb") as handle:
            while True:
                chunk = handle.read(_READ_CHUNK)
                if not chunk:
                    break
                digest.update(chunk)
    except FileNotFoundError:
        return None
    except OSError as exc:
        log.warning("could not hash %s: %s", path, exc)
        return None
    return digest.hexdigest()


def snapshot(root: Path, declared: set[str], *, ignore: IgnoreSpec | None = None) -> Snapshot:
    """Hash the declared paths; manifest the rest of the tree."""
    spec = ignore or IgnoreSpec()
    root = Path(root)

    declared_hashes = {os.path.normcase(str(p)): sha256_file(Path(p)) for p in declared}

    manifest: dict[str, tuple[int, int]] = {}
    errors: list[str] = []

    for dirpath, dirnames, filenames in os.walk(root, onerror=lambda e: errors.append(str(e))):
        # Pruned in place, so os.walk never descends into them at all.
        dirnames[:] = [d for d in dirnames if not spec.skips_dir(d)]
        for name in filenames:
            full = os.path.join(dirpath, name)
            norm = os.path.normcase(full)
            if spec.skips_file(norm) or norm in declared_hashes:
                continue
            try:
                stat = os.stat(full)
            except OSError:
                # Vanished mid-walk, or unreadable. Absent on one side and
                # present on the other reads as a change, which is the safe
                # direction.
                continue
            manifest[norm] = (stat.st_size, stat.st_mtime_ns)

    return Snapshot(
        root=os.path.normcase(str(root)),
        declared=declared_hashes,
        manifest=manifest,
        walk_errors=tuple(errors),
    )


def compare(
    before: Snapshot,
    after: Snapshot,
    task: Task,
    *,
    wave_declared: set[str] | None = None,
    attribution: str = "task",
) -> TaintReport:
    """Turn two snapshots into a report about one task.

    `wave_declared` is every path declared by any task in the same wave. With
    parallel workers an undeclared change cannot be pinned to one of them, so a
    path that another task in the wave declared is not counted against this one
    -- and `attribution` records which level the undeclared findings belong to.
    """
    report = TaintReport(task_ref=task.task_ref, attribution=attribution)
    declared_here = set(task.reads_norm) | set(task.writes_norm)
    known = {os.path.normcase(str(p)) for p in (wave_declared or set())} | declared_here

    for path in task.writes_norm:
        was, now = before.declared.get(path), after.declared.get(path)
        if was == now:
            report.writes_unchanged.append(path)
        elif now is None:
            report.missing.append(path)
        else:
            report.writes_changed.append(path)

    for path in task.reads_norm:
        was, now = before.declared.get(path), after.declared.get(path)
        if was == now:
            continue
        if now is None:
            report.missing.append(path)
        else:
            report.reads_modified.append(path)

    for path, entry in after.manifest.items():
        if path in known:
            continue
        if before.manifest.get(path) != entry:
            report.undeclared.append(path)
    for path in before.manifest:
        if path not in after.manifest and path not in known:
            report.undeclared.append(path)

    report.missing.sort()
    report.undeclared = sorted(set(report.undeclared))
    return report
