"""Does every part of the tool actually work? One command, one matrix.

    python bench/quality/preflight_all.py           # free checks only
    python bench/quality/preflight_all.py --real    # + the opt-in suites (~15 workers)

This is a **gate**, not a report. Nothing in `bench/quality/` may be run
against real tokens until this exits 0, because every check below is something
that would otherwise fail silently in the middle of a measurement and be
discovered afterwards, when the tokens are already spent.

It runs what already exists rather than reimplementing any of it. If a check
here disagrees with its own suite, the suite is right and this is wrong.

Three things no automation can reach -- the approval prompt, a real
cancellation, and the client restart after a registration change. They are
printed as a checklist at the end and explicitly NOT counted as passing.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

PY = sys.executable


@dataclass
class Check:
    name: str
    what: str
    cmd: list[str]
    costs_tokens: bool = False
    env: dict[str, str] = field(default_factory=dict)
    # A check may legitimately exit non-zero for a reason that is not a
    # failure of the tool -- --check-config returns 1 on a machine where the
    # server is not registered, which is true of any clean clone.
    tolerate: tuple[int, ...] = ()


FREE: list[Check] = [
    Check("tests", "every offline invariant",
          [PY, "-m", "pytest", "-q"]),
    Check("smoke", "stdout is not polluted; the protocol survives",
          [PY, "tests/smoke_stdio.py"]),
    Check("clone", "a stranger's clean clone installs and passes",
          [PY, "tools/install_check.py"]),
    Check("config", "client deadline reads back, and the agy binary resolves",
          [PY, "server.py", "--check-config"]),
    Check("prune", "the cache prunes, and migrates a database without the table",
          [PY, "server.py", "--prune-cache"]),
    Check("replay", "the historical hit rate still reproduces",
          [PY, "bench/cache/replay.py"]),
    Check("fixture", "the quality fixture matches its own answer key",
          [PY, "bench/quality/inject.py", "--check"]),
    Check("dry", "the measurement harness runs end to end against a fake",
          [PY, "bench/quality/run_quality.py", "--repeats", "1", "--dry",
           "--out", "-"]),
]

REAL: list[Check] = [
    Check("worker", "a real worker does the work, reports usage, dies on time",
          [PY, "-m", "pytest", "tests/test_real_worker.py", "-q"],
          costs_tokens=True, env={"SUBAGENTS_REAL_AGY": "1"}),
    Check("cache", "a real hit, a content miss, and a deleted-output miss",
          [PY, "-m", "pytest", "tests/test_real_cache.py", "-q"],
          costs_tokens=True, env={"SUBAGENTS_REAL_AGY": "1"}),
    Check("mcp", "a headless parent can call our MCP tools at all",
          [PY, "bench/ab/preflight_allowrule.py"], costs_tokens=True),
    Check("taint", "an undeclared write is detected, then clean once declared",
          [PY, "bench/ab/demo_taint.py"], costs_tokens=True),
    Check("reuse", "cold against warm, with real workers",
          [PY, "bench/cache/measure.py"], costs_tokens=True),
]

BY_HAND = [
    ("approval prompt", "execute_plan's prompt opens with `affects`, not a hash "
                        "(NOTES.md section 20)"),
    ("decline is clean", "declining leaves zero executions rows and spawns nothing"),
    ("deadline recovery", "a plan cancelled at the client deadline, then recovered "
                          "with collect(plan_id)"),
    ("client restart", "the client was restarted after the last tool-registration "
                       "change (NOTES.md section 21)"),
]


def run_check(check: Check, timeout_s: int) -> tuple[bool, float, str]:
    env = {**os.environ, **check.env}
    began = time.time()
    try:
        proc = subprocess.run(check.cmd, capture_output=True, text=True,
                              cwd=str(REPO), env=env, timeout=timeout_s)
    except subprocess.TimeoutExpired:
        return False, time.time() - began, f"timed out after {timeout_s}s"
    took = time.time() - began
    ok = proc.returncode == 0 or proc.returncode in check.tolerate
    if ok:
        return True, took, ""
    tail = (proc.stdout or "")[-300:].strip() or (proc.stderr or "")[-300:].strip()
    return False, took, f"exit {proc.returncode}: {' '.join(tail.split())[:220]}"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--real", action="store_true",
                        help="also run the opt-in suites against live agy (~15 workers)")
    parser.add_argument("--timeout", type=int, default=900,
                        help="per-check timeout in seconds")
    args = parser.parse_args()

    checks = FREE + (REAL if args.real else [])
    print(f"{len(checks)} check(s)"
          + (", ~15 workers of real spend" if args.real else ", nothing spent"))
    print("=" * 74)

    failures: list[tuple[str, str]] = []
    for check in checks:
        marker = "$" if check.costs_tokens else " "
        print(f"{marker} {check.name:<9} {check.what}", flush=True)
        ok, took, detail = run_check(check, args.timeout)
        if ok:
            print(f"    PASS  {took:.1f}s")
        else:
            print(f"    FAIL  {took:.1f}s  {detail}")
            failures.append((check.name, detail))

    print("=" * 74)
    if failures:
        print(f"{len(failures)} check(s) FAILED -- do not spend tokens on a measurement:")
        for name, detail in failures:
            print(f"  {name}: {detail}")
        return 1

    print(f"all {len(checks)} automated check(s) passed"
          + ("" if args.real else "  (run with --real to include the live ones)"))
    print("\nNOT CHECKED HERE -- no automation reaches these. Walk them by hand:")
    for name, detail in BY_HAND:
        print(f"  [ ] {name}: {detail}")
    print("\nThis script cannot tell whether you did. It is a checklist, not a pass.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
