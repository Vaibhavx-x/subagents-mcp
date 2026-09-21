"""Build the two graded fixtures, and the answer keys, from one source of truth.

    python bench/quality/inject.py            # build into bench/quality/fixture/
    python bench/quality/inject.py --check    # prove each key matches its fixture

Two fixtures, because the two tasks want opposite things from the code:

- `fixture/audit/`   -- four modules with known defects planted in them;
- `fixture/extract/` -- the same four modules, **clean**, with a question key.

Keeping them separate means an extraction question can never accidentally ask
about a line that was deliberately broken.

The keys are **generated from the edit that produced them**, never written by
hand. A hand-maintained key drifts away from the fixture the moment either
changes, and then grades against something that is not there -- which is the
kind of defect that makes every downstream number wrong while nothing raises.

`--check` is the gate `preflight_all.py` runs. It proves five things:

1. every recorded defect line really contains the injected text;
2. no two defects in one module sit within MIN_GAP lines, so the grader's +/-2
   tolerance cannot credit one finding to two defects;
3. the clean source does NOT already contain the replacement -- otherwise the
   "defect" was always there and finding it proves nothing;
4. each injected module still parses, because a syntax error turns an audit
   task into a different task;
5. every extraction answer is actually present on the line its evidence names.
"""

from __future__ import annotations

import argparse
import ast
import json
import shutil
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

FIXTURE = Path(__file__).resolve().parent / "fixture"
MODULES = ("worker", "execution", "hashing", "db")

# The +/-2 grading tolerance means two defects closer than this could both be
# credited to one reported line.
MIN_GAP = 5


# (module, needle, replacement, kind). Every needle is verified unique.
# Each edit stays on ONE line and preserves the line count, so the recorded
# line numbers are the fixture's own.
DEFECTS: list[tuple[str, str, str, str]] = [
    # ---------------------------------------------------------------- worker
    ("worker",
     "return code.value == 259  # STILL_ACTIVE",
     "return code.value != 259  # STILL_ACTIVE",
     "inverted liveness check: a running process reports as exited"),
    ("worker",
     'if normalised.upper() == "SUCCESS":',
     'if normalised.upper() != "SUCCESS":',
     "success and failure are swapped when classifying agy's status"),
    ("worker",
     'if status == "ok" and AGY_TIMEOUT_BANNER in stderr:',
     'if status == "ok" or AGY_TIMEOUT_BANNER in stderr:',
     "and became or: a healthy run is misread as a timeout"),
    ("worker",
     'elif status == "ok" and not response.strip():',
     'elif status == "ok" and response.strip():',
     "dropped not: an empty response passes and a real answer is rejected"),
    # ------------------------------------------------------------- execution
    ("execution",
     "if _now() > expires:",
     "if _now() < expires:",
     "expiry comparison reversed: only unexpired plans are refused"),
    ("execution",
     'return result.status in ("failed", "unparseable")',
     'return result.status in ("failed", "unparseable", "timeout")',
     "a timeout now escalates, spending another full budget on more time"),
    ("execution",
     "if not is_rate_limited(worker_result) or attempt == RATE_LIMIT_ATTEMPTS - 1:",
     "if not is_rate_limited(worker_result) and attempt == RATE_LIMIT_ATTEMPTS - 1:",
     "or became and: the rate-limit retry loop never breaks early"),
    ("execution",
     'attribution = "task" if len(runnable) == 1 else "wave"',
     'attribution = "task" if len(runnable) >= 1 else "wave"',
     "attribution claims per-task precision for a whole parallel wave"),
    # --------------------------------------------------------------- hashing
    ("hashing",
     "return any(fnmatch(name, pattern) or fnmatch(norm_path, pattern)",
     "return any(fnmatch(name, pattern) and fnmatch(norm_path, pattern)",
     "or became and: ignore globs stop matching almost everything"),
    ("hashing",
     "return bool(self.reads_modified or self.undeclared or self.missing)",
     "return bool(self.reads_modified or self.missing)",
     "undeclared changes no longer count as taint -- the detector's main case"),
    ("hashing",
     "except FileNotFoundError:",
     "except PermissionError:",
     "a missing file now raises instead of hashing to None"),
    ("hashing",
     "dirnames[:] = [d for d in dirnames if not spec.skips_dir(d)]",
     "dirnames[:] = [d for d in dirnames if spec.skips_dir(d)]",
     "dropped not: the walk descends only into the directories it should skip"),
    # -------------------------------------------------------------------- db
    ("db",
     'conn.execute("PRAGMA foreign_keys=ON;")',
     'conn.execute("PRAGMA foreign_keys=OFF;")',
     "foreign keys disabled, silently voiding every referential constraint"),
    ("db",
     "for attempt in range(WRITE_ATTEMPTS):",
     "for attempt in range(WRITE_ATTEMPTS - 1):",
     "off by one: one fewer lock attempt than the constant promises"),
    ("db",
     "delay = WRITE_BACKOFF_S * (2 ** attempt) + random.uniform(0, 0.02)",
     "delay = WRITE_BACKOFF_S * (2 * attempt) + random.uniform(0, 0.02)",
     "backoff is linear, not exponential, and starts at zero"),
    ("db",
     "if seconds > 0:",
     "if seconds >= 0:",
     "zero-length durations enter the estimator's sample"),
]

# Each answer must appear on the line its `needle` identifies, in the CLEAN
# module named by `module`. `--check` proves that, so the key cannot drift.
# `spans` marks a question whose evidence is in a module OTHER than the one a
# per-module worker would be given -- that is where delegation should hurt,
# and a question set without any would be built to flatter the tool.
QUESTIONS: list[dict] = [
    {"id": 1, "module": "db", "needle": "WRITE_ATTEMPTS = ", "answer": "6",
     "text": "In db.py, how many attempts does a write transaction make to take the lock?"},
    {"id": 2, "module": "db", "needle": 'PRAGMA busy_timeout=', "answer": "5000",
     "text": "In db.py, what value is PRAGMA busy_timeout set to?"},
    {"id": 3, "module": "db", "needle": "PRAGMA journal_mode=", "answer": "WAL",
     "text": "In db.py, what journal_mode does connect() set?"},
    {"id": 4, "module": "db", "needle": "conn.execute(\"BEGIN IMMEDIATE\")",
     "answer": "BEGIN IMMEDIATE",
     "text": "In db.py, which exact SQL statement starts a write transaction?"},
    {"id": 5, "module": "worker", "needle": "KILL_GRACE_S = ", "answer": "20",
     "text": "In worker.py, what is the value of KILL_GRACE_S?"},
    {"id": 6, "module": "worker", "needle": "AGY_TIMEOUT_BANNER = ",
     "answer": "print timeout after",
     "text": "In worker.py, what exact string is AGY_TIMEOUT_BANNER?"},
    {"id": 7, "module": "worker", "needle": "return code.value == 259",
     "answer": "259",
     "text": "In worker.py, which numeric exit code means a process is still running?"},
    {"id": 8, "module": "hashing", "needle": "_READ_CHUNK = ", "answer": "1 << 20",
     "text": "In hashing.py, what is _READ_CHUNK set to? Give the expression as written."},
    {"id": 9, "module": "hashing", "needle": "DEFAULT_IGNORE_GLOBS = ", "answer": "*.log",
     "text": "In hashing.py, what is the FIRST pattern in DEFAULT_IGNORE_GLOBS?"},
    {"id": 10, "module": "execution", "needle": "SCOPE_SUMMARY_MAX = ", "answer": "300",
     "text": "In execution.py, what is SCOPE_SUMMARY_MAX?"},
    # ---- cross-module: the evidence is NOT in the module that uses it ----
    {"id": 11, "module": "worker", "needle": "KILL_GRACE_S = ", "answer": "20",
     "spans": "execution",
     "text": ("execution.py kills a worker at config.worker_timeout_s + KILL_GRACE_S. "
              "What is the numeric value of KILL_GRACE_S?")},
    {"id": 12, "module": "db", "needle": "WRITE_BACKOFF_S = ", "answer": "0.05",
     "spans": "execution",
     "text": ("execution.py persists each worker's result through db.write_transaction, "
              "which backs off before retrying a locked database. What is the base "
              "backoff in seconds?")},
]


def clean_source(module: str) -> str:
    return (REPO / "subagents" / f"{module}.py").read_text(encoding="utf-8")


def build(dest: Path) -> dict:
    """Write both fixtures and return the combined key."""
    if dest.exists():
        shutil.rmtree(dest)
    audit, extract = dest / "audit" / "src", dest / "extract" / "src"
    audit.mkdir(parents=True)
    extract.mkdir(parents=True)
    (dest / "audit" / "findings").mkdir()
    (dest / "extract" / "answers").mkdir()

    planted: list[dict] = []
    for module in MODULES:
        source = clean_source(module)
        (extract / f"{module}.py").write_text(source, encoding="utf-8")

        for mod, needle, replacement, kind in DEFECTS:
            if mod != module:
                continue
            if source.count(needle) != 1:
                raise SystemExit(
                    f"{module}: needle is not unique ({source.count(needle)} hits): {needle!r}"
                )
            source = source.replace(needle, replacement, 1)

        (audit / f"{module}.py").write_text(source, encoding="utf-8")

        # Line numbers are read back OUT of the written file rather than
        # predicted, so the key is a fact about the fixture rather than a
        # belief about the edit.
        lines = source.splitlines()
        for mod, _needle, replacement, kind in DEFECTS:
            if mod != module:
                continue
            hits = [i + 1 for i, line in enumerate(lines) if replacement in line]
            if len(hits) != 1:
                raise SystemExit(f"{module}: replacement not uniquely located: {replacement!r}")
            planted.append({"module": module, "line": hits[0], "kind": kind})

    key = {
        "modules": list(MODULES),
        "min_gap": MIN_GAP,
        "defects": sorted(planted, key=lambda d: (d["module"], d["line"])),
        "questions": QUESTIONS,
    }
    (dest / "key.json").write_text(json.dumps(key, indent=2), encoding="utf-8")

    for name, body in (
        ("audit", "# fixture\n\nFour modules from a real project. Some lines are wrong.\n"),
        ("extract", "# fixture\n\nFour modules from a real project, unmodified.\n"),
    ):
        (dest / name / "README.md").write_text(body, encoding="utf-8")
    return key


def check(dest: Path) -> list[str]:
    """Everything that must hold for the key to be worth grading against."""
    problems: list[str] = []
    key = json.loads((dest / "key.json").read_text(encoding="utf-8"))

    by_module: dict[str, list[dict]] = {}
    for defect in key["defects"]:
        by_module.setdefault(defect["module"], []).append(defect)

    replacement_of = {(m, k): r for m, _n, r, k in DEFECTS}

    for module, defects in by_module.items():
        path = dest / "audit" / "src" / f"{module}.py"
        lines = path.read_text(encoding="utf-8").splitlines()
        original = clean_source(module)

        # 4. still valid Python
        try:
            ast.parse("\n".join(lines))
        except SyntaxError as exc:
            problems.append(f"{module}: injected fixture does not parse ({exc})")

        if len(lines) != len(original.splitlines()):
            problems.append(f"{module}: injection changed the line count")

        for defect in defects:
            # 1. the recorded line really holds the injected text
            wanted = replacement_of.get((module, defect["kind"]))
            if wanted is None:
                problems.append(f"{module}:{defect['line']}: key kind not in DEFECTS")
                continue
            actual = lines[defect["line"] - 1] if defect["line"] <= len(lines) else ""
            if wanted not in actual:
                problems.append(
                    f"{module}:{defect['line']}: key says {defect['kind']!r} but the "
                    f"line does not contain the injected text"
                )
            # 3. it was not already broken
            if wanted in original:
                problems.append(
                    f"{module}: the clean source already contains {wanted!r} -- "
                    f"nothing was actually injected"
                )

        # 2. spacing, so the +/-2 tolerance cannot double-credit
        seen = sorted(d["line"] for d in defects)
        for earlier, later in zip(seen, seen[1:]):
            if later - earlier < MIN_GAP:
                problems.append(
                    f"{module}: defects on lines {earlier} and {later} are "
                    f"{later - earlier} apart, under the {MIN_GAP}-line minimum"
                )

    # 5. every answer is where its evidence says it is
    for question in key["questions"]:
        source = (dest / "extract" / "src" / f"{question['module']}.py").read_text(
            encoding="utf-8")
        matching = [ln for ln in source.splitlines() if question["needle"] in ln]
        if not matching:
            problems.append(f"Q{question['id']}: needle {question['needle']!r} not found "
                            f"in {question['module']}.py")
            continue
        if not any(question["answer"] in ln for ln in matching):
            problems.append(f"Q{question['id']}: answer {question['answer']!r} is not on "
                            f"the line its evidence names")
    return problems


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true",
                        help="rebuild into a temp dir and verify, without touching the fixture")
    parser.add_argument("--dest", type=Path, default=FIXTURE)
    args = parser.parse_args()

    if args.check:
        # The COMMITTED fixture is checked, not a fresh build -- that is the
        # one the grader reads, and a fresh build passing says nothing about
        # whether what is on disk still matches its key.
        if not (args.dest / "key.json").is_file():
            print(f"no fixture at {args.dest} -- run without --check first")
            return 1
        problems = check(args.dest)

        # Determinism is checked separately, in a temp dir, because a key that
        # varies between builds could never be committed in the first place.
        with tempfile.TemporaryDirectory(prefix="quality-check-") as tmp:
            first = build(Path(tmp) / "a")
            second = build(Path(tmp) / "b")
            if json.dumps(first, sort_keys=True) != json.dumps(second, sort_keys=True):
                problems.append("inject.py is not deterministic: two builds differ")
            for module in MODULES:
                one = (Path(tmp) / "a" / "audit" / "src" / f"{module}.py").read_bytes()
                two = (Path(tmp) / "b" / "audit" / "src" / f"{module}.py").read_bytes()
                if one != two:
                    problems.append(f"{module}: two builds produced different bytes")

        if problems:
            print(f"{len(problems)} problem(s):")
            for problem in problems:
                print(f"  {problem}")
            return 1
        print(f"fixture key is sound: {len(DEFECTS)} defect(s) across {len(MODULES)} "
              f"module(s), {len(QUESTIONS)} question(s)")
        return 0

    key = build(args.dest)
    print(f"built {args.dest}")
    print(f"  audit   : {len(key['defects'])} defect(s) across {len(MODULES)} module(s)")
    print(f"  extract : {len(key['questions'])} question(s), "
          f"{sum(1 for q in key['questions'] if q.get('spans'))} of them cross-module")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
