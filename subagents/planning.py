"""propose_plan: parse, validate, schedule, estimate, persist, render."""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .config import Config
from .db import init_db
from .digest import compute_digest
from .errors import PlanRefused
from .estimate import Estimate, estimate_plan
from .models import Task
from .tiers import Tier, classify_task, summarise
from .validate import (
    resolve_declared_path,
    resolve_workspace_root,
    validate_task_ref,
    validate_unique_task_refs,
)
from .waves import group_into_waves

MAX_TASKS = 32
MAX_INSTRUCTION_CHARS = 8000


@dataclass(frozen=True)
class ProposedPlan:
    plan_id: str
    plan_digest: str
    workspace_root: Path
    waves: list[list[Task]]
    tiers: dict[str, Tier]
    estimate: Estimate
    warnings: list[str]


def _now() -> datetime:
    return datetime.now(timezone.utc)


def parse_tasks_json(raw: str) -> list[dict]:
    """Accept either a bare list or a {"tasks": [...]} wrapper.

    Failure messages name the expected shape: the parent is a model, and
    "invalid JSON" alone gives it nothing to correct.
    """
    if not isinstance(raw, str) or not raw.strip():
        raise PlanRefused("tasks_json is required", "expected a JSON string")
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise PlanRefused("tasks_json is not valid JSON", str(exc)) from exc

    if isinstance(parsed, dict):
        parsed = parsed.get("tasks")
    if not isinstance(parsed, list):
        raise PlanRefused(
            "tasks_json must be a list of tasks",
            'expected [{"task_ref": ..., "instruction": ..., "reads": [], "writes": []}]',
        )
    if not parsed:
        raise PlanRefused("tasks_json contains no tasks")
    if len(parsed) > MAX_TASKS:
        raise PlanRefused("too many tasks", f"{len(parsed)} tasks; limit is {MAX_TASKS}")
    return parsed


def _string_list(value: object, *, field: str, task_ref: str) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        raise PlanRefused(
            f"{task_ref}: {field} must be a list, not a string",
            f"wrap it: [{value!r}]",
        )
    if not isinstance(value, list):
        raise PlanRefused(f"{task_ref}: {field} must be a list")
    return value


def build_tasks(raw_tasks: list[dict], root: Path, default_model: str) -> list[Task]:
    refs: list[str] = []
    for entry in raw_tasks:
        if not isinstance(entry, dict):
            raise PlanRefused("each task must be a JSON object", repr(entry)[:80])
        refs.append(validate_task_ref(entry.get("task_ref")))
    validate_unique_task_refs(refs)

    tasks: list[Task] = []
    for entry, ref in zip(raw_tasks, refs):
        instruction = entry.get("instruction")
        if not isinstance(instruction, str) or not instruction.strip():
            raise PlanRefused(f"{ref}: instruction is required")
        if len(instruction) > MAX_INSTRUCTION_CHARS:
            raise PlanRefused(
                f"{ref}: instruction too long",
                f"{len(instruction)} chars; limit is {MAX_INSTRUCTION_CHARS}",
            )

        reads = {
            str(resolve_declared_path(p, root, field="reads", task_ref=ref))
            for p in _string_list(entry.get("reads"), field="reads", task_ref=ref)
        }
        writes = {
            str(resolve_declared_path(p, root, field="writes", task_ref=ref))
            for p in _string_list(entry.get("writes"), field="writes", task_ref=ref)
        }

        model = entry.get("model") or default_model
        if not isinstance(model, str):
            raise PlanRefused(f"{ref}: model must be a string")

        # sorted(), never bare set iteration: set order can vary between
        # processes, which would make the digest unstable.
        tasks.append(
            Task(
                task_ref=ref,
                instruction=instruction.strip(),
                reads=tuple(sorted(reads)),
                writes=tuple(sorted(writes)),
                model=model,
            )
        )
    return tasks


def propose(tasks_json: str, workspace_root: str, config: Config) -> ProposedPlan:
    root = resolve_workspace_root(workspace_root, config.allowed_roots)
    tasks = build_tasks(parse_tasks_json(tasks_json), root, config.model)

    tiers: dict[str, Tier] = {}
    never_reasons: list[str] = []
    for task in tasks:
        tier, reasons = classify_task(task, root)
        tiers[task.task_ref] = tier
        never_reasons.extend(reasons)
    if never_reasons:
        raise PlanRefused("plan contains never-tier actions", "; ".join(never_reasons))

    waves = group_into_waves(tasks)
    estimate = estimate_plan(
        waves, max_parallel=config.max_parallel, worker_timeout_s=config.worker_timeout_s
    )

    warnings: list[str] = []
    for task in tasks:
        if task.declares_no_reads:
            warnings.append(
                f"{task.task_ref}: declares no reads[]. Taint verification cannot apply; "
                "results from this task will not be marked verified."
            )
    if estimate.warning:
        warnings.append(estimate.warning)

    plan = ProposedPlan(
        plan_id=uuid.uuid4().hex[:12],
        plan_digest=compute_digest(str(root), tasks),
        workspace_root=root,
        waves=waves,
        tiers=tiers,
        estimate=estimate,
        warnings=warnings,
    )
    _persist(plan, tasks, config)
    return plan


def _persist(plan: ProposedPlan, tasks: list[Task], config: Config) -> None:
    created = _now()
    expires = created + timedelta(seconds=config.plan_ttl_s)

    wave_of = {t.task_ref: i for i, wave in enumerate(plan.waves) for t in wave}
    plan_json = json.dumps(
        [
            {
                "task_ref": t.task_ref,
                "instruction": t.instruction,
                "reads": list(t.reads),
                "writes": list(t.writes),
                "model": t.model,
                "wave_index": wave_of[t.task_ref],
            }
            for t in sorted(tasks, key=lambda t: t.task_ref)
        ],
        sort_keys=True,
    )

    conn = init_db(config.db_path)
    try:
        conn.execute(
            "INSERT INTO plans (id, plan_digest, workspace_root, plan_json, tier_summary,"
            " wave_count, estimated_wall_s, worst_case_wall_s, warnings, created_at,"
            " expires_at, status)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                plan.plan_id,
                plan.plan_digest,
                str(plan.workspace_root),
                plan_json,
                summarise(plan.tiers),
                plan.estimate.wave_count,
                plan.estimate.expected_s,
                plan.estimate.worst_case_s,
                json.dumps(plan.warnings),
                created.isoformat(),
                expires.isoformat(),
                "proposed",
            ),
        )
        conn.commit()
    finally:
        conn.close()


def render(plan: ProposedPlan) -> str:
    lines: list[str] = []
    add = lines.append

    add(f"PLAN {plan.plan_id}")
    add(f"  workspace_root : {plan.workspace_root}")
    add(f"  plan_digest    : {plan.plan_digest}")
    add(f"  tiers          : {summarise(plan.tiers)}")
    add(
        f"  schedule       : {plan.estimate.worker_count} worker(s) in "
        f"{plan.estimate.wave_count} wave(s), max_parallel={plan.estimate.max_parallel}"
    )
    add(
        f"  estimate       : ~{plan.estimate.expected_s}s expected, "
        f"~{plan.estimate.worst_case_s}s worst case"
    )
    add("")

    for index, wave in enumerate(plan.waves):
        how = "in parallel" if len(wave) > 1 else "alone"
        add(f"WAVE {index} ({len(wave)} task(s), {how})")
        for task in wave:
            add(f"  [{task.task_ref}] tier={plan.tiers[task.task_ref].name.lower()}")
            add(f"      {task.instruction}")
            add(f"      reads : {', '.join(task.reads) if task.reads else '(none)'}")
            add(f"      writes: {', '.join(task.writes) if task.writes else '(none)'}")
        add("")

    if plan.warnings:
        add("WARNINGS")
        for warning in plan.warnings:
            add(f"  {warning}")
        add("")

    add("NEXT")
    add(
        f"  execute_plan(plan_id={plan.plan_id!r}, plan_digest={plan.plan_digest!r}, "
        "scope_summary=<describe what will be touched>)"
    )
    add("  If that call is cancelled, call collect(plan_id) -- do not retry.")
    return "\n".join(lines)
