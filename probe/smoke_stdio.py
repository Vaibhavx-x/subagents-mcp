"""Stdio smoke test: connect to probe_server.py as a real subprocess.

This catches what the in-memory tests cannot -- a stray write to stdout, a
logging handler pointed at stdout, an import-time side effect. If this hangs
or fails to parse a message, the problem is on stdout.

Run:  python smoke_stdio.py      (exit 0 = clean)

Never prints to stdout on the server side; this client script may print,
because it is not the one speaking JSON-RPC over its own stdout.
"""

from __future__ import annotations

import os
import sys

import anyio
from mcp import Client, StdioServerParameters

HERE = os.path.dirname(os.path.abspath(__file__))

# The SDK's stdio client passes an allow-listed environment, not the full one.
# PATH is on that allow-list, so `agy` stays findable; we add nothing here on
# purpose, so this run measures the same minimal environment a strict host
# would give the server.
params = StdioServerParameters(
    command=sys.executable,
    args=[os.path.join(HERE, "probe_server.py")],
    cwd=HERE,
)


async def main() -> int:
    failures: list[str] = []

    async with Client(params) as client:
        tools = await client.list_tools()
        names = [t.name for t in tools.tools] if hasattr(tools, "tools") else [t.name for t in tools]
        sys.stderr.write(f"SMOKE | tools: {names}\n")
        if len(names) != 5:
            failures.append(f"expected 5 tools over stdio, got {len(names)}: {names}")

        r1 = await client.call_tool("whoami", {})
        t1 = "".join(getattr(b, "text", "") for b in r1.content)
        sys.stderr.write(f"SMOKE | whoami is_error={r1.is_error} -> {t1}\n")
        if r1.is_error:
            failures.append(f"whoami errored: {t1}")

        r2 = await client.call_tool("ask_once", {"topic": "auto"})
        t2 = "".join(getattr(b, "text", "") for b in r2.content)
        sys.stderr.write(f"SMOKE | ask_once(auto) is_error={r2.is_error} -> {t2}\n")
        if r2.is_error:
            failures.append(f"ask_once(auto) errored: {t2}")
        elif "ACCEPTED proceed=True" not in t2:
            failures.append(f"ask_once(auto) unexpected result: {t2}")

    if failures:
        for f in failures:
            sys.stderr.write(f"SMOKE FAIL | {f}\n")
        return 1
    sys.stderr.write("SMOKE | OK - stdio transport clean, stdout uncontaminated\n")
    return 0


if __name__ == "__main__":
    sys.exit(anyio.run(main))
