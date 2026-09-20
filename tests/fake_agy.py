"""A stand-in for the `agy` CLI acting as a PARENT, for A/B harness dry runs.

tests/fake_worker.py fakes a worker. This fakes the parent: it accepts the same
flags, does the documentation task itself, and emits the same JSON shape.

It deliberately cannot delegate -- it speaks no MCP. So a dry run exercises
arms A and B end to end, and arm C correctly voids with `did_not_delegate`,
which is the guard working rather than a gap in the rehearsal.

    python tests/fake_agy.py --output-format json --print "<prompt>"
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

# Shaped like a real agy parent turn: input dominates output by two orders of
# magnitude, which is the finding the whole project rests on.
BASE_INPUT = 180_000
PER_FILE_INPUT = 12_000


def main() -> int:
    parser = argparse.ArgumentParser()
    for flag in ("--model", "--print-timeout", "--add-dir", "--output-format"):
        parser.add_argument(flag, dest=flag.lstrip("-").replace("-", "_"), default=None)
    parser.add_argument("--dangerously-skip-permissions", action="store_true")
    parser.add_argument("--print", dest="prompt", default="")
    parser.add_argument("--fake-mode", default=os.environ.get("FAKE_AGY_MODE", "ok"),
                        help="ok | stub | refuse")
    args = parser.parse_args()

    root = Path(args.add_dir or os.getcwd())
    names = sorted(p.stem for p in (root / "src").glob("*.py")) if (root / "src").is_dir() else []

    written = 0
    if args.fake_mode != "refuse":
        (root / "docs").mkdir(exist_ok=True)
        for name in names:
            source = (root / "src" / f"{name}.py").read_text(encoding="utf-8", errors="replace")
            defined = [m.group(2) for m in re.finditer(r"^(def|class)\s+(\w+)", source, re.M)]
            symbol = defined[0] if defined else name
            if args.fake_mode == "stub":
                body = f"# {name}\n"
            else:
                body = (
                    f"# {name}\n\n"
                    f"This module defines `{symbol}` and related helpers. "
                    + "It is documented here for the A/B fixture. " * 8
                )
            (root / "docs" / f"{name}.md").write_text(body, encoding="utf-8")
            written += 1

    doc = {
        "conversation_id": "fake-agy-0000",
        "status": "SUCCESS",
        "response": f"Wrote {written} document(s).",
        "duration_seconds": 1.0,
        "num_turns": 1,
        "usage": {
            "input_tokens": BASE_INPUT + PER_FILE_INPUT * len(names),
            "output_tokens": 2_400,
            "thinking_tokens": 0,
            "cache_read_tokens": 40_000,
            "total_tokens": BASE_INPUT + PER_FILE_INPUT * len(names) + 2_400,
        },
    }
    sys.stdout.write(json.dumps(doc))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
