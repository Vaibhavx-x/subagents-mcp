"""Reusing a finished task instead of running it again.

The case this exists for is the **re-propose loop**. A plan cancelled at the
client deadline, a task that came back tainted and is re-declared, an approval
the human declined and the parent retried: each produces a *new plan id*
carrying tasks whose work is already done. `execution._already_complete` skips
those only within one `plan_id`, so today every one of them is spawned again at
full cost.

What a hit claims, and what it does not
---------------------------------------

A hit does **not** claim the model would produce the same text. It cannot, and
a cache built on that claim would be wrong the first time a model sampled
differently.

It claims something narrower and checkable: **re-running this worker would
change nothing observable.** That decomposes into two mechanical tests.

*The key* -- what the work was:

    sha256( workspace_root, instruction, model, declared reads, declared
            writes, and the sha256 of each declared READ at the moment the
            recorded run started )

*The validity check* -- whether the world still looks the way that run left it:

    every declared WRITE currently hashes to the value stored in `file_hashes`
    at phase `post` for that run.

The second is what makes a hit honest. A hit does not replay side effects; it
asserts they are already present. Delete the output file and the entry stops
matching and the task runs. That check is stated over absolute paths, so an
entry cannot travel between workspaces -- `workspace_root` is in the key by
construction rather than by choice, and that is exactly why the retrospective
hit rate on this project's own benchmark is zero (`bench/cache/RESULTS.md`).
Keying on relative paths would flatter that number and break the guarantee.

`task_ref` is deliberately NOT in the key. The parent names tasks by intent, so
the same work re-proposed as `docs-worker` rather than `write-worker-docs` is
the same work.

What it still cannot see
------------------------

An effect outside the declared writes -- a test suite run, a package installed,
a service restarted. Nothing records those, so nothing can verify them, and a
hit skips them silently. That is what `SUBAGENTS_CACHE_TTL_S` bounds, and it is
stated in the README rather than hoped away.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .config import Config
from .db import init_db, write_transaction
from .hashing import Snapshot, sha256_file
from .models import Task

log = logging.getLogger("subagents.cache")


@dataclass(frozen=True)
class CacheHit:
    """A finished run that makes re-running this task pointless."""

    key_hash: str
    run_id: str
    source_plan_id: str
    source_task_ref: str
    content: str
    summary: str
    created_at: str
    age_s: float

    def describe(self) -> str:
        """For the parent and for the human reading the execution report."""
        minutes = self.age_s / 60
        age = f"{minutes:.0f}m" if minutes >= 1 else f"{self.age_s:.0f}s"
        return f"cache hit (run {self.run_id}, {age} old, plan {self.source_plan_id})"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def compute_key(task: Task, workspace_root: Path | str, model: str,
                read_hashes: dict[str, str | None]) -> str:
    """Hash the identity of the work.

    Every collection is sorted before it enters the payload, for the same
    reason `digest.canonical_form` says so: set iteration order can differ
    between processes, and an unstable key would be invisible in-process and
    show up only as a cache that never hits on someone else's machine.

    An absent read hashes to `None` and that is a value, not a gap -- "this
    file did not exist" is part of the input state the work was done against.
    """
    payload = {
        "workspace_root": os.path.normcase(str(workspace_root)),
        "instruction": task.instruction,
        "model": model,
        "reads": list(task.reads_norm),
        "writes": list(task.writes_norm),
        # Ordered by reads_norm, which is itself sorted -- never by dict order.
        "read_hashes": [[path, read_hashes.get(path)] for path in task.reads_norm],
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"),
                           ensure_ascii=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def is_cacheable(task: Task) -> bool:
    """Whether this task can ever produce an entry.

    A task declaring no reads has no input fingerprint, so nothing could
    invalidate its entry except the clock -- and a cache whose only
    invalidation is a TTL will eventually serve a stale answer confidently.
    That is the same reason `Task.declares_no_reads` is flagged at validation
    (CLAUDE.md section 5): a check over nothing passes silently.
    """
    return not task.declares_no_reads


def hash_declared_reads(task: Task) -> dict[str, str | None]:
    """Current content of every declared read, keyed by normcased path."""
    return {os.path.normcase(p): sha256_file(Path(p)) for p in task.reads}


def lookup(config: Config, task: Task, workspace_root: Path,
           model: str) -> CacheHit | None:
    """The entry that makes running this task unnecessary, or None.

    Re-hashes the declared reads at call time rather than trusting anything
    stored, then verifies the declared writes against the recorded `post`
    hashes. Both must hold. A miss is never an error -- it is the normal case
    and the safe one.
    """
    if config.cache_ttl_s <= 0 or not is_cacheable(task):
        return None

    key = compute_key(task, workspace_root, model, hash_declared_reads(task))

    conn = init_db(config.db_path)
    try:
        row = conn.execute(
            "SELECT c.key_hash, c.run_id, c.created_at, c.expires_at,"
            "       r.plan_id, r.task_ref, r.status, r.tainted,"
            "       res.content, res.summary"
            "  FROM cache_entries c"
            "  JOIN runs r ON r.id = c.run_id"
            "  LEFT JOIN results res ON res.run_id = r.id"
            " WHERE c.key_hash = ?"
            " ORDER BY res.id DESC LIMIT 1",
            (key,),
        ).fetchone()
        if row is None:
            return None

        # Re-checked rather than trusted. `record` refuses both already, but the
        # runs row can be updated afterwards -- an escalation retry rewrites it
        # in place -- and an entry pointing at a run that has since been marked
        # tainted must not be served.
        if row["status"] != "ok" or row["tainted"]:
            log.info("cache: entry for %s points at a %s run; ignoring",
                     task.task_ref, row["status"])
            return None
        if row["content"] is None:
            return None

        now = _now()
        try:
            expires = datetime.fromisoformat(row["expires_at"])
            created = datetime.fromisoformat(row["created_at"])
        except (TypeError, ValueError):
            return None
        if now >= expires:
            log.info("cache: entry for %s expired at %s", task.task_ref, row["expires_at"])
            return None

        recorded_post = {
            h["path"]: h["sha256"]
            for h in conn.execute(
                "SELECT path, sha256 FROM file_hashes WHERE run_id = ? AND phase = 'post'",
                (row["run_id"],),
            ).fetchall()
        }
    finally:
        conn.close()

    # The half that makes a hit honest: the declared outputs must already be in
    # the state that run left them. Nothing is replayed.
    for path in task.writes_norm:
        if path not in recorded_post:
            log.info("cache: %s has no recorded post hash for %s; running it",
                     task.task_ref, path)
            return None
        if sha256_file(Path(path)) != recorded_post[path]:
            log.info("cache: %s would hit but %s has changed since; running it",
                     task.task_ref, path)
            return None

    return CacheHit(
        key_hash=key,
        run_id=row["run_id"],
        source_plan_id=row["plan_id"],
        source_task_ref=row["task_ref"],
        content=row["content"],
        summary=row["summary"] or "",
        created_at=row["created_at"],
        age_s=max(0.0, (now - created).total_seconds()),
    )


def record(config: Config, plan_id: str, task: Task, workspace_root: Path,
           model: str, before: Snapshot, *, tainted: bool, ok: bool) -> None:
    """Make a finished run reusable, if it earned it.

    Refused for a run that failed (a failure is not a result) and for one that
    came back tainted (it already wrote somewhere undeclared, so "nothing needs
    doing" is false about it). Refused for a task with no declared reads, per
    `is_cacheable`.

    The key uses the `before` snapshot's hashes, not the current ones: the
    entry has to describe the input state the work was actually done against,
    and by the time this runs the worker may have changed a file it declared as
    both a read and a write.

    Stores a key and a pointer, never a copy. The transcript stays in
    `results`, the hashes stay in `file_hashes`, and this table cannot drift
    out of agreement with the audit trail because it holds none of it.
    """
    if config.cache_ttl_s <= 0 or not ok or tainted or not is_cacheable(task):
        return

    read_hashes = {path: before.declared.get(path) for path in task.reads_norm}
    key = compute_key(task, workspace_root, model, read_hashes)
    now = _now()
    expires = now + timedelta(seconds=config.cache_ttl_s)

    with write_transaction(config.db_path) as conn:
        row = conn.execute(
            "SELECT id FROM runs WHERE plan_id = ? AND task_ref = ?",
            (plan_id, task.task_ref),
        ).fetchone()
        if row is None:
            log.warning("cache: no run row for %s; not recording", task.task_ref)
            return
        conn.execute(
            "INSERT INTO cache_entries (key_hash, run_id, task_ref, created_at, expires_at)"
            " VALUES (?,?,?,?,?)"
            " ON CONFLICT(key_hash) DO UPDATE SET"
            "   run_id=excluded.run_id, task_ref=excluded.task_ref,"
            "   created_at=excluded.created_at, expires_at=excluded.expires_at",
            (key, row["id"], task.task_ref, now.isoformat(), expires.isoformat()),
        )
    log.info("cache: recorded %s (key %s...) valid until %s",
             task.task_ref, key[:12], expires.isoformat(timespec="seconds"))


def prune(config: Config, *, now: datetime | None = None) -> int:
    """Drop expired entries. Returns how many went.

    Run before any spawn rather than lazily at lookup, so an expired entry is
    never something a reader of the database has to reason about.
    """
    if not Path(config.db_path).is_file():
        return 0
    # init_db, not connect: every database written before this table existed is
    # still a perfectly good database, and `CREATE TABLE IF NOT EXISTS` on each
    # open IS the migration path. Without this the first --prune-cache against
    # an install from an earlier phase raises "no such table".
    init_db(config.db_path).close()
    stamp = (now or _now()).isoformat()
    with write_transaction(config.db_path) as conn:
        cur = conn.execute("DELETE FROM cache_entries WHERE expires_at <= ?", (stamp,))
        removed = cur.rowcount or 0
    if removed:
        log.info("cache: pruned %d expired entr%s", removed,
                 "y" if removed == 1 else "ies")
    return removed


def count(config: Config) -> int:
    """Live entries, for the CLI and the tests."""
    if not Path(config.db_path).is_file():
        return 0
    conn = init_db(config.db_path)
    try:
        return int(conn.execute("SELECT count(*) FROM cache_entries").fetchone()[0])
    except Exception:  # noqa: BLE001 -- no schema yet is not an error
        return 0
    finally:
        conn.close()
