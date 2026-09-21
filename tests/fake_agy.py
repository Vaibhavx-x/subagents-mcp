"""A stand-in for the `agy` CLI acting as a PARENT, for A/B harness dry runs.

tests/fake_worker.py fakes a worker. This fakes the parent: it accepts the same
flags, does the documentation task itself, and emits the same JSON shape.

It deliberately cannot delegate -- it speaks no MCP. So a dry run exercises
arms A and B end to end, and arm C correctly voids with `did_not_delegate`,
which is the guard working rather than a gap in the rehearsal.

    python tests/fake_agy.py --output-format json --print "<prompt>"

It recognises three deliverables from the prompt -- documentation, a defect
audit, or an answer sheet -- and writes the shape each one asks for.

**It never reads the answer key.** The audit output names a fixed set of line
numbers and the answer sheet says "unknown", so a rehearsal scores near zero
on purpose. A fake that consulted the key would agree with the grader by
construction and prove nothing about it; what a dry run is for is the plumbing
-- does a run complete, does the CSV row fill in, does the grader parse what
was actually written.
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


# Line numbers reported by the fake auditor. Fixed, and chosen WITHOUT looking
# at any key: the point of a rehearsal is the plumbing, not the score.
FAKE_FINDINGS = (10, 50, 100)


def detect_task(prompt: str) -> str:
    """Which deliverable the prompt asks for.

    Matched on the required output format rather than on topic words, because
    the format is the thing that is unambiguous -- "audit" appears in prose
    about several different tasks.
    """
    if "LINE <number>:" in prompt:
        return "audit"
    if "Q<number>:" in prompt:
        return "extract"
    return "docs"


def deliver(task: str, root: Path, names: list[str], prompt: str, mode: str) -> int:
    if task == "audit":
        (root / "findings").mkdir(exist_ok=True)
        for name in names:
            body = "" if mode == "stub" else "\n".join(
                f"LINE {n}: this looks wrong" for n in FAKE_FINDINGS)
            (root / "findings" / f"{name}.md").write_text(body + "\n", encoding="utf-8")
        return len(names)

    if task == "extract":
        (root / "answers").mkdir(exist_ok=True)
        # Leading whitespace allowed: the delegating prompt indents its
        # per-module question assignments, and a rehearsal that silently
        # produced an empty sheet there would be testing nothing.
        ids = sorted({int(m) for m in re.findall(r"^\s*Q(\d+):", prompt, re.M)})
        body = "" if mode == "stub" else "\n".join(f"Q{i}: unknown" for i in ids)
        (root / "answers" / "answers.md").write_text(body + "\n", encoding="utf-8")
        return 1

    (root / "docs").mkdir(exist_ok=True)
    for name in names:
        source = (root / "src" / f"{name}.py").read_text(encoding="utf-8", errors="replace")
        defined = [m.group(2) for m in re.finditer(r"^(def|class)\s+(\w+)", source, re.M)]
        symbol = defined[0] if defined else name
        if mode == "stub":
            body = f"# {name}\n"
        else:
            body = (
                f"# {name}\n\n"
                f"This module defines `{symbol}` and related helpers. "
                + "It is documented here for the A/B fixture. " * 8
            )
        (root / "docs" / f"{name}.md").write_text(body, encoding="utf-8")
    return len(names)


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
        written = deliver(detect_task(args.prompt), root, names,
                          args.prompt, args.fake_mode)

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
