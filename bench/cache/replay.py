"""How often would the cache have hit, over this install's real history?

Free: reads the existing database, spawns nothing, spends nothing.

    python bench/cache/replay.py            # the default database
    python bench/cache/replay.py path.db

What this can and cannot measure
--------------------------------

It measures an **upper bound**, and says so in the output rather than in a
footnote. A real hit needs three things to hold:

1. the same identity -- workspace root, instruction, model, declared paths;
2. every declared read hashing the same as when the recorded run started;
3. every declared write still hashing to what that run left behind.

Only (1) is answerable from history, because `file_hashes` was a table nothing
wrote to until Phase 5 (`NOTES.md` section 38), so (2) and (3) have no data
before today. Every run counted here as a would-be hit therefore *might* have
been one; none of the runs not counted could have been.

So: if this prints zero, the true rate is zero and the bound is tight. If it
prints more than zero, the true rate is somewhere at or below it and the gap
is not knowable from these rows. That asymmetry is the whole reason to report
a bound instead of a rate.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from subagents.config import load_config  # noqa: E402

sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def identity(row: sqlite3.Row) -> tuple:
    """Everything about a run the cache key covers EXCEPT the content hashes."""
    return (
        (row["workspace_root"] or "").lower(),
        row["instruction"],
        row["model_requested"],
        tuple(sorted(p.lower() for p in json.loads(row["declared_reads"] or "[]"))),
        tuple(sorted(p.lower() for p in json.loads(row["declared_writes"] or "[]"))),
    )


def parse(stamp: str | None) -> datetime | None:
    try:
        return datetime.fromisoformat(stamp) if stamp else None
    except ValueError:
        return None


def main() -> int:
    db = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(load_config().db_path)
    ttl_s = load_config().cache_ttl_s or 3600

    if not db.is_file():
        print(f"no database at {db} -- nothing to replay")
        return 1

    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT r.*, p.workspace_root FROM runs r JOIN plans p ON p.id = r.plan_id"
        " ORDER BY COALESCE(r.finished_at, r.started_at, '')"
    ).fetchall()
    conn.close()

    # Entries a run would have LEFT behind, keyed by identity: the moment the
    # most recent eligible run with that identity finished.
    left_behind: dict[tuple, datetime] = {}
    counts: Counter[str] = Counter()
    would_hit: list[str] = []

    for row in rows:
        key = identity(row)
        finished = parse(row["finished_at"])

        if row["model_used"] == "cache":
            # A run this feature already served. Not evidence about history.
            counts["already_cached"] += 1
            continue

        no_reads = not json.loads(row["declared_reads"] or "[]")
        if no_reads:
            counts["not_cacheable_no_declared_reads"] += 1
        elif key in left_behind:
            age = (finished - left_behind[key]).total_seconds() if finished else None
            if age is not None and age <= ttl_s:
                counts["would_have_hit"] += 1
                would_hit.append(f"{row['task_ref']} ({age / 60:.0f}m after its predecessor)")
            else:
                counts["identity_matched_but_expired"] += 1
        else:
            counts["cold_no_predecessor"] += 1

        # Would this run have left a usable entry?
        if row["status"] == "ok" and not row["tainted"] and not no_reads and finished:
            left_behind[key] = finished

    total = sum(counts.values())
    print(f"database : {db}")
    print(f"runs     : {total}")
    print(f"ttl      : {ttl_s}s\n")

    print("UPPER BOUND on the historical hit rate")
    hits = counts["would_have_hit"]
    eligible = total - counts["not_cacheable_no_declared_reads"] - counts["already_cached"]
    rate = (hits / eligible * 100) if eligible else 0.0
    print(f"  {hits} of {eligible} eligible run(s) -- at most {rate:.0f}%")
    for line in would_hit[:10]:
        print(f"    {line}")
    print()

    print("why the rest could not have hit")
    for label in ("cold_no_predecessor", "identity_matched_but_expired",
                  "not_cacheable_no_declared_reads", "already_cached"):
        if counts[label]:
            print(f"  {counts[label]:>4}  {label.replace('_', ' ')}")

    print("\nThis is a BOUND, not a rate: the content-hash halves of the check have")
    print("no data before Phase 5, so a counted run only MIGHT have hit. A run not")
    print("counted could not have. Zero here means zero (NOTES.md section 38).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
