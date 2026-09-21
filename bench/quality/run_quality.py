"""Run the accuracy measurement.

    python bench/quality/run_quality.py --repeats 5            # spends tokens
    python bench/quality/run_quality.py --repeats 1 --dry      # fake agy, free
    python bench/quality/run_quality.py --tasks audit --repeats 1

One CSV row per run, written AS IT FINISHES. The same rule the server follows
and for the same reason: a measurement that only records on a clean exit loses
everything to one crash forty minutes in.

Task and arm are interleaved (audit-A, audit-C, extract-A, extract-C, then
repeat) rather than run in blocks, so a machine that gets slower or a quota
that degrades partway through hits everything equally instead of ruining one
cell of the matrix.
"""

from __future__ import annotations

import argparse
import csv
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))
# Imported by package path, never by bare name. `bench/ab/` and
# `bench/quality/` both define `harness` and `aggregate`, so a bare
# `import aggregate` resolves to whichever directory landed on sys.path first
# -- which depends on test collection order and fails somewhere else entirely.

from bench.quality.harness import (  # noqa: E402
    CSV_COLUMNS,
    FIXTURE,
    MCP_CONFIG,
    TASKS,
    ServerToggle,
    run_one,
)
from subagents.config import load_config  # noqa: E402

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

OUT = REPO / "bench" / "quality" / "runs.csv"


def append_row(path: Path, record) -> None:
    """`-` means discard, for the dry run in preflight_all.py: rehearsing the
    harness must not append rehearsal rows to the real measurement."""
    if str(path) == "-":
        return
    exists = path.is_file()
    with open(path, "a", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_COLUMNS)
        if not exists:
            writer.writeheader()
        writer.writerow(record.as_row())


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repeats", type=int, default=5)
    parser.add_argument("--arms", default="A,C")
    parser.add_argument("--tasks", default=",".join(TASKS))
    parser.add_argument("--timeout", type=int, default=900,
                        help="per-run --print-timeout in seconds")
    parser.add_argument("--out", type=Path, default=OUT)
    parser.add_argument("--dry", action="store_true",
                        help="use tests/fake_agy.py instead of the real CLI")
    args = parser.parse_args()

    if not (FIXTURE / "key.json").is_file():
        print(f"no fixture at {FIXTURE} -- run: python bench/quality/inject.py")
        return 1

    config = load_config()
    agy_cmd = None
    if args.dry:
        fake = REPO / "tests" / "fake_agy.py"
        if not fake.is_file():
            print(f"no fake at {fake}")
            return 1
        agy_cmd = [sys.executable, str(fake)]

    arms = [a.strip().upper() for a in args.arms.split(",") if a.strip()]
    tasks = [t.strip() for t in args.tasks.split(",") if t.strip()]
    planned = len(arms) * len(tasks) * args.repeats
    print(f"{planned} run(s): tasks {tasks} x arms {arms} x {args.repeats}")
    print(f"config : {MCP_CONFIG}")
    print(f"agy    : {agy_cmd or [config.agy_path]}")
    print(f"output : {args.out}\n")

    began = time.time()
    voids = 0
    with ServerToggle(MCP_CONFIG) as toggle:
        for repeat in range(1, args.repeats + 1):
            for task in tasks:
                for arm in arms:
                    print(f"[{task} {arm} {repeat}] running ...", flush=True)
                    record = run_one(task, arm, repeat, config, toggle,
                                     print_timeout_s=args.timeout, agy_cmd=agy_cmd)
                    append_row(args.out, record)
                    voids += bool(record.void_reason)
                    flag = (f"VOID ({record.void_reason})" if record.void_reason
                            else f"score {record.score_hits}/{record.score_total}")
                    print(f"[{task} {arm} {repeat}] {flag} "
                          f"acc={record.accuracy} parse_ok={record.parse_ok} "
                          f"parent_in={record.parent_in} turns={record.num_turns} "
                          f"workers={record.worker_count} {record.wall_s}s")
                    if record.detail:
                        print(f"             {record.detail[:150]}")
                    if record.hit_timeout:
                        print("             hit its own print-timeout -- recorded, not void")

    print(f"\n{planned} run(s) in {time.time() - began:.0f}s, {voids} void")
    if str(args.out) != "-":
        print(f"rows appended to {args.out}")
        print("next: python bench/quality/report.py")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
