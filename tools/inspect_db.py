"""Read-only look at what the last run actually recorded.

    python tools/inspect_db.py            # the most recent plan
    python tools/inspect_db.py <plan_id>  # a specific one

Exists so that verifying a real run is one command rather than ad-hoc SQL.
Opens the database read-only and writes to stdout: this is a CLI, never
imported by the server, so stdout is not the protocol channel here.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from subagents.config import load_config  # noqa: E402

# Worker summaries are model-authored and routinely contain curly quotes, dashes
# and worse. A Windows console is cp1252, where printing those raises
# UnicodeEncodeError -- the inspector would crash on exactly the runs worth
# inspecting.
sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def clip(text: object, n: int = 88) -> str:
    s = " ".join(str(text or "").split())
    return s if len(s) <= n else s[: n - 1] + "\u2026"


def main() -> int:
    db = load_config().db_path
    if not Path(db).is_file():
        print(f"no database at {db} -- nothing has run yet")
        return 1

    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row

    if len(sys.argv) > 1:
        plan = conn.execute("SELECT * FROM plans WHERE id = ?", (sys.argv[1],)).fetchone()
    else:
        plan = conn.execute("SELECT * FROM plans ORDER BY created_at DESC LIMIT 1").fetchone()
    if plan is None:
        print("no such plan")
        return 1

    print(f"PLAN {plan['id']}   status={plan['status']}   created={plan['created_at']}")
    print(f"  affects : {clip(plan['scope_summary']) or '(none recorded)'}")
    print(f"  digest  : {plan['plan_digest'][:16]}...   expires={plan['expires_at']}")

    execs = conn.execute(
        "SELECT * FROM executions WHERE plan_id = ? ORDER BY started_at", (plan["id"],)
    ).fetchall()
    for e in execs:
        print(f"  exec    : outcome={e['outcome']} started={e['started_at']} "
              f"workers {e['workers_completed']}/{e['workers_started']}")
    if not execs:
        print("  exec    : none -- nothing was ever executed for this plan")

    rows = conn.execute(
        "SELECT r.*, s.summary, s.content_bytes FROM runs r"
        " LEFT JOIN results s ON s.run_id = r.id"
        " WHERE r.plan_id = ? ORDER BY r.wave_index, r.task_ref", (plan["id"],)
    ).fetchall()
    print(f"\n  {len(rows)} run(s)")
    for r in rows:
        tok = (f"in={r['tokens_in']} out={r['tokens_out']}"
               if r["tokens_in"] is not None else "no usage recorded")
        print(f"\n  [{r['task_ref']}] wave={r['wave_index']} status={r['status']}  {tok}")
        print(f"      reason   : {clip(r['exit_reason'])}")
        print(f"      timing   : {r['started_at']} -> {r['finished_at']}")
        print(f"      stored   : {r['content_bytes'] or 0} bytes of transcript")
        print(f"      summary  : {clip(r['summary'])}")
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
