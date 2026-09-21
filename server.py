"""Ephemeral sub-agent orchestrator -- MCP server.

Three tools: propose_plan (read-only), execute_plan (spawns workers), collect.

Targets MCP Python SDK **v2** (`mcp` 2.2.0) and protocol 2026-07-28. The v1 API
(`FastMCP`, `get_context()`, `inputSchema`) is wrong here -- see CLAUDE.md.

stdout is the MCP protocol channel. Nothing in this process may `print()`:
logging goes to a file and to stderr. One stray line on stdout makes the host
drop the connection, which some hosts render as a server with zero tools.
"""

from __future__ import annotations

import logging
import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path

# --- SDK v2 imports, verified against the installed package ---------------
from mcp.server import MCPServer
from mcp.server.mcpserver import Context
from mcp.server.mcpserver.exceptions import ToolError

from subagents import __version__, cache
from subagents.client_config import apply_timeout, detect_timeout, recommended_block
from subagents.config import load_config, unknown_keys
from subagents.errors import PlanRefused
from subagents.execution import collect_plan, execute
from subagents.worker import DEPTH_ENV_VAR
from subagents.instructions import INSTRUCTIONS
from subagents.planning import propose, render
from subagents.rendering import render_collection, render_execution

CONFIG = load_config()
SERVER_FILE = Path(__file__).resolve()

# Read the client's own config ONCE, at startup. That is exactly what the
# client loaded when it spawned us: editing the file later does not change
# this session's deadline, so a fresh read per call would only mislead.
CLIENT_TIMEOUT = detect_timeout(SERVER_FILE)

# Set by us on every worker we spawn. Present here means this server is running
# inside a worker, which may use every other tool but must not fan out again.
WORKER_DEPTH = os.environ.get(DEPTH_ENV_VAR, "")

logging.basicConfig(
    filename=str(CONFIG.log_file),
    level=getattr(logging, CONFIG.log_level.upper(), logging.INFO),
    format="%(asctime)s | %(levelname)s | %(message)s",
    force=True,
)
log = logging.getLogger("subagents")

# Mirror to stderr, which the spec says the client may capture and must not
# treat as an error channel. Never stdout.
_stderr = logging.StreamHandler(sys.stderr)
_stderr.setFormatter(logging.Formatter("SUBAGENTS | %(levelname)s | %(message)s"))
log.addHandler(_stderr)

mcp = MCPServer("subagents", version=__version__, instructions=INSTRUCTIONS)


@mcp.tool()
async def propose_plan(tasks_json: str, workspace_root: str, ctx: Context) -> str:
    """Validate a set of sub-tasks and return an executable plan.

    Call this BEFORE execute_plan, always. It is cheap and read-only: it spawns
    no subprocesses and changes nothing on disk.

    Validates every declared path against the workspace boundary, classifies
    each task's action tier, refuses plans where two tasks write the same file
    or whose read/write dependencies form a cycle, groups the rest into waves
    that can safely run in parallel, and estimates wall-clock.

    Args:
        tasks_json: JSON list of tasks. Each needs `task_ref` (short name you
            choose, charset [a-z0-9-], max 40 chars, unique in the plan),
            `instruction` (self-contained -- the worker sees no history),
            `reads` (every file it will read) and `writes` (every file it will
            modify or create). Declare `reads` as carefully as `writes`.
        workspace_root: Absolute path to the workspace. Must be on the server's
            allowlist; every declared path must resolve inside it.

    Returns:
        A readable plan including plan_id and plan_digest, both required by
        execute_plan. Relay it to the human for approval.
    """
    # NOT ctx.info(): the MCP logging capability is deprecated as of protocol
    # 2026-07-28 (SEP-2577) and emits a deprecation warning. The file logger is
    # the supported path, and request_id keeps lines correlatable.
    log.info("propose_plan [%s]: workspace_root=%s", ctx.request_id, workspace_root)
    try:
        plan = propose(tasks_json, workspace_root, CONFIG, CLIENT_TIMEOUT)
    except PlanRefused as exc:
        log.warning("plan refused: %s", exc)
        # A refusal is an outcome the parent must act on, not a crash. The
        # message names what to change; "refused" alone would just get retried.
        raise ToolError(f"PLAN REFUSED: {exc}") from exc

    log.info(
        "plan %s proposed: %d worker(s), %d wave(s), digest=%s",
        plan.plan_id,
        plan.estimate.worker_count,
        plan.estimate.wave_count,
        plan.plan_digest[:12],
    )
    return render(plan)


@mcp.tool()
async def execute_plan(
    affects: str,          # named for the approval prompt, not for us -- see below
    plan_id: str,
    plan_digest: str,
    ctx: Context,
) -> str:
    """Run an approved plan's sub-tasks as isolated workers.

    Requires a plan_id and plan_digest from a previous propose_plan call. The
    digest is recomputed and checked, so a stale or altered plan is refused
    rather than run under an approval granted for something else.

    Args:
        affects: Short, human-readable description of what will be touched,
            e.g. "edits pkg/config.py and pkg/server.py; no deletes". This is
            the text the human sees in the approval prompt, which TRUNCATES
            arguments -- so lead with the paths and the action. It is recorded
            verbatim and is not checked against the plan.
        plan_id: From propose_plan.
        plan_digest: From propose_plan. Refused if it does not match.

    Returns:
        Per-task status, summary and token usage. Full transcripts stay in the
        database behind the handle; call collect(plan_id) to read them back,
        including after a timeout.
    """
    # The parameter is `affects` on the wire and scope_summary everywhere
    # inside. Measured, 2026-09-19: the client writes its cached copy of our
    # schema with `properties` sorted ALPHABETICALLY, and the model emits
    # arguments in that order -- so declaration order does not reach the human,
    # the first key alphabetically does. With `scope_summary` the prompt led
    # with `{"plan_digest":"30bccf8b20d6f83a...`, spending the entire ~40-char
    # preview on a hash. A short name sorting before `plan_*` is the only lever
    # we have, and every character of the key is a character of scope the human
    # does not get to read. See NOTES.md section 20.
    scope_summary = affects
    log.info("execute_plan [%s]: plan=%s scope=%r", ctx.request_id, plan_id, scope_summary[:80])

    if WORKER_DEPTH:
        # This server is running inside a worker we spawned. Workers keep every
        # other MCP server, tool and skill -- they just cannot fan out again,
        # which would make process growth unbounded.
        raise ToolError(
            "PLAN REFUSED: this session is itself a sub-agent worker "
            f"({DEPTH_ENV_VAR}={WORKER_DEPTH}), so it cannot launch nested workers. "
            "Do the work directly, or report back to the parent."
        )

    try:
        outcome = await execute(
            scope_summary, plan_id, plan_digest, CONFIG,
            # Not deprecated (SEP-2577 removed the logging capability, not
            # this), but whether agy renders it is unverified -- so it is
            # emitted and never depended on.
            progress=lambda done, total, note: ctx.report_progress(done, total, note),
            # The real number read from the client's own config, so an
            # escalation retry can be refused on arithmetic rather than hope.
            deadline_s=CLIENT_TIMEOUT.effective_s if CLIENT_TIMEOUT.known else None,
        )
    except PlanRefused as exc:
        log.warning("execution refused: %s", exc)
        raise ToolError(f"PLAN REFUSED: {exc}") from exc

    return render_execution(outcome)


@mcp.tool()
async def collect(plan_id: str, ctx: Context) -> str:
    """Read back the results of a plan, including after a timeout.

    If execute_plan was cancelled at the client's deadline, the workers that
    finished have already written their results -- this returns them. Call this
    rather than retrying execute_plan, which would re-run everything.
    """
    log.info("collect [%s]: plan=%s", ctx.request_id, plan_id)
    try:
        rows = collect_plan(CONFIG, plan_id)
    except PlanRefused as exc:
        raise ToolError(f"NOT FOUND: {exc}") from exc
    return render_collection(plan_id, rows)


def _report(message: str) -> None:
    """User-facing CLI output.

    Goes to stderr, not stdout, even though the CLI paths never serve the
    protocol. Keeping "nothing in this process writes to stdout" absolute is
    easier to reason about and to test than a rule with exceptions.
    """
    sys.stderr.write(message + "\n")


def check_agy() -> bool:
    """Is the CLI this whole server delegates to actually runnable?

    `--check-config` validated the client's JSON and never checked the binary
    every worker depends on, which is the single most likely first-run failure
    for someone who is not me: `agy` installed somewhere else, or not at all.
    A wrong answer here surfaces much later as `spawn_error` on every task.

    Reported, never thrown. Config checking has to keep working on a machine
    where the CLI is not installed yet -- that user needs the report most.
    """
    resolved = shutil.which(CONFIG.agy_path) or (
        CONFIG.agy_path if Path(CONFIG.agy_path).is_file() else None
    )
    if resolved is None:
        _report(f"  agy: NOT FOUND -- {CONFIG.agy_path!r} is not on PATH and is not a file.")
        _report("  Every worker would fail to spawn. Set SUBAGENTS_AGY_PATH in .env")
        _report("  to the absolute path of the CLI.")
        return False
    try:
        proc = subprocess.run([resolved, "--version"], capture_output=True, text=True,
                              timeout=30)
    except (OSError, subprocess.SubprocessError) as exc:
        _report(f"  agy: found at {resolved} but would not run -- {exc}")
        return False
    if proc.returncode != 0:
        _report(f"  agy: {resolved} exited {proc.returncode} on --version")
        return False
    _report(f"  agy: {(proc.stdout or proc.stderr).strip() or '(no version output)'}")
    _report(f"       {resolved}")
    return True


def check_config() -> int:
    """Report the deadline the client will actually enforce, and the CLI."""
    found = CLIENT_TIMEOUT
    _report(f"platform: {platform.system()} {platform.release()} | python {sys.version.split()[0]}")
    if platform.system() != "Windows":
        # Stated rather than discovered later. The kill path here is a process
        # group, which does NOT die with its creator -- so a hard kill of this
        # server can leave workers running. test_hard_kill.py skips off Windows
        # because the guarantee does not exist to test.
        _report("  NOTE: measured and supported on Windows. Elsewhere the worker kill")
        _report("  falls back to a process group, which survives a hard kill of this")
        _report("  server. Untested on this platform -- see README, Platform.")
    agy_ok = check_agy()
    _report(f"config: {found.config_path}")

    if not found.readable:
        _report("  UNREADABLE -- cannot tell what deadline applies.")
        _report(f"  Expected an entry like:\n{recommended_block(SERVER_FILE)}")
        return 1

    if not found.known:
        _report(f"  no entry has args pointing at {SERVER_FILE}")
        _report(f"  This server is not registered. Add:\n{recommended_block(SERVER_FILE)}")
        return 1

    _report(f"  entry: {found.server_name!r}")
    if not found.is_explicit:
        _report(
            f"  timeoutSeconds: NOT SET -- calls are cancelled at 180s.\n"
            f"  A single worker at a {CONFIG.worker_timeout_s}s budget already exceeds that.\n"
            f"  Fix with: python server.py --fix-config"
        )
        return 1

    one_worker = CONFIG.worker_timeout_s + 10
    _report(f"  timeoutSeconds: {found.timeout_s}s")
    if found.timeout_s < one_worker:
        _report(
            f"  TOO LOW -- one worker can take {one_worker}s "
            f"({CONFIG.worker_timeout_s}s budget + ~10s spawn). Raise it to at least that."
        )
        return 1

    _report(f"  OK -- fits a single worker ({one_worker}s); multi-wave plans may still exceed it.")
    # The deadline can be perfect and the install still unusable. Both have to
    # hold for exit 0 to mean "this will work".
    return 0 if agy_ok else 1


def fix_config() -> int:
    """Set timeoutSeconds to 900. Deliberate, never automatic."""
    try:
        _report(apply_timeout(SERVER_FILE, 900))
    except (OSError, RuntimeError, ValueError) as exc:
        _report(f"could not update config: {exc}")
        return 1
    return 0


def prune_cache() -> int:
    """Drop expired entries and say what is left.

    Execution prunes on its own before anything spawns; this is for a human who
    wants to know what the cache is holding, or wants it gone after changing
    something the key does not cover.
    """
    removed = cache.prune(CONFIG)
    _report(f"cache: pruned {removed} expired entr{'y' if removed == 1 else 'ies'}, "
            f"{cache.count(CONFIG)} live (ttl={CONFIG.cache_ttl_s}s)")
    return 0


def main() -> None:
    if "--check-config" in sys.argv:
        raise SystemExit(check_config())
    if "--fix-config" in sys.argv:
        raise SystemExit(fix_config())
    if "--prune-cache" in sys.argv:
        raise SystemExit(prune_cache())

    # The effective values, not just the paths. A .env that failed to apply --
    # a BOM, a typo, a server not restarted -- is otherwise indistinguishable
    # from one that worked, and the difference only shows up as a worker that
    # runs for the wrong length of time. See NOTES.md section 23.
    log.info(
        "subagents %s starting | python=%s | db=%s | roots=%s | "
        "worker_timeout=%ss | max_parallel=%s | model=%s | cache_ttl=%ss",
        __version__,
        sys.version.split()[0],
        CONFIG.db_path,
        [str(r) for r in CONFIG.allowed_roots],
        CONFIG.worker_timeout_s,
        CONFIG.max_parallel,
        CONFIG.model,
        CONFIG.cache_ttl_s,
    )
    for key in unknown_keys(CONFIG._dotenv):
        log.warning(".env sets %s, which this server never reads -- misspelled?", key)
    if not CLIENT_TIMEOUT.is_explicit:
        log.warning(
            "client timeoutSeconds is not set (config=%s, entry=%s): tool calls will be "
            "cancelled at 180s. Run: python server.py --fix-config",
            CLIENT_TIMEOUT.config_path,
            CLIENT_TIMEOUT.server_name,
        )
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
