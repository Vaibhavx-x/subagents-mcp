"""Stdio smoke test: connect to server.py as a real subprocess.

This catches what the in-memory tests structurally cannot -- a stray write to
stdout, a logging handler pointed at stdout, an import-time side effect. stdout
IS the protocol channel, and one junk line makes the host drop the connection,
which some hosts render as "a server with zero tools".

Run:  python tests/smoke_stdio.py [path/to/server.py]      (exit 0 = clean)

This client script may write to stderr; it is not the process speaking JSON-RPC
over its own stdout.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import anyio

from mcp import Client, StdioServerParameters

REPO_ROOT = Path(__file__).resolve().parent.parent
EXPECTED_TOOLS = {"propose_plan", "execute_plan", "collect"}


async def run(server_path: Path) -> int:
    failures: list[str] = []
    params = StdioServerParameters(
        command=sys.executable,
        args=[str(server_path)],
        cwd=str(REPO_ROOT),
    )

    async with Client(params) as client:
        # 1. handshake reached us at all -- if stdout were polluted we would
        #    not get here.
        sys.stderr.write(f"SMOKE | protocol: {client.protocol_version}\n")

        # 2. instructions survive the wire. This is the lever that tells the
        #    parent agent to call propose_plan before execute_plan.
        instructions = client.instructions or ""
        sys.stderr.write(f"SMOKE | instructions: {len(instructions)} chars\n")
        if not instructions.strip():
            failures.append("server sent no instructions")
        elif "propose_plan" not in instructions:
            failures.append("instructions do not mention propose_plan")

        # 3. exactly the tools we expect
        listed = await client.list_tools()
        tools = listed.tools if hasattr(listed, "tools") else listed
        names = {t.name for t in tools}
        sys.stderr.write(f"SMOKE | tools: {sorted(names)}\n")
        if names != EXPECTED_TOOLS:
            failures.append(f"expected {sorted(EXPECTED_TOOLS)}, got {sorted(names)}")

        # 4. a real round trip
        tasks = (
            '[{"task_ref": "smoke-read", "instruction": "read the readme",'
            ' "reads": ["README.md"], "writes": []}]'
        )
        result = await client.call_tool(
            "propose_plan",
            {"tasks_json": tasks, "workspace_root": str(REPO_ROOT)},
        )
        text = "".join(getattr(b, "text", "") for b in result.content)
        sys.stderr.write(f"SMOKE | propose_plan is_error={result.is_error}\n")
        if result.is_error:
            failures.append(f"propose_plan errored: {text[:200]}")
        elif "plan_digest" not in text:
            failures.append(f"propose_plan returned no digest: {text[:200]}")

        # 5. collect on an unknown plan is a clean error, not a broken stream
        missing = await client.call_tool("collect", {"plan_id": "nosuchplan"})
        if not missing.is_error:
            failures.append("collect on an unknown plan_id did not error")

        # 6. execute_plan refuses a bad digest without running anything
        refused = await client.call_tool(
            "execute_plan",
            {
                "scope_summary": "smoke test: should never execute anything at all",
                "plan_id": "nosuchplan",
                "plan_digest": "0" * 64,
            },
        )
        if not refused.is_error:
            failures.append("execute_plan accepted an unknown plan")

        # 7. a refusal still travels as a clean error, not a broken stream
        bad = await client.call_tool(
            "propose_plan",
            {
                "tasks_json": '[{"task_ref": "esc", "instruction": "i", "reads": ["../x"]}]',
                "workspace_root": str(REPO_ROOT),
            },
        )
        if not bad.is_error:
            failures.append("path escape was not refused over stdio")

    for failure in failures:
        sys.stderr.write(f"SMOKE FAIL | {failure}\n")
    if failures:
        return 1
    sys.stderr.write("SMOKE | clean\n")
    return 0


def main() -> int:
    server_path = Path(sys.argv[1]) if len(sys.argv) > 1 else REPO_ROOT / "server.py"
    if not server_path.is_file():
        sys.stderr.write(f"SMOKE FAIL | no such server: {server_path}\n")
        return 2
    try:
        return anyio.run(run, server_path)
    except Exception as exc:  # noqa: BLE001
        # A protocol-stream failure typically surfaces here as a parse or
        # connection error rather than a clean assertion.
        sys.stderr.write(f"SMOKE FAIL | {type(exc).__name__}: {exc}\n")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
