"""A stand-in for `agy` that emits controllable output.

Lets the worker lifecycle -- partial capture, tree kill, malformed output,
status classification -- be tested exhaustively without spending API tokens.
Mimics the real worker's JSON shape, verified stable across 33 benchmark logs.

  python fake_worker.py --mode ok
  python fake_worker.py --mode hang --flush-first --spawn-child --marker X
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time

TEMPLATE = {
    "conversation_id": "11111111-2222-3333-4444-555555555555",
    "status": "SUCCESS",
    "response": "Renamed the helper and updated both call sites.",
    "duration_seconds": 1.25,
    "num_turns": 1,
    "usage": {
        "input_tokens": 120000,
        "output_tokens": 1500,
        "thinking_tokens": 0,
        "cache_read_tokens": 40000,
        "total_tokens": 121500,
    },
}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", default="ok")
    ap.add_argument("--delay", type=float, default=0.0)
    ap.add_argument("--flush-first", action="store_true",
                    help="emit real output, flush, THEN hang -- the partial-capture case")
    ap.add_argument("--spawn-child", action="store_true",
                    help="spawn a grandchild that outlives us unless killed as a tree")
    ap.add_argument("--marker", default="FAKE_WORKER_CHILD")
    # Swallow the real agy flags so the same command shape can be used.
    for flag in ("--model", "--print-timeout", "--add-dir", "--output-format", "--print"):
        ap.add_argument(flag, dest=flag.lstrip("-").replace("-", "_"), default=None)
    args = ap.parse_args()

    if args.spawn_child:
        subprocess.Popen(
            [sys.executable, "-c", f"import time; time.sleep(120)  # {args.marker}"]
        )

    if args.mode == "hang":
        if args.flush_first:
            sys.stdout.write("PARTIAL-BEFORE-HANG")
            sys.stdout.flush()
            sys.stderr.write("PARTIAL-STDERR")
            sys.stderr.flush()
        time.sleep(300)
        return 0

    if args.delay:
        time.sleep(args.delay)

    doc = dict(TEMPLATE)
    if args.mode == "ok":
        pass
    elif args.mode == "error_status":
        doc["status"] = "ERROR"
    elif args.mode == "rate_limited":
        doc["status"] = "RESOURCE_EXHAUSTED"
    elif args.mode == "conv_id_429":
        # Bench defect D2: a 429 inside the conversation_id must NOT be read as
        # a rate limit when the status says SUCCESS.
        doc["conversation_id"] = "429abcde-1111-2222-3333-444455556666"
        doc["status"] = "SUCCESS"
    elif args.mode == "no_usage":
        doc.pop("usage")
    elif args.mode == "truncated":
        sys.stdout.write(json.dumps(doc)[: len(json.dumps(doc)) // 2])
        return 0
    elif args.mode == "garbage":
        sys.stdout.write("this is not json at all\nsecond line\n")
        return 1
    elif args.mode == "empty":
        return 1
    elif args.mode == "stderr_noise":
        sys.stderr.write("a warning on stderr\n")
    else:
        raise SystemExit(f"unknown mode {args.mode}")

    sys.stdout.write(json.dumps(doc))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
