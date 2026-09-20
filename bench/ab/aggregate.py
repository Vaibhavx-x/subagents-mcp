"""Turn A/B run records into the numbers, and the caveats that go with them.

Void runs never reach a median. That is bench defect D1, where runs voided by a
harness bug were still quoted in the published table -- the cost figures were
real but attached to work that had not happened.
"""

from __future__ import annotations

import csv
import statistics
from pathlib import Path

ARMS = [
    ("A", "solo", "server not registered; the parent does everything"),
    ("B", "registered", "server registered, parent not told to use it"),
    ("C", "delegating", "server registered, parent delegates"),
]


def read_rows(csv_path: Path) -> list[dict]:
    with open(csv_path, newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def counted(rows: list[dict], arm: str) -> list[dict]:
    """Runs eligible for a median: this arm, and not void."""
    return [r for r in rows if r["arm"] == arm and not (r.get("void_reason") or "").strip()]


def voided(rows: list[dict], arm: str) -> list[dict]:
    return [r for r in rows if r["arm"] == arm and (r.get("void_reason") or "").strip()]


def _numbers(rows: list[dict], column: str) -> list[float]:
    out = []
    for row in rows:
        value = (row.get(column) or "").strip()
        if value:
            try:
                out.append(float(value))
            except ValueError:
                continue
    return out


def median_of(rows: list[dict], column: str) -> float | None:
    values = _numbers(rows, column)
    return statistics.median(values) if values else None


def spread_of(rows: list[dict], column: str) -> tuple[float, float] | None:
    """min and max, reported alongside every median.

    Three runs cannot separate an effect smaller than the spread, and a median
    of three hides exactly that. Bench runs varied 110k-305k input tokens per
    turn, so this is not a theoretical concern.
    """
    values = _numbers(rows, column)
    return (min(values), max(values)) if values else None


def summarise(rows: list[dict]) -> dict:
    summary = {}
    for arm, name, _ in ARMS:
        ok = counted(rows, arm)
        summary[arm] = {
            "name": name,
            "n": len(ok),
            "void": len(voided(rows, arm)),
            "void_reasons": sorted({r["void_reason"] for r in voided(rows, arm)}),
            "parent_in": median_of(ok, "parent_in"),
            "parent_in_spread": spread_of(ok, "parent_in"),
            "parent_out": median_of(ok, "parent_out"),
            "parent_cache_read": median_of(ok, "parent_cache_read"),
            "worker_in": median_of(ok, "worker_in"),
            "worker_out": median_of(ok, "worker_out"),
            "wall_s": median_of(ok, "wall_s"),
            "wall_spread": spread_of(ok, "wall_s"),
        }
        parent_in = summary[arm]["parent_in"] or 0
        worker_in = summary[arm]["worker_in"] or 0
        summary[arm]["total_in"] = parent_in + worker_in
    return summary


def deltas(summary: dict) -> dict:
    """The two numbers the three arms exist to separate.

    `toll` is what registering the server costs before any work happens --
    schemas and instructions, paid on every turn. `saving` is what delegation
    then returns. A two-arm test reports only their sum.
    """
    a, b, c = (summary[k]["parent_in"] for k in ("A", "B", "C"))
    out: dict = {"toll": None, "saving": None, "net": None, "total_cost_ratio": None}
    if a is not None and b is not None:
        out["toll"] = b - a
    if b is not None and c is not None:
        out["saving"] = b - c
    if a is not None and c is not None:
        out["net"] = a - c
        if c:
            out["parent_ratio"] = a / c
    total_a, total_c = summary["A"]["total_in"], summary["C"]["total_in"]
    if total_a and total_c:
        out["total_cost_ratio"] = total_c / total_a
    return out
