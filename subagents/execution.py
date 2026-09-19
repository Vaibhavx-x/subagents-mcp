"""execute_plan: validate the approval, then run the plan's workers.

Waves run strictly in sequence; the workers inside one wave run concurrently
under `max_parallel`. The sequence is not a performance choice -- it is the
writer-before-reader contract that makes the schedule safe, and the only reason
a reader can trust what it reads.

The ordering of operations matters more than it looks. Validation happens
BEFORE anything spawns, so a refused plan costs nothing and cannot half-run.
Results are written the moment each worker finishes, never at the end, because
that is the only thing that makes `collect` useful after a cancellation -- and
it is a mistake that stays invisible until a run overruns.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import os
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from .config import RATE_LIMIT_ATTEMPTS, RATE_LIMIT_BACKOFF_S, Config
from .db import connect, init_db, write_transaction
from .digest import compute_digest
from .errors import PlanRefused
from .hashing import IgnoreSpec, Snapshot, TaintReport, compare, snapshot
from .models import Task
from .waves import build_edges
from .worker import (
    KILL_GRACE_S,
    WorkerResult,
    build_command,
    is_rate_limited,
    run_worker,
    worker_env,
)

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
    outcome: str                # complete | partial | cancelled_at_deadline
    results: list[WorkerResult] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    # (task_ref, upstream task_ref): never spawned, because the task it depends
    # on failed and it would otherwise read stale state and report success.
    blocked: list[tuple[str, str]] = field(default_factory=list)
    taints: dict[str, TaintReport] = field(default_factory=dict)
    escalated: list[str] = field(default_factory=list)

    @property
    def completed(self) -> int:
        return sum(1 for r in self.results if r.ok)

    @property
    def tainted_refs(self) -> list[str]:
        return sorted(ref for ref, report in self.taints.items() if report.tainted)


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

    Called `affects` on the wire: the client sorts schema properties
    alphabetically before the model sees them, so the name has to sort ahead of
    `plan_digest`/`plan_id` to reach the prompt at all (NOTES.md section 20).
    Error messages use the wire name, since that is what the parent passes.
    """
    if not isinstance(text, str):
        raise PlanRefused("`affects` is required", "expected a short description")
    cleaned = " ".join(text.split())
    if len(cleaned) < SCOPE_SUMMARY_MIN:
        raise PlanRefused(
            "`affects` is too short to be informative",
            f"got {len(cleaned)} chars; describe what will be touched, e.g. "
            f'"edits pkg/config.py and pkg/server.py; no deletes"',
        )
    if len(cleaned) > SCOPE_SUMMARY_MAX:
        raise PlanRefused(
            "`affects` is too long",
            f"{len(cleaned)} chars; the approval prompt truncates, so lead with the paths",
        )
    if cleaned == plan_id:
        raise PlanRefused("`affects` must describe the work, not repeat the plan id")
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
        # The two vocabularies are not the same, and conflating them fails a
        # CHECK constraint the moment a run is cancelled. `executions.outcome`
        # records HOW the attempt ended; `plans.status` records what the plan
        # is now -- and a cancelled plan is a partial one, with finished work
        # collect() can still return.
        plan_status = "partial" if outcome == "cancelled_at_deadline" else outcome
        conn.execute("UPDATE plans SET status = ? WHERE id = ?", (plan_status, plan_id))
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
    # write_transaction, not connect: Phase 3 lands a whole wave of results at
    # once, and a bare connection discovers the contention mid-statement.
    with write_transaction(config.db_path) as conn:
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


def persist_taint(config: Config, plan_id: str, task_ref: str, report: TaintReport) -> None:
    """Record the taint verdict against a run that is already stored.

    A second write rather than part of persist_result, because the post-run
    snapshot is taken once the whole wave has exited -- and the first write is
    what `collect` depends on after a cancellation, so it must not wait for it.
    """
    with write_transaction(config.db_path) as conn:
        conn.execute(
            "UPDATE runs SET tainted = ?, tainted_paths = ? WHERE plan_id = ? AND task_ref = ?",
            (1 if report.tainted else 0,
             json.dumps(report.paths) if report.tainted else None,
             plan_id, task_ref),
        )


def persist_blocked(config: Config, plan_id: str, task: Task, wave_index: int,
                    upstream: str) -> None:
    """A task that never ran still gets a row, with the reason.

    Silence would be indistinguishable from "not started yet", and the parent
    needs to know the difference before it re-proposes.
    """
    with write_transaction(config.db_path) as conn:
        conn.execute(
            "INSERT INTO runs (id, plan_id, task_ref, wave_index, instruction,"
            " declared_reads, declared_writes, model_requested, status, exit_reason)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)"
            " ON CONFLICT(plan_id, task_ref) DO UPDATE SET"
            "   status=excluded.status, exit_reason=excluded.exit_reason",
            (uuid.uuid4().hex[:12], plan_id, task.task_ref, wave_index, task.instruction,
             json.dumps(list(task.reads)), json.dumps(list(task.writes)), task.model,
             "blocked", f"not run: depends on {upstream}, which did not succeed"),
        )


def group_into_waves(plan: StoredPlan) -> list[list[Task]]:
    """Tasks by wave index, each wave ordered by task_ref for determinism."""
    waves: dict[int, list[Task]] = {}
    for task in plan.tasks:
        waves.setdefault(plan.wave_of[task.task_ref], []).append(task)
    return [sorted(waves[i], key=lambda t: t.task_ref) for i in sorted(waves)]


def downstream_of(tasks: list[Task]) -> dict[str, set[str]]:
    """reader -> {writers it depends on}, inverted from the wave scheduler.

    Reuses `waves.build_edges` rather than re-deriving the dependency graph, so
    the blocking rule and the scheduling rule can never disagree.
    """
    inverted: dict[str, set[str]] = {t.task_ref: set() for t in tasks}
    for writer, readers in build_edges(tasks).items():
        for reader in readers:
            inverted.setdefault(reader, set()).add(writer)
    return inverted


def declared_paths(tasks: list[Task]) -> set[str]:
    return {p for task in tasks for p in (*task.reads, *task.writes)}


def should_escalate(result: WorkerResult, report: TaintReport | None) -> bool:
    """One retry on the stronger model, for the failures a model can fix.

    NOT a timeout: it needed more time, not more reasoning, and a retry costs
    another full budget while the client deadline keeps running. NOT a tainted
    run: it already wrote somewhere it should not have, and running it again
    compounds that rather than correcting it. NOT a spawn error: the binary is
    missing or unrunnable, which no model changes.
    """
    if report is not None and report.tainted:
        return False
    if is_rate_limited(result):
        # The provider just said no. run_one has already waited it out to the
        # attempt limit, and escalating would answer a quota refusal by sending
        # a MORE expensive request at the same quota.
        return False
    return result.status in ("failed", "unparseable")


def fits_remaining_deadline(elapsed_s: float, config: Config,
                            deadline_s: int | None) -> bool:
    """Whether one more worker can finish before the client gives up on us.

    The deadline is read from the client's own config, so this is arithmetic on
    a real number rather than optimism. Without it, an escalation retry is the
    most likely way to turn a partial result into a cancelled one.
    """
    if not deadline_s:
        return True
    worst = config.worker_timeout_s + KILL_GRACE_S
    return elapsed_s + worst <= deadline_s


async def execute(
    scope_summary: str,
    plan_id: str,
    plan_digest: str,
    config: Config,
    *,
    runner=None,
    progress=None,
    deadline_s: int | None = None,
) -> ExecutionOutcome:
    """Validate the approval, then run the plan wave by wave.

    Within a wave the workers run concurrently under `max_parallel`. Between
    waves nothing overlaps, because a reader is only safe once its writer has
    exited -- not once its writer has reported done.
    """
    # Resolved at call time, not bound as a default, so tests can substitute a
    # fake worker by patching the module attribute.
    spawn = runner or run_worker

    async def report_progress(done: int, total: int, note: str) -> None:
        """Progress is decoration, and whether the client renders it at all is
        still unknown. An execution must never fail because of it.

        Awaits the callback when it returns an awaitable: the SDK's
        `ctx.report_progress` is a coroutine, and calling it without awaiting
        silently sends nothing while emitting a RuntimeWarning nobody reads.
        """
        if progress is None:
            return
        try:
            maybe = progress(done, total, note)
            if inspect.isawaitable(maybe):
                await maybe
        except Exception:  # noqa: BLE001 -- a broken client must not lose a run
            log.warning("progress callback failed; continuing", exc_info=True)

    summary = validate_scope_summary(scope_summary, plan_id)
    stored = load_plan(config, plan_id)

    try:
        verify_approval(stored, plan_digest)
    except PlanRefused as exc:
        outcome = "refused_expiry" if "expired" in exc.reason else "refused_digest"
        record_refusal(config, plan_id, outcome, str(exc))
        raise

    done = _already_complete(config, plan_id)
    waves = group_into_waves(stored)
    depends_on = downstream_of(stored.tasks)
    ignore = IgnoreSpec.for_config(config)
    execution_id = _record_execution(config, plan_id, summary)

    result = ExecutionOutcome(plan_id=plan_id, outcome="complete")
    failed: set[str] = set()
    started = 0
    waves_done = 0
    began = time.monotonic()

    async def run_one(task: Task, wave_index: int, sem: asyncio.Semaphore,
                      model: str | None = None) -> WorkerResult:
        async with sem:
            for attempt in range(RATE_LIMIT_ATTEMPTS):
                log.info("plan %s: starting %s (wave %d)", plan_id, task.task_ref, wave_index)
                worker_result = await spawn(
                    task.task_ref,
                    build_command(task, stored.workspace_root, config, model),
                    # agy gets its own --print-timeout at worker_timeout_s;
                    # ours fires KILL_GRACE_S later so agy can exit cleanly and
                    # still report token usage. Our kill is the backstop for an
                    # agy that hangs -- a killed process tells us far less.
                    timeout_s=config.worker_timeout_s + KILL_GRACE_S,
                    cwd=stored.workspace_root,
                    model=model or task.model or config.model,
                    env=worker_env(),
                )
                if not is_rate_limited(worker_result) or attempt == RATE_LIMIT_ATTEMPTS - 1:
                    break
                # The semaphore is deliberately still held while we wait: a
                # rate limit means the provider wants less traffic, and
                # releasing the slot would immediately start another worker.
                delay = RATE_LIMIT_BACKOFF_S * (2 ** attempt)
                log.warning("plan %s: %s rate limited; waiting %ss (attempt %d/%d)",
                            plan_id, task.task_ref, delay, attempt + 1, RATE_LIMIT_ATTEMPTS)
                await asyncio.sleep(delay)
            # shield: a cancellation arriving now must not lose a finished
            # worker's output, which is the whole basis for collect().
            await asyncio.shield(
                asyncio.to_thread(persist_result, config, plan_id, task, wave_index,
                                  worker_result)
            )
            return worker_result

    try:
        for wave_index, wave in enumerate(waves):
            runnable: list[Task] = []
            for task in wave:
                if task.task_ref in done:
                    result.skipped.append(task.task_ref)
                    continue
                upstream = sorted(depends_on.get(task.task_ref, set()) & failed)
                if upstream:
                    # Running it would read state its writer never produced,
                    # and a worker has no way to tell that from success.
                    log.warning("plan %s: blocking %s (upstream %s failed)",
                                plan_id, task.task_ref, upstream[0])
                    result.blocked.append((task.task_ref, upstream[0]))
                    await asyncio.to_thread(persist_blocked, config, plan_id, task,
                                            wave_index, upstream[0])
                    continue
                runnable.append(task)

            if not runnable:
                continue

            wave_declared = declared_paths(runnable)
            before = await asyncio.to_thread(
                snapshot, stored.workspace_root, wave_declared, ignore=ignore
            )

            sem = asyncio.Semaphore(max(1, config.max_parallel))
            started += len(runnable)
            worker_results = await asyncio.gather(
                *(run_one(task, wave_index, sem) for task in runnable)
            )

            # Taken after every process in the wave has exited -- not when it
            # said it was done. A grandchild still writing would otherwise be
            # hashed mid-write.
            after = await asyncio.to_thread(
                snapshot, stored.workspace_root, wave_declared, ignore=ignore
            )
            # One worker in the wave means an undeclared change has exactly one
            # possible author; more than one and it honestly does not.
            attribution = "task" if len(runnable) == 1 else "wave"

            for task, worker_result in zip(runnable, worker_results):
                report = compare(before, after, task, wave_declared=wave_declared,
                                 attribution=attribution)
                result.taints[task.task_ref] = report
                if report.tainted:
                    log.warning("plan %s: %s tainted -- %s", plan_id, task.task_ref,
                                report.describe())
                await asyncio.shield(
                    asyncio.to_thread(persist_taint, config, plan_id, task.task_ref, report)
                )
                result.results.append(worker_result)
                if not worker_result.ok:
                    failed.add(task.task_ref)

            waves_done += 1
            await report_progress(waves_done, len(waves),
                            f"wave {wave_index}: {len(runnable)} worker(s)")

            # Escalation runs after the wave, so a retry cannot overlap the
            # snapshot window it would otherwise pollute.
            recovered = await _escalate_wave(
                runnable, worker_results, result, config, plan_id, wave_index,
                sem, run_one, began, deadline_s,
            )
            failed -= recovered
            started += len(result.escalated)

    except asyncio.CancelledError:
        # Everything already written stays written, and run_worker kills its
        # own tree on the way out, so nothing is left spending tokens.
        log.warning("plan %s: execution cancelled after %d wave(s)", plan_id, waves_done)
        result.outcome = "cancelled_at_deadline"
        _finish_execution(config, execution_id, plan_id, result.outcome, started,
                          result.completed, waves_done)
        raise

    if any(not r.ok for r in result.results) or result.blocked:
        result.outcome = "partial"

    _finish_execution(
        config, execution_id, plan_id, result.outcome, started,
        result.completed, waves_done,
    )
    return result


async def _escalate_wave(runnable, worker_results, result, config, plan_id,
                         wave_index, sem, run_one, began, deadline_s) -> set[str]:
    """Retry this wave's eligible failures once on the stronger model.

    Sequential, not gathered: an escalation is already the expensive path, and
    firing several at once is the surest way to turn a partial result into a
    cancelled one.
    """
    recovered: set[str] = set()
    for task, worker_result in zip(runnable, worker_results):
        if not should_escalate(worker_result, result.taints.get(task.task_ref)):
            continue
        elapsed = time.monotonic() - began
        if not fits_remaining_deadline(elapsed, config, deadline_s):
            log.warning("plan %s: not escalating %s -- %.0fs elapsed of a %ss deadline",
                        plan_id, task.task_ref, elapsed, deadline_s)
            continue

        log.info("plan %s: escalating %s to %s", plan_id, task.task_ref,
                 config.model_escalate)
        retry = await run_one(task, wave_index, sem, model=config.model_escalate)
        result.escalated.append(task.task_ref)
        # Replace the failed record rather than appending: the plan has one
        # task, and two rows for it would make `collect` ambiguous.
        result.results = [r for r in result.results if r.task_ref != task.task_ref]
        result.results.append(retry)
        if retry.ok:
            recovered.add(task.task_ref)
    return recovered


def collect_plan(config: Config, plan_id: str) -> list[dict]:
    """Read back whatever has completed, including after a cancellation."""
    conn = init_db(config.db_path)
    try:
        if conn.execute("SELECT 1 FROM plans WHERE id = ?", (plan_id,)).fetchone() is None:
            raise PlanRefused("no such plan", plan_id)
        rows = conn.execute(
            "SELECT r.task_ref, r.wave_index, r.status, r.exit_reason, r.tokens_in,"
            " r.tokens_out, r.thinking_tokens, r.cache_read_tokens, r.finished_at,"
            " r.tainted, r.tainted_paths, res.summary, res.content_bytes"
            " FROM runs r LEFT JOIN results res ON res.run_id = r.id"
            " WHERE r.plan_id = ? ORDER BY r.wave_index, r.task_ref",
            (plan_id,),
        ).fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows]
