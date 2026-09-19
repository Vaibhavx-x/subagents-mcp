"""execute_plan: validate the approval, then run the plan's workers.

Phase 2 runs workers one at a time, honouring wave order. Phase 3 raises
`max_parallel` within a wave; it should not need to change anything here.

The ordering of operations matters more than it looks. Validation happens
BEFORE anything spawns, so a refused plan costs nothing and cannot half-run.
Results are written the moment each worker finishes, never at the end, because
that is the only thing that makes `collect` useful after a cancellation -- and
it is a mistake that stays invisible until a run overruns.
"""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from .config import Config
from .db import connect, init_db
from .digest import compute_digest
from .errors import PlanRefused
from .models import Task
from .worker import WorkerResult, build_command, run_worker, worker_env

log = logging.getLogger("subagents.execution")

SCOPE_SUMMARY_MIN = 20
SCOPE_SUMMARY_MAX = 300


@dataclass
class StoredPlan:
    plan_id: str
    plan_digest: str
    workspace_root: Path
    tasks: list[Task]
    wave_of: dict[str, int]
    status: str
    expires_at: str


@dataclass
class ExecutionOutcome:
    plan_id: str
    outcome: str                                    # complete | partial
    results: list[WorkerResult] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)

    @property
    def completed(self) -> int:
        return sum(1 for r in self.results if r.ok)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def validate_scope_summary(text: object, plan_id: str) -> str:
    """Check it is present and says something.

    Deliberately NOT checked against the plan: it is model-authored free text,
    and no honest check exists. The server records it verbatim and the
    permission prompt shows it; claiming to verify it would be a lie in the
    audit trail. It is validated only for shape, because it is the first thing
    the human reads -- and, given the prompt truncates arguments, often the
    only thing.
    """
    if not isinstance(text, str):
        raise PlanRefused("scope_summary is required", "expected a short description")
    cleaned = " ".join(text.split())
    if len(cleaned) < SCOPE_SUMMARY_MIN:
        raise PlanRefused(
            "scope_summary is too short to be informative",
            f"got {len(cleaned)} chars; describe what will be touched, e.g. "
            f'"edits pkg/config.py and pkg/server.py; no deletes"',
        )
    if len(cleaned) > SCOPE_SUMMARY_MAX:
        raise PlanRefused(
            "scope_summary is too long",
            f"{len(cleaned)} chars; the approval prompt truncates, so lead with the paths",
        )
    if cleaned == plan_id:
        raise PlanRefused("scope_summary must describe the work, not repeat the plan id")
    return cleaned


def load_plan(config: Config, plan_id: str) -> StoredPlan:
    # init_db, not connect: a collect() or execute() may be the first call this
    # process makes, and querying a database with no schema is an
    # OperationalError rather than the clean "no such plan" the parent needs.
    conn = init_db(config.db_path)
    try:
        row = conn.execute(
            "SELECT id, plan_digest, workspace_root, plan_json, status, expires_at"
            " FROM plans WHERE id = ?",
            (plan_id,),
        ).fetchone()
    finally:
        conn.close()

    if row is None:
        raise PlanRefused("no such plan", f"{plan_id!r} -- call propose_plan first")

    entries = json.loads(row["plan_json"])
    tasks = [
        Task(
            task_ref=e["task_ref"],
            instruction=e["instruction"],
            reads=tuple(e["reads"]),
            writes=tuple(e["writes"]),
            model=e["model"],
        )
        for e in entries
    ]
    return StoredPlan(
        plan_id=row["id"],
        plan_digest=row["plan_digest"],
        workspace_root=Path(row["workspace_root"]),
        tasks=tasks,
        wave_of={e["task_ref"]: e["wave_index"] for e in entries},
        status=row["status"],
        expires_at=row["expires_at"],
    )


def verify_approval(stored: StoredPlan, supplied_digest: str) -> None:
    """Refuse a stale, swapped or expired plan. Runs before any spawn."""
    # Recomputed from the stored tasks, not taken from the stored column: that
    # is what proves the row has not been edited underneath the approval.
    recomputed = compute_digest(str(stored.workspace_root), stored.tasks)
    if recomputed != stored.plan_digest:
        raise PlanRefused(
            "stored plan does not match its own digest",
            "the plans row has been modified since it was proposed",
        )
    if supplied_digest != stored.plan_digest:
        raise PlanRefused(
            "plan_digest does not match this plan",
            "the approval was granted for a different plan; call propose_plan again",
        )
    try:
        expires = datetime.fromisoformat(stored.expires_at)
    except ValueError:
        raise PlanRefused("plan has an unreadable expiry", stored.expires_at) from None
    if _now() > expires:
        raise PlanRefused(
            "plan has expired",
            f"approved plans are executable until {stored.expires_at}; propose it again",
        )


def _already_complete(config: Config, plan_id: str) -> set[str]:
    conn = init_db(config.db_path)
    try:
        rows = conn.execute(
            "SELECT task_ref FROM runs WHERE plan_id = ? AND status = 'ok'", (plan_id,)
        ).fetchall()
    finally:
        conn.close()
    return {r["task_ref"] for r in rows}


def _record_execution(config: Config, plan_id: str, scope_summary: str) -> int:
    conn = init_db(config.db_path)
    try:
        cur = conn.execute(
            "INSERT INTO executions (plan_id, started_at) VALUES (?, ?)",
            (plan_id, _now().isoformat()),
        )
        conn.execute(
            "UPDATE plans SET status = 'executing', scope_summary = ? WHERE id = ?",
            (scope_summary, plan_id),
        )
        conn.commit()
        return int(cur.lastrowid)
    finally:
        conn.close()


def _finish_execution(config: Config, execution_id: int, plan_id: str, outcome: str,
                      started: int, completed: int, waves: int) -> None:
    conn = connect(config.db_path)
    try:
        conn.execute(
            "UPDATE executions SET ended_at = ?, outcome = ?, workers_started = ?,"
            " workers_completed = ?, waves_completed = ? WHERE id = ?",
            (_now().isoformat(), outcome, started, completed, waves, execution_id),
        )
        conn.execute("UPDATE plans SET status = ? WHERE id = ?", (outcome, plan_id))
        conn.commit()
    finally:
        conn.close()


def record_refusal(config: Config, plan_id: str, outcome: str, reason: str) -> None:
    """A refused approval is auditable even though nothing ran."""
    conn = init_db(config.db_path)
    try:
        conn.execute(
            "INSERT INTO executions (plan_id, started_at, ended_at, outcome)"
            " VALUES (?,?,?,?)",
            (plan_id, _now().isoformat(), _now().isoformat(), outcome),
        )
        conn.execute(
            "INSERT INTO violations (plan_id, attempted_action, reason, blocked_at)"
            " VALUES (?,?,?,?)",
            (plan_id, "execute_plan", reason, _now().isoformat()),
        )
        conn.commit()
    except Exception:  # noqa: BLE001 - auditing must never mask the refusal
        log.exception("could not record refusal for plan %s", plan_id)
    finally:
        conn.close()


def persist_result(config: Config, plan_id: str, task: Task, wave_index: int,
                   result: WorkerResult) -> None:
    """Write one worker's outcome immediately.

    Called per worker completion, never batched at the end: `collect` after a
    cancellation can only return what is already committed.
    """
    run_id = uuid.uuid4().hex[:12]
    conn = connect(config.db_path)
    try:
        conn.execute(
            "INSERT INTO runs (id, plan_id, task_ref, wave_index, instruction,"
            " declared_reads, declared_writes, model_requested, model_used, status,"
            " started_at, finished_at, tokens_in, tokens_out, thinking_tokens,"
            " cache_read_tokens, exit_reason)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
            " ON CONFLICT(plan_id, task_ref) DO UPDATE SET"
            "   status=excluded.status, finished_at=excluded.finished_at,"
            "   model_used=excluded.model_used, tokens_in=excluded.tokens_in,"
            "   tokens_out=excluded.tokens_out, thinking_tokens=excluded.thinking_tokens,"
            "   cache_read_tokens=excluded.cache_read_tokens,"
            "   exit_reason=excluded.exit_reason",
            (
                run_id, plan_id, task.task_ref, wave_index, task.instruction,
                json.dumps(list(task.reads)), json.dumps(list(task.writes)),
                task.model, result.model_used, result.status,
                result.started_at, result.finished_at,
                result.tokens_in, result.tokens_out, result.thinking_tokens,
                result.cache_read_tokens, result.exit_reason,
            ),
        )
        actual = conn.execute(
            "SELECT id FROM runs WHERE plan_id = ? AND task_ref = ?",
            (plan_id, task.task_ref),
        ).fetchone()["id"]
        conn.execute(
            "INSERT INTO results (run_id, content, content_bytes, summary, created_at)"
            " VALUES (?,?,?,?,?)",
            (actual, result.transcript, len(result.transcript.encode("utf-8")),
             result.summary, _now().isoformat()),
        )
        conn.commit()
    finally:
        conn.close()


async def execute(
    scope_summary: str,
    plan_id: str,
    plan_digest: str,
    config: Config,
    *,
    runner=None,
) -> ExecutionOutcome:
    """Validate, then run the plan's tasks in wave order, one at a time."""
    import asyncio

    # Resolved at call time, not bound as a default, so tests can substitute a
    # fake worker by patching the module attribute.
    spawn = runner or run_worker

    summary = validate_scope_summary(scope_summary, plan_id)
    stored = load_plan(config, plan_id)

    try:
        verify_approval(stored, plan_digest)
    except PlanRefused as exc:
        outcome = "refused_expiry" if "expired" in exc.reason else "refused_digest"
        record_refusal(config, plan_id, outcome, str(exc))
        raise

    done = _already_complete(config, plan_id)
    ordered = sorted(stored.tasks, key=lambda t: (stored.wave_of[t.task_ref], t.task_ref))
    execution_id = _record_execution(config, plan_id, summary)

    result = ExecutionOutcome(plan_id=plan_id, outcome="complete")
    started = 0
    waves_seen: set[int] = set()

    for task in ordered:
        if task.task_ref in done:
            result.skipped.append(task.task_ref)
            continue

        wave_index = stored.wave_of[task.task_ref]
        waves_seen.add(wave_index)
        started += 1
        log.info("plan %s: starting %s (wave %d)", plan_id, task.task_ref, wave_index)

        worker_result = await spawn(
            task.task_ref,
            build_command(task, stored.workspace_root, config),
            timeout_s=config.worker_timeout_s,
            cwd=stored.workspace_root,
            model=task.model or config.model,
            env=worker_env(),
        )

        # shield: a cancellation arriving now must not lose a finished worker's
        # output, which is the whole basis for collect() after a timeout.
        await asyncio.shield(
            asyncio.to_thread(persist_result, config, plan_id, task, wave_index, worker_result)
        )
        result.results.append(worker_result)

    if any(not r.ok for r in result.results):
        result.outcome = "partial"

    _finish_execution(
        config, execution_id, plan_id, result.outcome, started,
        result.completed, len(waves_seen),
    )
    return result


def collect_plan(config: Config, plan_id: str) -> list[dict]:
    """Read back whatever has completed, including after a cancellation."""
    conn = init_db(config.db_path)
    try:
        if conn.execute("SELECT 1 FROM plans WHERE id = ?", (plan_id,)).fetchone() is None:
            raise PlanRefused("no such plan", plan_id)
        rows = conn.execute(
            "SELECT r.task_ref, r.wave_index, r.status, r.exit_reason, r.tokens_in,"
            " r.tokens_out, r.thinking_tokens, r.cache_read_tokens, r.finished_at,"
            " res.summary, res.content_bytes"
            " FROM runs r LEFT JOIN results res ON res.run_id = r.id"
            " WHERE r.plan_id = ? ORDER BY r.wave_index, r.task_ref",
            (plan_id,),
        ).fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows]
