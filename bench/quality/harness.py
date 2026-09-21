"""Two gradeable tasks, run with the tool and without it.

`bench/ab/` answered "what does delegation cost". This answers "is the work any
good", which that bench explicitly could not: its `verify()` is a binary gate
used to void runs, and 23 of 23 graded runs passed it.

Reused from `bench/ab/harness.py` rather than copied: `ServerToggle` (backs the
client config up, refuses to start without a backup, restores in a finally),
`kill_server_processes`, and `WORK_ROOT`. Two harnesses with two copies of the
config-restore logic is one copy too many for something that can brick a
machine's MCP setup.

Arms A and C only. B -- registered but unused -- came out negative and inside
the noise at n=5 (`NOTES.md` section 32); re-buying a number already known to
be unmeasurable is not worth ten more runs.

Void versus zero
----------------

**Void** means *this run does not measure what we think it does*: wrong arm
state, a cache hit, agy not reporting SUCCESS, arm C that did not delegate.

**Zero** means *the run measured badly*: no output, or output in the wrong
format. It stays in the median.

That line matters. If arm A follows the output format worse than arm C --
plausible, since each arm-C worker gets one simple file -- then voiding those
runs would erase a real finding about the tool. `parse_ok` is reported as its
own per-arm rate instead.

A timeout is **recorded, not voided**, scored on whatever it produced, with the
per-arm count printed beside the medians. Arm A doing four modules serially is
genuinely likelier to hit a wall than arm C doing them at once, and that is a
result rather than an inconvenience.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))
# Imported by package path, never by bare name. `bench/ab/` and
# `bench/quality/` both define `harness` and `aggregate`, so a bare
# `import aggregate` resolves to whichever directory landed on sys.path first
# -- which depends on test collection order and fails somewhere else entirely.

from bench.ab.harness import (  # noqa: E402
    MCP_CONFIG,
    SERVER_NAME,
    ServerToggle,
    kill_server_processes,
    workers_for,
)
from bench.quality.grade import grade, load_key  # noqa: E402
from bench.quality.inject import MODULES  # noqa: E402
from subagents.config import Config  # noqa: E402

FIXTURE = REPO / "bench" / "quality" / "fixture"
WORK_ROOT = REPO / "bench" / "quality" / "work"
TASKS = ("audit", "extract")

CSV_COLUMNS = [
    "run_id", "task", "arm", "repeat", "wall_s",
    "parent_in", "parent_out", "parent_thinking", "parent_cache_read", "num_turns",
    "worker_in", "worker_out", "worker_count",
    "score_hits", "score_total", "score_false", "accuracy", "precision",
    "parse_ok", "hit_timeout",
    "server_enabled_observed", "delegated", "cached_hits", "agy_status",
    "void_reason",
]


@dataclass
class RunRecord:
    run_id: str
    task: str
    arm: str
    repeat: int
    wall_s: float = 0.0
    parent_in: int | None = None
    parent_out: int | None = None
    parent_thinking: int | None = None
    parent_cache_read: int | None = None
    # Recorded because `parent_in` is CUMULATIVE across turns, not peak context
    # occupancy: ten turns of 20k and two of 100k both total 200k and only one
    # fills a window. input-per-turn is derived from this at aggregation.
    num_turns: int | None = None
    worker_in: int = 0
    worker_out: int = 0
    worker_count: int = 0
    score_hits: int = 0
    score_total: int = 0
    score_false: int = 0
    accuracy: float = 0.0
    precision: float = 0.0
    parse_ok: bool = False
    hit_timeout: bool = False
    server_enabled_observed: bool | None = None
    delegated: bool = False
    cached_hits: int = 0
    agy_status: str = ""
    void_reason: str = ""
    detail: str = field(default="", repr=False)
    stderr_tail: str = field(default="", repr=False)

    @property
    def void(self) -> bool:
        return bool(self.void_reason)

    def as_row(self) -> dict:
        data = asdict(self)
        return {col: data[col] for col in CSV_COLUMNS}


def materialise(task: str, run_id: str) -> Path:
    """A fresh copy of the fixture per run.

    Fresh per run for two reasons: a previous run's findings must not be
    gradeable by the next one, and a unique absolute path makes a cache hit
    structurally impossible -- which `classify_void` then checks anyway.
    """
    root = WORK_ROOT / f"{task}-{run_id}"
    if root.exists():
        shutil.rmtree(root, ignore_errors=True)
    root.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(FIXTURE / task, root)
    return root.resolve()


# --------------------------------------------------------------------- prompts
# The prompts deliberately do NOT say what kind of defect to look for, how many
# there are, or that every module has one. Calibration scored 100% when they
# did -- naming the categories turns an audit into a search-and-replace
# (NOTES.md section 44).
AUDIT_SOLO = """Some of the Python modules under src/ contain defects: lines whose behaviour
contradicts what the surrounding code and comments clearly intend. Some modules
may be entirely correct.

Read each file and write findings/<name>.md listing the defects you find.

Use EXACTLY this format, one defect per line, and nothing else in the file:

LINE <number>: <short description>

where <number> is the line number in that module. Report only lines you believe
are actually defective -- a wrong line counts against you. Do not modify
anything under src/.

Modules: {names}"""

AUDIT_DELEGATE = """Use the subagents MCP server. Do not do this work yourself, and do not read
the source files yourself.

Call propose_plan with workspace_root "{root}" and one task per module below.
Each task_ref is "audit-<name>", each reads is ["src/<name>.py"], each writes is
["findings/<name>.md"], and each instruction is:

"src/<name>.py may contain defects: lines whose behaviour contradicts what the
surrounding code and comments clearly intend. It may also be entirely correct.
Read it and write findings/<name>.md listing the defects you find. If you find
none, write an empty file. Use EXACTLY this format, one
defect per line, and nothing else in the file: LINE <number>: <short
description>, where <number> is the line number in src/<name>.py. Report only
lines you believe are actually defective -- a wrong line counts against you.
Do not modify anything under src/."

Modules: {names}

Then call execute_plan with the plan_id and plan_digest it returns, and a short
affects argument naming what is written."""

EXTRACT_SOLO = """Answer these questions about the Python modules under src/. No question tells
you which file its answer is in; finding it is part of the task.

Write answers/answers.md using EXACTLY this format, one answer per line, and
nothing else in the file:

Q<number>: <answer>

Give the shortest exact answer -- a value, a name or a literal string, not a
sentence. Do not modify anything under src/.

{questions}"""

EXTRACT_DELEGATE = """Use the subagents MCP server. Do not do this work yourself, and do not read
the source files yourself.

Call propose_plan with workspace_root "{root}" and one task per module below.
Each task_ref is "ask-<name>", each reads is ["src/<name>.py"], each writes is
["answers/<name>.md"], and each instruction asks that module's worker to answer
the questions assigned to it below, writing answers/<name>.md in EXACTLY this
format, one answer per line and nothing else in the file: Q<number>: <answer>,
giving the shortest exact answer -- a value, a name or a literal string, not a
sentence. Do not modify anything under src/.

{assignments}

Then call execute_plan with the plan_id and plan_digest it returns, and a short
affects argument naming what is written. Finally, concatenate every
answers/<name>.md into answers/answers.md, keeping one `Q<number>: <answer>`
line per answer and nothing else."""


def question_block(key: dict) -> str:
    return "\n".join(f"Q{q['id']}: {q['text']}" for q in key["questions"])


def assignment_block(key: dict) -> str:
    """Questions grouped by the module a delegating parent would send them to.

    Assigned by the module the QUESTION NAMES, not by where the answer lives.
    That is the honest split: a parent delegating by module has no way to know
    that Q11's answer is in worker.py when the question is about execution.py.
    Those two questions are exactly what the cross-module design is testing,
    and pre-routing them would delete the test.
    """
    grouped: dict[str, list[dict]] = {name: [] for name in MODULES}
    for question in key["questions"]:
        owner = question.get("spans") or question["module"]
        grouped.setdefault(owner, []).append(question)
    blocks = []
    for module, questions in grouped.items():
        if not questions:
            continue
        lines = "\n".join(f"  Q{q['id']}: {q['text']}" for q in questions)
        blocks.append(f"{module} -- answers/{module}.md:\n{lines}")
    return "\n\n".join(blocks)


def prompt_for(task: str, arm: str, root: Path, key: dict) -> str:
    names = ", ".join(MODULES)
    if task == "audit":
        return (AUDIT_DELEGATE.format(root=str(root), names=names) if arm == "C"
                else AUDIT_SOLO.format(names=names))
    if arm == "C":
        return EXTRACT_DELEGATE.format(root=str(root), assignments=assignment_block(key))
    return EXTRACT_SOLO.format(questions=question_block(key))


# ------------------------------------------------------------------------ one run
def run_one(task: str, arm: str, repeat: int, config: Config, toggle: ServerToggle,
            print_timeout_s: int = 900, agy_cmd: list[str] | None = None) -> RunRecord:
    run_id = f"{arm}-{repeat}-{int(time.time())}"
    record = RunRecord(run_id=run_id, task=task, arm=arm, repeat=repeat)

    record.server_enabled_observed = toggle.set_enabled(arm == "C")
    kill_server_processes()

    key = load_key(FIXTURE)
    root = materialise(task, run_id)
    since = datetime.now(timezone.utc).isoformat()

    cmd = [
        *(agy_cmd or [config.agy_path]),
        "--model", config.model,
        "--print-timeout", f"{print_timeout_s}s",
        "--add-dir", str(root),
        "--output-format", "json",
        # Identical for both arms. File tools are auto-approved headlessly
        # regardless, so this changes nothing for A; without it arm C cannot
        # call an MCP tool at all (NOTES section 31).
        "--dangerously-skip-permissions",
        "--print", prompt_for(task, arm, root, key),
    ]

    began = time.time()
    proc = subprocess.run(cmd, capture_output=True, text=True, cwd=str(root))
    record.wall_s = round(time.time() - began, 1)
    record.stderr_tail = (proc.stderr or "")[-400:]
    record.hit_timeout = "print timeout after" in (proc.stderr or "")

    try:
        doc = json.loads(proc.stdout or "{}")
    except json.JSONDecodeError:
        doc = {}

    usage = doc.get("usage") or {}
    record.agy_status = doc.get("status", "")
    record.num_turns = doc.get("num_turns")
    record.parent_in = usage.get("input_tokens")
    record.parent_out = usage.get("output_tokens")
    record.parent_thinking = usage.get("thinking_tokens")
    record.parent_cache_read = usage.get("cache_read_tokens")

    count, win, wout, delegated, cached = workers_for(config.db_path, since)
    record.worker_count, record.worker_in, record.worker_out = count, win, wout
    record.delegated = delegated
    record.cached_hits = cached

    score = grade(task, root, key)
    record.score_hits, record.score_total = score.hits, score.total
    record.score_false = score.false_positives
    record.accuracy = round(score.accuracy, 4)
    record.precision = round(score.precision, 4)
    record.parse_ok = score.parse_ok
    record.detail = score.detail

    record.void_reason = classify_void(record)
    return record


def classify_void(record: RunRecord) -> str:
    """Why this run cannot enter a median. Empty string means it counts.

    Read off structured evidence -- the runs table, the client config, agy's
    JSON status -- never off the transcript, which is bench defect D2.

    Deliberately NOT here: a low score, an unparseable answer sheet, or a
    timeout. Those are results.
    """
    if record.arm == "A" and record.server_enabled_observed:
        return "server_was_enabled_for_solo_arm"
    if record.arm == "C" and not record.delegated:
        # The parent did the work itself. Interesting (NOTES section 22), and
        # not arm C.
        return "did_not_delegate"
    if record.arm == "C" and record.worker_count != len(MODULES):
        return f"expected {len(MODULES)} workers, saw {record.worker_count}"
    if record.cached_hits:
        return f"cache served {record.cached_hits} task(s); not a cold measurement"
    if record.agy_status and record.agy_status.upper() not in ("SUCCESS", "OK"):
        # Infrastructure, not quality: the parent never got to attempt the task.
        return f"agy status {record.agy_status}"
    return ""
