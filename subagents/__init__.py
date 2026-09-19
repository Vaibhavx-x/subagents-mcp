"""Ephemeral sub-agent orchestrator.

Phase 1 covers validation and planning only: no subprocesses are spawned here.

IMPORTANT: stdout is the MCP protocol channel. Nothing in this package may
`print()`. Use the `logging` configured in `server.py`, which writes to a file
and to stderr.
"""

__version__ = "0.1.0"
