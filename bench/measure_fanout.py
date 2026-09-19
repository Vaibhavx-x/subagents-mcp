"""Measure the same 3-task plan twice: max_parallel=4, then max_parallel=1.

Six real workers on a throwaway workspace with three tiny files, so the spend
is small and the difference is entirely scheduling.
"""

import asyncio
import shutil
import sys
import tempfile
import time
from dataclasses import replace
from pathlib import Path

ROOT = Path(r"D:\Projects\subagents-mcp")
sys.path.insert(0, str(ROOT))

from subagents.config import load_config  # noqa: E402
from subagents.execution import execute  # noqa: E402
from subagents.planning import propose  # noqa: E402

WORDS = ("alpha", "beta", "gamma")


def make_workspace(base: Path) -> Path:
    ws = base / "ws"
    ws.mkdir()
    (ws / "README.md").write_text("# tiny workspace\n\nNothing to see.\n", encoding="utf-8")
    return ws.resolve()


def tasks(ws: Path) -> str:
    import json

    return json.dumps([
        {
            "task_ref": f"note-{n}",
            "instruction": (
                f"Create a file named note_{n}.txt in the workspace root "
                f"containing exactly the single word: {word}. Do nothing else."
            ),
            "reads": ["README.md"],
            "writes": [f"note_{n}.txt"],
        }
        for n, word in enumerate(WORDS)
    ])


def run_once(label: str, max_parallel: int) -> tuple[float, int, int]:
    base = Path(tempfile.mkdtemp(prefix="fanout-"))
    try:
        ws = make_workspace(base)
        cfg = replace(
            load_config(),
            allowed_roots=(ws,),
            db_path=base / "plans.db",
            max_parallel=max_parallel,
            worker_timeout_s=120,
        )
        plan = propose(tasks(ws), str(ws), cfg)
        assert len(plan.waves) == 1, "tasks should be independent"

        started = time.time()
        outcome = asyncio.run(execute(
            "writes note_0.txt, note_1.txt, note_2.txt; nothing else",
            plan.plan_id, plan.plan_digest, cfg,
        ))
        elapsed = time.time() - started

        ok = sum(1 for r in outcome.results if r.ok)
        tin = sum(r.tokens_in or 0 for r in outcome.results)
        written = sum(1 for n in range(3) if (ws / f"note_{n}.txt").is_file())
        tainted = outcome.tainted_refs

        print(f"{label:<22} {elapsed:6.1f}s   {ok}/3 ok   {written}/3 files   "
              f"in={tin:,}   tainted={tainted or 'none'}")
        return elapsed, ok, tin
    finally:
        shutil.rmtree(base, ignore_errors=True)


if __name__ == "__main__":
    print(f"{'config':<22} {'wall':>7}")
    par, _, tin_par = run_once("max_parallel=4", 4)
    seq, _, tin_seq = run_once("max_parallel=1", 1)
    print()
    print(f"speedup: {seq / par:.2f}x  ({seq - par:.1f}s saved on 3 trivial workers)")
    print(f"tokens:  {tin_par:,} parallel vs {tin_seq:,} sequential "
          f"-- scheduling should not change this materially")
