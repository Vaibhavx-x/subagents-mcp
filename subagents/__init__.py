"""Ephemeral sub-agent orchestrator.

Validates a plan, spawns each task as a separate `agy` process, hashes the
workspace before and after, and reuses work already done. `server.py` is the
MCP entry point; `CHANGELOG.md` records what each phase added.

IMPORTANT: stdout is the MCP protocol channel. Nothing in this package may
`print()`. Use the `logging` configured in `server.py`, which writes to a file
and to stderr.
"""

# Logged at startup, so it is worth keeping true: a server reporting 0.1.0
# while the repo is tagged v1.0 makes every log line ambiguous about which
# code is actually running.
__version__ = "1.0.0"
