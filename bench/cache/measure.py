"""Cold against warm, with one real worker.

    python bench/cache/measure.py          # spends ONE worker

Runs a two-task plan for real, then re-proposes the identical tasks under a
new plan id and executes again. The second execution must spawn nothing.

Deliberately not a median over repeats. The warm side has no variance to
measure -- it spawns no process and makes no network call -- so repeating it
would produce a tighter confidence interval around a number that is
structurally zero, which is the kind of rigour that looks like rigour. The
cold side's spread is already measured, over 20 real runs, in the p90 the
estimator uses.

Everything lands in a throwaway workspace and a throwaway database, so this
never touches the real audit trail.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import sys
import tempfile
import time
from dataclasses import replace
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from subagents.config import load_config  # noqa: E402
from subagents.execution import execute  # noqa: E402
from subagents.planning import propose  # noqa: E402

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

SCOPE = "writes two summaries into the throwaway measurement workspace; no deletes"

SOURCES = {
    "alpha.py": (
        "def parse_header(text):\n"
        "    '''Split a header block into key/value pairs.'''\n"
        "    return dict(line.split(': ', 1) for line in text.splitlines() if ': ' in line)\n"
    ),
    "beta.py": (
        "def merge_config(base, override):\n"
        "    '''Overlay one mapping on another, shallowly.'''\n"
        "    return {**base, **override}\n"
    ),
}


def tasks_for(root: Path) -> str:
    return json.dumps([
        {
            "task_ref": f"doc-{name.removesuffix('.py')}",
            "instruction": (
                f"Read {name} in the workspace root and write a short description "
                f"of what its function does to docs/{name.removesuffix('.py')}.md. "
                "Do nothing else."
            ),
            "reads": [name],
            "writes": [f"docs/{name.removesuffix('.py')}.md"],
        }
        for name in SOURCES
    ])


def run_once(root: Path, config) -> tuple[float, int, int, int]:
    """(wall seconds, workers spawned, cache hits, input tokens spent)."""
    plan = propose(tasks_for(root), str(root), config)
    began = time.time()
    outcome = asyncio.run(execute(SCOPE, plan.plan_id, plan.plan_digest, config))
    return (
        time.time() - began,
        len(outcome.results),
        len(outcome.cached),
        sum((r.tokens_in or 0) for r in outcome.results),
    )


def main() -> int:
    work = Path(tempfile.mkdtemp(prefix="subagents-cache-measure-"))
    root = work / "ws"
    (root / "docs").mkdir(parents=True)
    for name, text in SOURCES.items():
        (root / name).write_text(text, encoding="utf-8")

    config = replace(
        load_config(),
        allowed_roots=(root.resolve(),),
        db_path=work / "measure.db",
        worker_timeout_s=180,
    )

    try:
        print(f"workspace: {root}\n")
        cold = run_once(root.resolve(), config)
        print(f"cold : {cold[0]:6.1f}s  {cold[1]} worker(s), {cold[2]} cached, "
              f"{cold[3]:,} input tokens")

        warm = run_once(root.resolve(), config)
        print(f"warm : {warm[0]:6.1f}s  {warm[1]} worker(s), {warm[2]} cached, "
              f"{warm[3]:,} input tokens")

        if warm[1] or not warm[2]:
            print("\nFAILED: the warm run did not come entirely from cache.")
            return 1

        print(f"\n{cold[0] / max(warm[0], 0.001):.0f}x faster, "
              f"{cold[3]:,} input tokens not spent again.")
        print("The saving is the whole cold run, because the warm run does nothing.")
        print("That is only worth having in the loop it was built for -- a plan")
        print("re-proposed after a cancellation, a taint, or a declined approval.")
        return 0
    finally:
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
