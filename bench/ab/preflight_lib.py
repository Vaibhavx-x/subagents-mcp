"""Shared probe machinery for the pre-flight scripts.

Evidence comes from the database -- a plan row either exists or it does not --
never from parsing the parent's prose. That distinction is bench defect D2,
where a whole-log grep for "429" matched a conversation_id and voided a run
that had succeeded.
"""

from __future__ import annotations

import json
import shutil
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from subagents.config import load_config  # noqa: E402

WORK = REPO / "bench" / "ab" / "work" / "preflight"
SETTINGS = Path.home() / ".gemini" / "antigravity-cli" / "settings.json"
PRINT_TIMEOUT_S = 120

PROMPT = """Use the subagents MCP server. Do not do this work yourself.

Call propose_plan with workspace_root "{root}" and one task:
task_ref "hello", instruction "Create a file named hello.txt in the workspace
root containing exactly the single word: hi. Do nothing else.",
reads ["README.md"], writes ["hello.txt"]

Then call execute_plan with the plan_id and plan_digest it returns, and an
affects argument describing what it touches."""


def fresh_workspace() -> Path:
    if WORK.exists():
        shutil.rmtree(WORK, ignore_errors=True)
    WORK.mkdir(parents=True)
    (WORK / "README.md").write_text(
        "# preflight\n\nA throwaway workspace for one probe.\n", encoding="utf-8"
    )
    return WORK.resolve()


def plans_since(db: Path, started: str) -> list[tuple[str, str]]:
    """Plans and their statuses created after a timestamp."""
    if not Path(db).is_file():
        return []
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        return [
            (r[0], r[1])
            for r in conn.execute(
                "SELECT id, status FROM plans WHERE created_at > ? ORDER BY created_at",
                (started,),
            )
        ]
    finally:
        conn.close()


def run_probe(label: str, root: Path, skip_permissions: bool, db: Path) -> dict:
    config = load_config()
    cmd = [
        config.agy_path,
        "--model", config.model,
        "--print-timeout", f"{PRINT_TIMEOUT_S}s",
        "--add-dir", str(root),
        "--output-format", "json",
    ]
    if skip_permissions:
        cmd.append("--dangerously-skip-permissions")
    cmd += ["--print", PROMPT.format(root=str(root))]

    started_at = datetime.now(timezone.utc).isoformat()
    began = time.time()
    proc = subprocess.run(cmd, capture_output=True, text=True, cwd=str(root))
    elapsed = time.time() - began

    try:
        doc = json.loads(proc.stdout or "{}")
    except json.JSONDecodeError:
        doc = {}

    plans = plans_since(db, started_at)
    executed = [p for p, status in plans if status in ("complete", "partial", "executing")]

    return {
        "label": label,
        "skip_permissions": skip_permissions,
        "elapsed_s": round(elapsed, 1),
        "agy_status": doc.get("status", ""),
        "denied_actions": doc.get("denied_actions") or [],
        "response_chars": len((doc.get("response") or "").strip()),
        "input_tokens": (doc.get("usage") or {}).get("input_tokens"),
        "plans_created": len(plans),
        "plans_executed": len(executed),
        "file_written": (root / "hello.txt").is_file(),
        "stderr_tail": (proc.stderr or "")[-400:],
    }


def verdict(result: dict) -> str:
    if result["plans_created"] == 0:
        return "REFUSED -- propose_plan never ran"
    if result["plans_executed"] == 0:
        return "PARTIAL -- propose_plan ran, execute_plan did not"
    if not result["file_written"]:
        return "EXECUTED but the worker produced no file"
    return "OK -- both tools ran and the worker did the work"
