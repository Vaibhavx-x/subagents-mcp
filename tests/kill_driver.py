"""Starts a worker and then does nothing, so the test can kill IT.

Stands in for the server process being hard-killed mid-run. The worker it
spawns hangs and leaves a grandchild; whether both die is the whole question.
Not a test itself -- run by tests/test_hard_kill.py.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from subagents.worker import run_worker  # noqa: E402


async def main() -> None:
    marker = sys.argv[1]
    cmd = [sys.executable, str(Path(__file__).with_name("fake_worker.py")),
           "--mode", "hang", "--spawn-child", "--marker", marker]
    await run_worker("held", cmd, timeout_s=300, cwd=REPO_ROOT, model="fake-model")


if __name__ == "__main__":
    asyncio.run(main())
