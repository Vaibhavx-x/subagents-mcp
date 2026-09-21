"""
Capability probe for the sub-agent orchestrator project.

Five tools, each answering one unresolved question about the real client.
Everything is logged to probe.live.log, which is gitignored (NEVER stdout -
stdout is the protocol channel). The committed probe.log is the recorded
session behind RESULTS.md and is not written to.

Run:  agy mcp add probe -- python /abs/path/probe_server.py
      (or add to Antigravity's MCP config manually; see README.md)

This targets MCP Python SDK v2 / protocol 2026-07-28.
Written from the docs, not yet run against the SDK: expect to fix
one or two import paths on first run. Every such fix is a NOTES.md entry.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import platform
import shutil
import sys
import time
from typing import Annotated, Any

from pydantic import BaseModel, Field

# --- v2 imports. If any of these fail, the traceback names the real path. ---
from mcp.server import MCPServer
from mcp.server.mcpserver import (
    AcceptedElicitation,
    CancelledElicitation,
    DeclinedElicitation,
    Context,
    Elicit,
    ElicitationResult,
    Resolve,
)
from mcp.types import InputRequiredResult

# probe.live.log, NOT probe.log. `probe.log` is committed evidence -- the
# recorded capability-probe session that probe/RESULTS.md is derived from -- and
# this server is still registered in the client, so it starts whenever the
# client does. Appending to the committed file left the repo permanently dirty,
# mutated the artifact the results cite, and held a lock that made `git stash`
# fail outright. A live log belongs in a gitignored file; the evidence belongs
# in git, unchanging. PROBE_LOG still overrides.
LOG_PATH = os.environ.get(
    "PROBE_LOG",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "probe.live.log"),
)

logging.basicConfig(
    filename=LOG_PATH,
    level=logging.INFO,
    format="%(asctime)s | %(message)s",
    force=True,
)
log = logging.getLogger("probe")

# Also mirror to stderr, which the spec says the client may capture and
# must not treat as an error channel. Never stdout.
_stderr = logging.StreamHandler(sys.stderr)
_stderr.setFormatter(logging.Formatter("PROBE | %(message)s"))
log.addHandler(_stderr)


# --- call counters. Tests read and reset these; the log lines already imply them. ---
COUNTS: dict[str, int] = {}


def bump(key: str) -> int:
    COUNTS[key] = COUNTS.get(key, 0) + 1
    return COUNTS[key]


def reset_counts() -> None:
    COUNTS.clear()


def jdump(obj: Any) -> str:
    """Best-effort JSON for logging arbitrary SDK objects."""
    try:
        return json.dumps(obj, default=str, indent=2, sort_keys=True)
    except Exception:
        return repr(obj)


def describe_ctx(ctx: Context, label: str) -> dict[str, Any]:
    """Pull whatever this SDK build exposes off the context, without assuming names."""
    out: dict[str, Any] = {"label": label}
    for attr in (
        "protocol_version",
        "request_id",
        "client_params",
        "request_state",
        "input_responses",
        "meta",
    ):
        try:
            out[attr] = getattr(ctx, attr, "<absent>")
        except Exception as e:  # noqa: BLE001
            out[attr] = f"<raised {e!r}>"

    # Capabilities may hang off ctx directly or off request_context; try both.
    for path in ("client_capabilities", "request_context.client_capabilities"):
        obj: Any = ctx
        try:
            for part in path.split("."):
                obj = getattr(obj, part)
            out[path] = obj
        except Exception:
            pass
    return out


mcp = MCPServer("probe", version="0.1.0")


# ---------------------------------------------------------------- Q1/Q2/env
@mcp.tool()
async def whoami(ctx: Context) -> str:
    """Report the protocol version, client capabilities, and process environment."""
    bump("whoami")
    info = describe_ctx(ctx, "whoami")
    log.info("=== WHOAMI ===\n%s", jdump(info))

    env_report = {
        "platform": platform.platform(),
        "python": sys.version.split()[0],
        "cwd": os.getcwd(),
        "env_keys": sorted(os.environ.keys()),
        "PATH_present": "PATH" in os.environ,
        "PATH_entry_count": len(os.environ.get("PATH", "").split(os.pathsep)),
        "agy_on_path": shutil.which("agy"),
        "env_var_count": len(os.environ),
    }
    log.info("=== ENVIRONMENT ===\n%s", jdump(env_report))

    return (
        f"protocol_version={info.get('protocol_version')!r} "
        f"env_vars={env_report['env_var_count']} "
        f"agy_on_path={env_report['agy_on_path']!r} "
        f"(full detail in {LOG_PATH})"
    )


# ------------------------------------------------------------------ env/agy
@mcp.tool()
async def spawn_check() -> str:
    """Try to spawn `agy --version` as a subprocess and report what happened."""
    bump("spawn_check")
    exe = shutil.which("agy") or "agy"
    t0 = time.monotonic()
    try:
        proc = await asyncio.create_subprocess_exec(
            exe,
            "--version",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out, err = await asyncio.wait_for(proc.communicate(), timeout=30)
        result = {
            "resolved_exe": exe,
            "returncode": proc.returncode,
            "stdout": out.decode(errors="replace")[:2000],
            "stderr": err.decode(errors="replace")[:2000],
            "elapsed_s": round(time.monotonic() - t0, 2),
        }
    except FileNotFoundError as e:
        result = {"resolved_exe": exe, "error": f"NOT FOUND: {e}"}
    except asyncio.TimeoutError:
        result = {"resolved_exe": exe, "error": "TIMEOUT after 30s"}
    except Exception as e:  # noqa: BLE001
        result = {"resolved_exe": exe, "error": f"{type(e).__name__}: {e}"}

    log.info("=== SPAWN CHECK ===\n%s", jdump(result))
    return jdump(result)


# --------------------------------------------------------------------- Q3/Q2
class Confirm(BaseModel):
    """Deliberately flat: primitives only, per the elicitation schema rules."""

    proceed: bool = Field(description="Proceed with the operation?")
    note: str = Field(default="", description="Optional note")


async def ask_to_proceed(topic: str) -> Confirm | Elicit[Confirm]:
    """Resolver. Runs on EVERY round, so it must be deterministic and side-effect free.

    The message is derived ONLY from `topic` (a tool argument). No timestamps,
    no uuids: a message that renders differently each round makes every recorded
    answer look stale and the server re-asks until the client's round cap ends
    the call.
    """
    bump("resolver")
    log.info("RESOLVER ask_to_proceed ran | topic=%r", topic)
    if topic == "auto":
        # Proves the no-round-trip path: resolver answers without asking.
        return Confirm(proceed=True, note="auto-approved, no elicitation")
    return Elicit(f"Probe: proceed with {topic!r}?", Confirm)


@mcp.tool()
async def ask_once(
    topic: str,
    answer: Annotated[ElicitationResult[Confirm], Resolve(ask_to_proceed)],
    ctx: Context,
) -> str:
    """Ask the user one question, then report which branch was taken."""
    bump("ask_once_body")
    log.info("=== ASK_ONCE BODY ===\n%s", jdump(describe_ctx(ctx, "ask_once-body")))

    match answer:
        case AcceptedElicitation(data=Confirm(proceed=True)):
            verdict = "ACCEPTED proceed=True"
        case AcceptedElicitation():
            verdict = "ACCEPTED proceed=False"
        case DeclinedElicitation():
            verdict = "DECLINED"
        case CancelledElicitation():
            verdict = "CANCELLED"
        case _:
            verdict = f"UNKNOWN {type(answer).__name__}"

    log.info("ASK_ONCE verdict=%s topic=%r", verdict, topic)
    return f"{verdict} (topic={topic!r})"


# ------------------------------------------------------------------- timeout
@mcp.tool()
async def stall(seconds: int) -> str:
    """Sleep for N seconds, then return. Finds the client's tool-call timeout."""
    bump("stall")
    log.info("STALL start seconds=%d", seconds)
    t0 = time.monotonic()
    try:
        for i in range(seconds):
            await asyncio.sleep(1)
            if (i + 1) % 10 == 0:
                log.info("STALL heartbeat t=%ds", i + 1)
    except asyncio.CancelledError:
        # The client gave up and the SDK cancelled the task. This line is the
        # exact ceiling; without it the log just stops and you cannot tell
        # cancellation from the process being killed.
        log.info("STALL CANCELLED at t=%.1fs (seconds=%d)", time.monotonic() - t0, seconds)
        raise
    elapsed = round(time.monotonic() - t0, 1)
    log.info("STALL completed seconds=%d elapsed=%s", seconds, elapsed)
    return f"slept {elapsed}s"


# ----------------------------------------------------------------- round cap
@mcp.tool()
async def spin(ctx: Context) -> str | InputRequiredResult:
    """Always ask for another round. Counts how many the client will do.

    No `Resolve(...)` parameters here on purpose: a tool that uses resolvers
    may not also return InputRequiredResult.

    Returns request_state only, no input_requests, which the spec treats as
    "not done yet" -- the client should retry after a short backoff.
    """
    bump("spin")
    prior = getattr(ctx, "request_state", None)
    try:
        n = int(prior) if prior else 0
    except (TypeError, ValueError):
        n = 0
    n += 1
    log.info("SPIN round=%d prior_state=%r", n, prior)

    if n >= 50:  # never actually terminate before the client gives up
        return f"spin reached {n} rounds (server-side cap)"
    return InputRequiredResult(request_state=str(n))


if __name__ == "__main__":
    log.info("#" * 60)
    log.info("probe server starting | pid=%d | python=%s", os.getpid(), sys.version.split()[0])
    log.info("log file: %s", LOG_PATH)
    mcp.run(transport="stdio")
