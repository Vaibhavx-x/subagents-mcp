"""Ephemeral sub-agent orchestrator -- MCP server.

Phase 1: validation and planning only. `propose_plan` spawns no subprocesses.

Targets MCP Python SDK **v2** (`mcp` 2.2.0) and protocol 2026-07-28. The v1 API
(`FastMCP`, `get_context()`, `inputSchema`) is wrong here -- see CLAUDE.md.

stdout is the MCP protocol channel. Nothing in this process may `print()`:
logging goes to a file and to stderr. One stray line on stdout makes the host
drop the connection, which some hosts render as a server with zero tools.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

# --- SDK v2 imports, verified against the installed package ---------------
from mcp.server import MCPServer
from mcp.server.mcpserver import Context
from mcp.server.mcpserver.exceptions import ToolError

from subagents import __version__
from subagents.client_config import apply_timeout, detect_timeout, recommended_block
from subagents.config import load_config
from subagents.errors import PlanRefused
from subagents.instructions import INSTRUCTIONS
from subagents.planning import propose, render

CONFIG = load_config()
SERVER_FILE = Path(__file__).resolve()

# Read the client's own config ONCE, at startup. That is exactly what the
# client loaded when it spawned us: editing the file later does not change
# this session's deadline, so a fresh read per call would only mislead.
CLIENT_TIMEOUT = detect_timeout(SERVER_FILE)

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


def _report(message: str) -> None:
    """User-facing CLI output.

    Goes to stderr, not stdout, even though the CLI paths never serve the
    protocol. Keeping "nothing in this process writes to stdout" absolute is
    easier to reason about and to test than a rule with exceptions.
    """
    sys.stderr.write(message + "\n")


def check_config() -> int:
    """Report the deadline the client will actually enforce."""
    found = CLIENT_TIMEOUT
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
    return 0


def fix_config() -> int:
    """Set timeoutSeconds to 900. Deliberate, never automatic."""
    try:
        _report(apply_timeout(SERVER_FILE, 900))
    except (OSError, RuntimeError, ValueError) as exc:
        _report(f"could not update config: {exc}")
        return 1
    return 0


def main() -> None:
    if "--check-config" in sys.argv:
        raise SystemExit(check_config())
    if "--fix-config" in sys.argv:
        raise SystemExit(fix_config())

    log.info(
        "subagents %s starting | python=%s | db=%s | roots=%s",
        __version__,
        sys.version.split()[0],
        CONFIG.db_path,
        [str(r) for r in CONFIG.allowed_roots],
    )
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
