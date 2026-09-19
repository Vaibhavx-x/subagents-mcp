"""Read the CLIENT's own MCP config to find this server's tool-call deadline.

The server cannot set `timeoutSeconds` -- it is client-side config the user
writes -- but it can READ it, which turns a speculative warning into a fact.

Why this matters more than the estimate: a worker's duration is not really
predictable. It may call other MCP servers, install something, or run a test
suite. Sizing a plan from a median is guesswork, but the deadline is a number
sitting in a file, and whether the plan's worst case clears it is decidable.

Why this never WRITES by default: the client reads its config when it spawns
the server, so editing it mid-session would not raise the deadline for the
session that noticed the problem -- the current call would still be cancelled
at 180s. It would also silently edit a shared file that every other MCP server
depends on, possibly overriding a value the user chose deliberately. The
deliberate fix lives behind `python server.py --fix-config` instead.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

# Measured, not documented: the client cancels every tool call at 180s unless
# timeoutSeconds is set.
DEFAULT_DEADLINE_S = 180

CONFIG_PATH = Path.home() / ".gemini" / "config" / "mcp_config.json"


@dataclass(frozen=True)
class ClientTimeout:
    """What the client's config says about this server's deadline."""

    config_path: Path
    server_name: str | None      # our entry in mcpServers, if we found it
    timeout_s: int | None        # as configured; None if unset
    readable: bool               # False if the config is missing or unparseable

    @property
    def effective_s(self) -> int:
        """The deadline that will actually apply."""
        return self.timeout_s if self.timeout_s else DEFAULT_DEADLINE_S

    @property
    def is_explicit(self) -> bool:
        return self.timeout_s is not None

    @property
    def known(self) -> bool:
        """True when we positively identified our own entry."""
        return self.readable and self.server_name is not None


def detect_timeout(server_file: Path, config_path: Path | None = None) -> ClientTimeout:
    """Find our own entry by matching `args` against this server's file path.

    agy injects no identifying environment variables (verified: 12 inherited
    vars, none MCP-specific), so the server's own path is the only reliable
    way to recognise its entry.
    """
    path = config_path or CONFIG_PATH
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ClientTimeout(path, None, None, readable=False)

    try:
        target = server_file.resolve()
    except OSError:
        return ClientTimeout(path, None, None, readable=True)

    for name, spec in (raw.get("mcpServers") or {}).items():
        if not isinstance(spec, dict):
            continue
        for arg in spec.get("args") or []:
            if not isinstance(arg, str):
                continue
            try:
                if Path(arg).resolve() == target:
                    value = spec.get("timeoutSeconds")
                    return ClientTimeout(
                        path,
                        name,
                        int(value) if isinstance(value, (int, float)) else None,
                        readable=True,
                    )
            except OSError:
                continue

    return ClientTimeout(path, None, None, readable=True)


def recommended_block(server_file: Path, server_name: str = "subagents") -> str:
    return json.dumps(
        {
            server_name: {
                "command": "C:/Python313/python.exe",
                "args": [str(server_file).replace("\\", "/")],
                "cwd": str(server_file.parent).replace("\\", "/"),
                "timeoutSeconds": 900,
                "forceAllToolsEager": True,
            }
        },
        indent=2,
    )


def apply_timeout(server_file: Path, timeout_s: int, config_path: Path | None = None) -> str:
    """Set timeoutSeconds on our entry. Deliberate, never automatic.

    Returns a human-readable description of what changed. Raises if the entry
    cannot be found, rather than inventing one -- guessing the command or
    interpreter for somebody else's config would be worse than doing nothing.
    """
    path = config_path or CONFIG_PATH
    raw = json.loads(path.read_text(encoding="utf-8"))

    found = detect_timeout(server_file, path)
    if not found.known:
        raise RuntimeError(
            f"no entry in {path} has args pointing at {server_file}. "
            f"Add one first:\n{recommended_block(server_file)}"
        )

    entry = raw["mcpServers"][found.server_name]
    previous = entry.get("timeoutSeconds")
    if previous == timeout_s:
        return f"{found.server_name}: timeoutSeconds already {timeout_s}; nothing changed"

    backup = path.with_suffix(path.suffix + ".bak")
    backup.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")

    entry["timeoutSeconds"] = timeout_s
    path.write_text(json.dumps(raw, indent=2) + "\n", encoding="utf-8")
    return (
        f"{found.server_name}: timeoutSeconds {previous!r} -> {timeout_s} in {path}\n"
        f"backup written to {backup}\n"
        f"Restart the agy session: the client reads this when it spawns the server."
    )
