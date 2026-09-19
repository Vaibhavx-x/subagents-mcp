"""Rendering execution and collection results for the parent agent.

These outputs are the product. The whole argument for this project is that a
parent delegating five sub-tasks carries five summaries instead of five full
tool transcripts, so anything returned here has to stay short. Full content
lives in `results` behind the handle and is reachable only by asking for it.
"""

from __future__ import annotations

from .execution import ExecutionOutcome

_STATUS_NOTE = {
    "ok": "",
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

    for result in outcome.results:
        note = _STATUS_NOTE.get(result.status, "")
        add(f"[{result.task_ref}] {result.status}" + (f" -- {note}" if note else ""))
        add(f"  {result.summary}")
        usage = _tokens(result)
        if usage:
            add(usage)
        add("")

    if outcome.skipped:
        add(f"SKIPPED (already complete): {', '.join(outcome.skipped)}")
        add("")

    failed = [r.task_ref for r in outcome.results if not r.ok]
    if failed:
        add(f"NOT COMPLETE: {', '.join(failed)}")
        add("  Re-propose just these task_refs, or fix the instruction and try again.")
        add("  Full transcripts are stored -- call collect(plan_id) to read them.")
    else:
        add("All workers completed. Full transcripts: collect(plan_id)")
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
        lines.append("")

    incomplete = [r["task_ref"] for r in rows if r["status"] != "ok"]
    if incomplete:
        lines.append(f"NOT COMPLETE: {', '.join(incomplete)}")
        lines.append("  Re-propose these task_refs rather than re-running the whole plan.")
    return "\n".join(lines)
