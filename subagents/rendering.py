"""Rendering execution and collection results for the parent agent.

These outputs are the product. The whole argument for this project is that a
parent delegating five sub-tasks carries five summaries instead of five full
tool transcripts, so anything returned here has to stay short. Full content
lives in `results` behind the handle and is reachable only by asking for it.
"""

from __future__ import annotations

import json

from .execution import ExecutionOutcome

_STATUS_NOTE = {
    "ok": "",
    "blocked": "never started: an upstream task did not succeed",
    "timeout": "killed at its deadline; partial output stored",
    "failed": "worker reported a failure",
    "unparseable": "worker produced no valid result document",
    "spawn_error": "worker could not be started",
}


def _tokens(row_or_result) -> str:
    get = row_or_result.get if isinstance(row_or_result, dict) else lambda k: getattr(row_or_result, k, None)
    tin, tout = get("tokens_in"), get("tokens_out")
    if tin is None and tout is None:
        return ""
    return f"  tokens: in={tin or 0:,} out={tout or 0:,}"


def render_execution(outcome: ExecutionOutcome) -> str:
    lines: list[str] = []
    add = lines.append

    ran = len(outcome.results)
    add(f"EXECUTION {outcome.plan_id} -- {outcome.outcome}")
    add(f"  {outcome.completed}/{ran} worker(s) succeeded"
        + (f", {len(outcome.skipped)} skipped (already complete)" if outcome.skipped else ""))
    add("")

    for result in sorted(outcome.results, key=lambda r: r.task_ref):
        note = _STATUS_NOTE.get(result.status, "")
        add(f"[{result.task_ref}] {result.status}" + (f" -- {note}" if note else ""))
        add(f"  {result.summary}")
        usage = _tokens(result)
        if usage:
            add(usage)
        if result.task_ref in outcome.escalated:
            add("  retried once on the escalation model")
        report = outcome.taints.get(result.task_ref)
        if report is not None and result.ok and report.writes_unchanged:
            # Not taint, and not a failure the status field would catch: agy
            # can report SUCCESS having produced a real-looking answer while
            # never touching the file. The declared target being byte-identical
            # afterwards is the only evidence of that.
            add("  NOTE: declared but unchanged -- "
                + ", ".join(report.writes_unchanged[:3]))
        if report is not None and report.tainted:
            # Named, not counted. "1 path changed" tells a human nothing they
            # can act on, and this is the only signal that a worker did
            # something the plan did not describe.
            add(f"  TAINTED: {report.describe()}")
            for path in report.paths[:5]:
                add(f"    {path}")
            if len(report.paths) > 5:
                add(f"    ... and {len(report.paths) - 5} more")
        add("")

    if outcome.blocked:
        add("BLOCKED (never started):")
        for task_ref, upstream in outcome.blocked:
            add(f"  [{task_ref}] depends on {upstream}, which did not succeed")
        add("")

    if outcome.skipped:
        add(f"SKIPPED (already complete): {', '.join(outcome.skipped)}")
        add("")

    if outcome.outcome == "cancelled_at_deadline":
        add("CANCELLED at the client deadline. Finished workers were still")
        add(f"  recorded -- call collect({outcome.plan_id}) rather than re-running the plan.")
        return "\n".join(lines)

    tainted = outcome.tainted_refs
    if tainted:
        add(f"REVIEW BEFORE TRUSTING: {', '.join(tainted)} changed paths the plan")
        add("  did not declare. The change was detected, not prevented.")
        add("")

    failed = [r.task_ref for r in outcome.results if not r.ok]
    if failed or outcome.blocked:
        stuck = failed + [ref for ref, _ in outcome.blocked]
        add(f"NOT COMPLETE: {', '.join(sorted(stuck))}")
        add("  Re-propose just these task_refs, or fix the instruction and try again.")
        add(f"  Full transcripts are stored -- call collect({outcome.plan_id}) to read them.")
    else:
        add(f"All workers completed. Full transcripts: collect({outcome.plan_id})")
    return "\n".join(lines)


def render_collection(plan_id: str, rows: list[dict]) -> str:
    if not rows:
        return (
            f"PLAN {plan_id}: nothing has run yet.\n"
            f"  The plan exists but no worker has been started. Call execute_plan."
        )

    lines = [f"COLLECT {plan_id} -- {len(rows)} task(s) on record", ""]
    for row in rows:
        status = row["status"]
        note = _STATUS_NOTE.get(status, "")
        lines.append(
            f"[{row['task_ref']}] wave {row['wave_index']} -- {status}"
            + (f" ({note})" if note else "")
        )
        if row.get("summary"):
            lines.append(f"  {row['summary']}")
        if row.get("exit_reason") and status != "ok":
            lines.append(f"  exit: {row['exit_reason']}")
        usage = _tokens(row)
        if usage:
            lines.append(usage)
        if row.get("content_bytes"):
            lines.append(f"  transcript: {row['content_bytes']:,} bytes stored")
        if row.get("tainted"):
            paths = json.loads(row["tainted_paths"] or "[]")
            lines.append(f"  TAINTED: {len(paths)} undeclared path(s)")
            for path in paths[:5]:
                lines.append(f"    {path}")
        lines.append("")

    incomplete = [r["task_ref"] for r in rows if r["status"] != "ok"]
    if incomplete:
        lines.append(f"NOT COMPLETE: {', '.join(incomplete)}")
        lines.append("  Re-propose these task_refs rather than re-running the whole plan.")
    return "\n".join(lines)
