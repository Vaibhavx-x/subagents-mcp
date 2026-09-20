"""Run the three-arm A/B measurement.

    python bench/ab/run_ab.py --repeats 3          # spends tokens
    python bench/ab/run_ab.py --repeats 1 --dry    # fake agy, spends nothing

Writes one CSV row per run AS IT FINISHES, never at the end -- the same rule
the server itself follows, and for the same reason: a measurement that only
records on a clean exit loses everything to one crash twenty minutes in.

Arms are interleaved (A B C, A B C, ...) rather than run in blocks, so a
machine that gets slower or a quota that degrades partway through hits all
three arms equally instead of ruining one.
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
sys.path.insert(0, str(Path(__file__).parent))

from harness import CSV_COLUMNS, MCP_CONFIG, ServerToggle, run_one  # noqa: E402

from subagents.config import load_config  # noqa: E402

OUT = REPO / "bench" / "ab" / "runs.csv"


def append_row(path: Path, record) -> None:
    exists = path.is_file()
    with open(path, "a", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_COLUMNS)
        if not exists:
            writer.writeheader()
        writer.writerow(record.as_row())


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--arms", default="A,B,C")
    parser.add_argument("--timeout", type=int, default=600,
                        help="per-run --print-timeout in seconds")
    parser.add_argument("--out", type=Path, default=OUT)
    parser.add_argument("--dry", action="store_true",
                        help="use tests/fake_agy.py instead of the real CLI")
    args = parser.parse_args()

    config = load_config()
    agy_cmd = None
    if args.dry:
        fake = REPO / "tests" / "fake_agy.py"
        if not fake.is_file():
            print(f"no fake at {fake}")
            return 1
        agy_cmd = [sys.executable, str(fake)]

    arms = [a.strip().upper() for a in args.arms.split(",") if a.strip()]
    planned = len(arms) * args.repeats
    print(f"{planned} run(s): arms {arms} x {args.repeats}")
    print(f"config   : {MCP_CONFIG}")
    print(f"agy      : {agy_cmd or [config.agy_path]}")
    print(f"output   : {args.out}\n")

    began = time.time()
    voids = 0
    with ServerToggle(MCP_CONFIG) as toggle:
        # Interleaved, not blocked: a machine that slows down partway through
        # should degrade every arm equally rather than ruining one of them.
        for repeat in range(1, args.repeats + 1):
            for arm in arms:
                print(f"[{arm} {repeat}] running ...", flush=True)
                record = run_one(arm, repeat, config, toggle,
                                 print_timeout_s=args.timeout, agy_cmd=agy_cmd)
                append_row(args.out, record)
                voids += bool(record.void_reason)
                flag = f"VOID ({record.void_reason})" if record.void_reason else "counted"
                print(f"[{arm} {repeat}] {flag} "
                      f"parent_in={record.parent_in} workers={record.worker_count} "
                      f"files={record.files_written} {record.wall_s}s")
                if record.stderr_tail.strip():
                    print(f"           stderr: {record.stderr_tail.strip()[:160]}")

    print(f"\n{planned} run(s) in {time.time() - began:.0f}s, {voids} void")
    print(f"rows appended to {args.out}")
    print("next: python bench/ab/report.py")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
