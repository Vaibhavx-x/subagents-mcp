"""Three-arm A/B harness: what does delegation cost, and what does it save?

The README's central claim -- that context is the dominant token cost and
delegating keeps the parent's window clean -- has never been measured. The
existing bench measures ONE WORKER doing one task. It establishes that context
is expensive; it says nothing about whether this server makes it cheaper,
because no parent was ever measured with and without it.

Three arms, because two would conflate two different costs:

    A solo        server not registered; the parent does everything
    B registered  server registered, parent NOT told to use it
    C delegating  server registered, parent told to delegate

B - A is the toll: tool schemas plus instructions.md, paid on every turn before
any work happens. B - C is the saving. A two-arm test reports only A - C and
cannot say which dominates.

Every number here is read from a structured source -- agy's JSON `usage` block
for the parent, the `runs` table for workers. Nothing is parsed out of prose.
That is bench defect D2, where a whole-log grep for "429" matched a
conversation_id and voided a run that had succeeded.
"""

from __future__ import annotations

import json
import re
import shutil
import sqlite3
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from subagents.config import Config, load_config  # noqa: E402

WORK_ROOT = REPO / "bench" / "ab" / "work"
MCP_CONFIG = Path.home() / ".gemini" / "config" / "mcp_config.json"
SERVER_NAME = "subagents"

# Four independent documentation sub-tasks: one wave, four workers.
MODULES = ("worker", "execution", "hashing", "waves")

# Small enough that a worker cannot pad its way past it, large enough that an
# empty stub fails.
MIN_DOC_BYTES = 300

CSV_COLUMNS = [
    "run_id", "arm", "repeat", "wall_s",
    "parent_in", "parent_out", "parent_thinking", "parent_cache_read",
    "worker_in", "worker_out", "worker_count",
    "server_enabled_observed", "delegated", "cached_hits", "files_written",
    "verified", "agy_status", "void_reason",
]


@dataclass
class RunRecord:
    run_id: str
    arm: str
    repeat: int
    wall_s: float = 0.0
    parent_in: int | None = None
    parent_out: int | None = None
    parent_thinking: int | None = None
    parent_cache_read: int | None = None
    worker_in: int = 0
    worker_out: int = 0
    worker_count: int = 0
    server_enabled_observed: bool | None = None
    delegated: bool = False
    cached_hits: int = 0
    files_written: int = 0
    verified: bool = False
    agy_status: str = ""
    void_reason: str = ""
    stderr_tail: str = field(default="", repr=False)

    @property
    def void(self) -> bool:
        return bool(self.void_reason)

    def as_row(self) -> dict:
        data = asdict(self)
        return {col: data[col] for col in CSV_COLUMNS}


# --------------------------------------------------------------- the client config
class ServerToggle:
    """Enable or disable the MCP server, and put the file back afterwards.

    A permanently disabled server -- or worse, a config left half-written by a
    crash mid-measurement -- would break every later session on this machine.
    So: back up first, refuse to start without a backup, restore in a finally,
    and verify the restore.
    """

    def __init__(self, config_path: Path = MCP_CONFIG, name: str = SERVER_NAME):
        self.path = Path(config_path)
        self.name = name
        self.backup = self.path.with_suffix(".json.ab-backup")
        self._original: str | None = None

    def __enter__(self) -> "ServerToggle":
        if not self.path.is_file():
            raise FileNotFoundError(f"no client config at {self.path}")
        self._original = self.path.read_text(encoding="utf-8")
        self.backup.write_text(self._original, encoding="utf-8")
        if not self.backup.is_file():
            raise RuntimeError("refusing to run: could not write a config backup")
        return self

    def __exit__(self, *exc) -> None:
        if self._original is not None:
            self.path.write_text(self._original, encoding="utf-8")
            if self.path.read_text(encoding="utf-8") != self._original:
                # Loud: a half-restored config is worse than never touching it.
                raise RuntimeError(f"config was NOT restored -- recover from {self.backup}")
        self.backup.unlink(missing_ok=True)

    def set_enabled(self, enabled: bool) -> bool:
        """Write the flag, then read it back. Returns what the file says.

        Read back rather than assumed: the arm label in the CSV has to be
        evidence about the configuration the run actually saw, not a note about
        what this function intended.
        """
        doc = json.loads(self.path.read_text(encoding="utf-8"))
        entry = doc.get("mcpServers", {}).get(self.name)
        if entry is None:
            raise KeyError(f"no '{self.name}' entry in {self.path}")
        entry["disabled"] = not enabled
        self.path.write_text(json.dumps(doc, indent=2), encoding="utf-8")
        return self.observed_enabled()

    def observed_enabled(self) -> bool:
        doc = json.loads(self.path.read_text(encoding="utf-8"))
        entry = doc.get("mcpServers", {}).get(self.name, {})
        return not entry.get("disabled", False)


def kill_server_processes() -> int:
    """The client keeps the server process alive across sessions (NOTES §21).

    Disabling the server in the config does nothing to a process that is
    already running, so an arm-A run would still see a live server. Killing it
    between arms is what makes the toggle mean anything.
    """
    if sys.platform != "win32":
        return 0
    script = (
        "Get-CimInstance Win32_Process | "
        "Where-Object { $_.CommandLine -like '*subagents-mcp*server.py*' -and "
        "$_.CommandLine -notlike '*probe*' } | "
        "ForEach-Object { Stop-Process -Id $_.ProcessId -Force; 'killed' }"
    )
    out = subprocess.run(["powershell", "-NoProfile", "-Command", script],
                         capture_output=True, text=True).stdout
    return out.count("killed")


# ------------------------------------------------------------------- the workspace
def materialise_workspace(run_id: str) -> Path:
    """A realistic codebase to document, copied per run.

    Deliberately this repo's own modules rather than bench/fixture_src (132
    lines total): at that size the parent's context is dominated by system
    prompt and tool schemas, and the effect under test would be noise. A real
    audit worker over these files measured 219,870 input tokens.

    It lands inside the repo so it is already covered by
    SUBAGENTS_ALLOWED_ROOTS. Pointing the allowlist elsewhere would mean
    editing .env and restarting the server for it to apply, which is the trap
    of NOTES §23.
    """
    root = WORK_ROOT / run_id
    if root.exists():
        shutil.rmtree(root, ignore_errors=True)
    (root / "src").mkdir(parents=True)
    (root / "docs").mkdir()

    for name in MODULES:
        shutil.copy2(REPO / "subagents" / f"{name}.py", root / "src" / f"{name}.py")
    (root / "README.md").write_text(
        "# fixture\n\nFour modules copied from a real project, to be documented.\n",
        encoding="utf-8",
    )
    return root.resolve()


SOLO_PROMPT = """Read each of these four files and write one Markdown document per file:

{files}

For each module, write docs/<name>.md containing: what the module is for, the
main functions or classes it defines, and any constraint the comments say must
hold. At least 300 characters each. Do not modify anything under src/."""

DELEGATE_PROMPT = """Use the subagents MCP server. Do not do this work yourself, and do not read
the source files yourself.

Call propose_plan with workspace_root "{root}" and one task per module below.
Each task_ref is "doc-<name>", each instruction is "Read src/<name>.py and
write docs/<name>.md containing: what the module is for, the main functions or
classes it defines, and any constraint the comments say must hold. At least 300
characters. Do not modify anything under src/.", each reads is
["src/<name>.py"], each writes is ["docs/<name>.md"].

Modules: {names}

Then call execute_plan with the plan_id and plan_digest it returns, and a short
affects argument naming what is written."""


def prompt_for(arm: str, root: Path) -> str:
    if arm == "C":
        return DELEGATE_PROMPT.format(root=str(root), names=", ".join(MODULES))
    files = "\n".join(f"- src/{name}.py" for name in MODULES)
    return SOLO_PROMPT.format(files=files)


# ------------------------------------------------------------------- verification
def public_symbols(source: str) -> list[str]:
    """Top-level names a document about this module should plausibly mention.

    Regex rather than string surgery, because the obvious surgery is subtly
    wrong: `line.split("(")[0].removeprefix("class ")` leaves the trailing
    colon on a base-less class, so `class WorkerResult:` yields
    `WorkerResult:` -- which appears in no prose ever written, and would have
    voided every real run for "naming none of its module's definitions". Caught
    by a dry run against the fake before any tokens were spent.
    """
    return re.findall(r"^(?:def|class)\s+(\w+)", source, flags=re.MULTILINE)


def verify(root: Path) -> tuple[int, bool, str]:
    """Did the work actually happen? Returns (files, verified, reason).

    A run that did not do the task must never reach a cost median -- that is
    bench defect D1, where void runs were quoted in the published table.
    """
    written = 0
    problems: list[str] = []
    for name in MODULES:
        doc = root / "docs" / f"{name}.md"
        if not doc.is_file():
            problems.append(f"{name}.md missing")
            continue
        written += 1
        text = doc.read_text(encoding="utf-8", errors="replace")
        if len(text.encode("utf-8")) < MIN_DOC_BYTES:
            problems.append(f"{name}.md under {MIN_DOC_BYTES} bytes")
            continue
        # Cheap evidence that it read the file rather than inventing prose: the
        # doc has to name something that actually exists in the module.
        source = (root / "src" / f"{name}.py").read_text(encoding="utf-8", errors="replace")
        defined = public_symbols(source)
        if defined and not any(symbol in text for symbol in defined):
            problems.append(f"{name}.md names none of its module's definitions")

    return written, not problems, "; ".join(problems)


# --------------------------------------------------------------------- the database
def workers_for(db: Path, since: str) -> tuple[int, int, int, bool, int]:
    """(count, input, output, delegated, cached) from the runs table since a timestamp.

    `cached` must be zero for every arm. Each run gets a freshly materialised
    workspace under a unique path, and the cache key covers the absolute
    workspace root, so a hit is structurally impossible -- but "impossible"
    that nothing checks is how a warm cache would quietly improve the headline
    number without anyone noticing it had stopped measuring the same thing.
    """
    if not Path(db).is_file():
        return 0, 0, 0, False, 0
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        rows = conn.execute(
            "SELECT tokens_in, tokens_out FROM runs WHERE started_at > ?", (since,)
        ).fetchall()
        plans = conn.execute(
            "SELECT COUNT(*) FROM plans WHERE created_at > ?", (since,)
        ).fetchone()[0]
        # Cached rows carry no started_at, so they are absent from `rows` above
        # and have to be counted separately -- which is also the point: they
        # never contaminate the token sums.
        cached = conn.execute(
            "SELECT COUNT(*) FROM runs r JOIN plans p ON p.id = r.plan_id"
            " WHERE p.created_at > ? AND r.model_used = 'cache'", (since,)
        ).fetchone()[0]
    finally:
        conn.close()
    return (
        len(rows),
        sum(r[0] or 0 for r in rows),
        sum(r[1] or 0 for r in rows),
        plans > 0,
        cached,
    )


# -------------------------------------------------------------------------- one run
def run_one(arm: str, repeat: int, config: Config, toggle: ServerToggle,
            print_timeout_s: int = 600, agy_cmd: list[str] | None = None) -> RunRecord:
    run_id = f"{arm}-{repeat}-{int(time.time())}"
    record = RunRecord(run_id=run_id, arm=arm, repeat=repeat)

    record.server_enabled_observed = toggle.set_enabled(arm in ("B", "C"))
    kill_server_processes()

    root = materialise_workspace(run_id)
    since = datetime.now(timezone.utc).isoformat()

    cmd = [
        *(agy_cmd or [config.agy_path]),
        "--model", config.model,
        "--print-timeout", f"{print_timeout_s}s",
        "--add-dir", str(root),
        "--output-format", "json",
        # Uniform across all three arms. File tools are auto-approved headlessly
        # regardless, so this changes nothing for A and B; without it arm C
        # cannot call an MCP tool at all (NOTES §31). Identical invocation is
        # what keeps the arms comparable.
        "--dangerously-skip-permissions",
        "--print", prompt_for(arm, root),
    ]

    began = time.time()
    proc = subprocess.run(cmd, capture_output=True, text=True, cwd=str(root))
    record.wall_s = round(time.time() - began, 1)
    record.stderr_tail = (proc.stderr or "")[-400:]

    try:
        doc = json.loads(proc.stdout or "{}")
    except json.JSONDecodeError:
        doc = {}

    usage = doc.get("usage") or {}
    record.agy_status = doc.get("status", "")
    record.parent_in = usage.get("input_tokens")
    record.parent_out = usage.get("output_tokens")
    record.parent_thinking = usage.get("thinking_tokens")
    record.parent_cache_read = usage.get("cache_read_tokens")

    count, win, wout, delegated, cached = workers_for(config.db_path, since)
    record.worker_count, record.worker_in, record.worker_out = count, win, wout
    record.delegated = delegated
    record.cached_hits = cached

    record.files_written, record.verified, problems = verify(root)
    record.void_reason = classify_void(record, problems)
    return record


def classify_void(record: RunRecord, problems: str) -> str:
    """Why this run must not enter a median. Empty string means it counts.

    Read off structured evidence -- denied_actions, the runs table, files on
    disk -- never off the transcript.
    """
    if record.arm == "B" and record.delegated:
        # Interesting, and not arm B: the parent reached for a tool nobody
        # told it about, so this measures something else entirely.
        return "unexpected_delegation"
    if record.arm == "C" and not record.delegated:
        return "did_not_delegate"
    if record.arm == "C" and record.worker_count != len(MODULES):
        return f"expected {len(MODULES)} workers, saw {record.worker_count}"
    if record.arm == "A" and record.server_enabled_observed:
        return "server_was_enabled_for_solo_arm"
    if record.cached_hits:
        # A cache hit means a task was NOT run, so the tokens this arm spent
        # are not the tokens the task costs. Every run materialises its own
        # workspace and the key covers the absolute root, so this should be
        # unreachable -- which is exactly why it is checked rather than
        # assumed. A warm cache would improve the headline number by measuring
        # something else.
        return f"cache served {record.cached_hits} task(s); not a cold measurement"
    if not record.verified:
        return f"work not verified: {problems}"
    return ""
