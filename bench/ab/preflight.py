"""Can a parent in `--print` mode call our MCP tools at all?

This gates the whole A/B measurement. Measured already (NOTES.md section 19):
headless agy auto-APPROVES file tools and auto-DENIES the `command` tool. MCP
tools were never tested. If they are denied, arm C cannot run headlessly and
the experiment has to be redesigned before any harness code is worth writing.

Two bounded runs, one prompt, roughly a minute of tokens:

    python bench/ab/preflight.py

Evidence is taken from the database -- a plan row either exists or it does not
-- rather than from the parent's prose, which is the mistake that produced
bench defect D2.
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(Path(__file__).parent))

from preflight_lib import PRINT_TIMEOUT_S, WORK, fresh_workspace, run_probe, verdict  # noqa: E402

from subagents.config import load_config  # noqa: E402


def main() -> int:
    db = load_config().db_path
    print(f"agy       : {load_config().agy_path}")
    print(f"database  : {db}")
    print(f"timeout   : {PRINT_TIMEOUT_S}s per probe\n")

    results = []
    for label, skip in (("no flag", False), ("--dangerously-skip-permissions", True)):
        root = fresh_workspace()
        print(f"--- probe: {label} ...", flush=True)
        result = run_probe(label, root, skip, db)
        result["verdict"] = verdict(result)
        results.append(result)
        print(f"    {result['verdict']}")
        print(f"    agy_status={result['agy_status']!r} "
              f"denied={[d.get('display_name') or d.get('action') for d in result['denied_actions']]} "
              f"plans={result['plans_created']} executed={result['plans_executed']} "
              f"file={result['file_written']} in_tok={result['input_tokens']} "
              f"{result['elapsed_s']}s")
        if result["stderr_tail"].strip():
            print(f"    stderr: {result['stderr_tail'].strip()[:200]}")
        print()

        # If the unflagged probe already works, the second one answers nothing
        # worth another minute of tokens.
        if not skip and result["verdict"].startswith("OK"):
            print("unflagged probe succeeded; skipping the flagged one to save tokens\n")
            break

    out = REPO / "bench" / "ab" / "preflight_result.json"
    out.write_text(json.dumps(results, indent=2), encoding="utf-8")
    print(f"written: {out}")
    shutil.rmtree(WORK, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
