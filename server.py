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

# --- SDK v2 imports, verified against the installed package ---------------
from mcp.server import MCPServer
from mcp.server.mcpserver import Context
from mcp.server.mcpserver.exceptions import ToolError

from subagents import __version__
from subagents.config import load_config
from subagents.errors import PlanRefused
from subagents.instructions import INSTRUCTIONS
from subagents.planning import propose, render

CONFIG = load_config()

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
        plan = propose(tasks_json, workspace_root, CONFIG)
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


def main() -> None:
    log.info(
        "subagents %s starting | python=%s | db=%s | roots=%s",
        __version__,
        sys.version.split()[0],
        CONFIG.db_path,
        [str(r) for r in CONFIG.allowed_roots],
    )
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
