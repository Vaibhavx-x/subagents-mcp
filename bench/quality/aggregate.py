"""Turn quality runs into numbers, per task, with the caveats attached.

`bench/ab/aggregate.py` already knows how to take a median with its spread and
refuse to call an overlapping delta an effect. That logic is task-agnostic and
is imported rather than rewritten -- two copies of "is this difference real"
is how two benches end up disagreeing with each other.

What is new here is the honest handling of `parent_in`. It is **cumulative
input across turns**, not peak context occupancy: ten turns of 20k and two of
100k both total 200k, and only the second fills a window. `num_turns` is
recorded so input-per-turn can sit beside it as the closer proxy for context
pressure. Neither number alone answers "did the parent's context stay clean".
"""

from __future__ import annotations

import csv
import statistics
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from bench.ab.aggregate import median_of, overlaps, spread_of  # noqa: E402

ARMS = [
    ("A", "solo", "server not registered; the parent does everything"),
    ("C", "delegating", "server registered, parent delegates one task per module"),
]


def read_rows(csv_path: Path) -> list[dict]:
    with open(csv_path, newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def for_task(rows: list[dict], task: str) -> list[dict]:
    return [r for r in rows if r.get("task") == task]


def counted(rows: list[dict], arm: str) -> list[dict]:
    """Eligible for a median: this arm, not void.

    A zero-scoring run IS eligible. Only a run that does not measure what we
    think it does is excluded -- see `harness.classify_void`.
    """
    return [r for r in rows if r["arm"] == arm and not (r.get("void_reason") or "").strip()]


def voided(rows: list[dict], arm: str) -> list[dict]:
    return [r for r in rows if r["arm"] == arm and (r.get("void_reason") or "").strip()]


def _rate(rows: list[dict], column: str) -> float | None:
    """Fraction of runs where a boolean column is true."""
    values = [(r.get(column) or "").strip().lower() for r in rows]
    values = [v for v in values if v]
    if not values:
        return None
    return sum(1 for v in values if v in ("true", "1")) / len(values)


def per_turn(rows: list[dict]) -> list[float]:
    """Mean input tokens per turn, per run.

    Computed per run and then medianed -- never as median(parent_in) divided by
    median(num_turns), which is a ratio of two different runs' numbers.
    """
    out: list[float] = []
    for row in rows:
        try:
            total, turns = float(row["parent_in"]), float(row["num_turns"])
        except (TypeError, ValueError, KeyError):
            continue
        if turns > 0:
            out.append(total / turns)
    return out


def summarise_task(rows: list[dict]) -> dict:
    summary: dict = {}
    for arm, name, _ in ARMS:
        ok = counted(rows, arm)
        turns = per_turn(ok)
        summary[arm] = {
            "name": name,
            "n": len(ok),
            "void": len(voided(rows, arm)),
            "void_reasons": sorted({r["void_reason"] for r in voided(rows, arm)}),
            "accuracy": median_of(ok, "accuracy"),
            "accuracy_spread": spread_of(ok, "accuracy"),
            "precision": median_of(ok, "precision"),
            "wall_s": median_of(ok, "wall_s"),
            "wall_spread": spread_of(ok, "wall_s"),
            "parent_in": median_of(ok, "parent_in"),
            "parent_in_spread": spread_of(ok, "parent_in"),
            "parent_out": median_of(ok, "parent_out"),
            "parent_cache_read": median_of(ok, "parent_cache_read"),
            "num_turns": median_of(ok, "num_turns"),
            # statistics.median, not sorted()[n//2]: at an even n those differ,
            # and the second one silently disagreed with every other median in
            # this table -- it reported 386,517 where the median was 295,038.
            "in_per_turn": (statistics.median(turns) if turns else None),
            "in_per_turn_spread": ((min(turns), max(turns)) if turns else None),
            "worker_in": median_of(ok, "worker_in"),
            "worker_out": median_of(ok, "worker_out"),
            "parse_rate": _rate(ok, "parse_ok"),
            "timeouts": sum(1 for r in ok if (r.get("hit_timeout") or "").lower()
                            in ("true", "1")),
        }
        summary[arm]["total_in"] = (summary[arm]["parent_in"] or 0) + (
            summary[arm]["worker_in"] or 0)
    return summary


def verdict(summary: dict, column: str) -> str:
    """How a difference between the arms may honestly be described."""
    spread_key = {
        "accuracy": "accuracy_spread",
        "parent_in": "parent_in_spread",
        "wall_s": "wall_spread",
        "in_per_turn": "in_per_turn_spread",
    }[column]
    if summary["A"][column] is None or summary["C"][column] is None:
        return "no data"
    if overlaps(summary["A"][spread_key], summary["C"][spread_key]):
        return "not measurable -- the observed ranges overlap"
    return "ranges do not overlap"


def deltas(summary: dict) -> dict:
    a, c = summary["A"], summary["C"]
    out: dict = {}
    if a["accuracy"] is not None and c["accuracy"] is not None:
        out["accuracy_delta"] = c["accuracy"] - a["accuracy"]
        out["accuracy_verdict"] = verdict(summary, "accuracy")
    if a["parent_in"] and c["parent_in"]:
        out["parent_ratio"] = a["parent_in"] / c["parent_in"]
        out["parent_verdict"] = verdict(summary, "parent_in")
    if a["wall_s"] and c["wall_s"]:
        out["wall_ratio"] = a["wall_s"] / c["wall_s"]
        out["wall_verdict"] = verdict(summary, "wall_s")
    if a["in_per_turn"] and c["in_per_turn"]:
        out["per_turn_ratio"] = a["in_per_turn"] / c["in_per_turn"]
        out["per_turn_verdict"] = verdict(summary, "in_per_turn")
    if a["total_in"] and c["total_in"]:
        out["total_cost_ratio"] = c["total_in"] / a["total_in"]
    return out


def turns_are_degenerate(rows: list[dict]) -> bool:
    """Did every run report exactly one turn?

    agy reports a whole `--print` run as a single turn, which makes
    input-per-turn identical to cumulative input and therefore useless as the
    peak-context proxy it was added to be. Reported rather than dropped: a
    column removed because it did not discriminate is a column nobody can
    check (NOTES.md section 46).
    """
    values = {(r.get("num_turns") or "").strip() for r in rows}
    return values <= {"1", ""}


def ceiling_warning(summary: dict) -> str:
    """The failure this whole bench exists to avoid.

    `bench/RESULTS.md` section 3: 23 of 23 graded runs passed, so that bench
    could rank cost and not quality. If both arms sit at the top or the bottom
    here, the task measured nothing and the report has to say so rather than
    reporting a difference of zero as "no difference".
    """
    scores = [summary[arm]["accuracy"] for arm, _, _ in ARMS
              if summary[arm]["accuracy"] is not None]
    if not scores:
        return ""
    if min(scores) >= 0.9:
        return ("CEILING: both arms scored 90%+ -- this task was too easy to "
                "separate them, and the accuracy comparison measures nothing.")
    if max(scores) <= 0.1:
        return ("FLOOR: neither arm scored above 10% -- this task was too hard, "
                "and the accuracy comparison measures nothing.")
    return ""
