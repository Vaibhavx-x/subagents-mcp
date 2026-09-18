#!/usr/bin/env python3
"""Reads results.csv and prints the comparison tables. No dependencies."""
import csv, statistics, sys, collections, pathlib

CSV = pathlib.Path(__file__).parent / "results.csv"
CONFIGS = [("A", "3.8 low"), ("B", "3.8 medium"), ("C", "3.8 high"), ("D", "3.7 medium")]
TASKS = [("t1", "T1 mechanical"), ("t2", "T2 implement"), ("t3", "T3 debug")]

rows = list(csv.DictReader(CSV.open()))
if not rows:
    sys.exit("no results yet")

graded = [r for r in rows if r["status"] in ("ok", "fail")]
excluded = collections.Counter(r["status"] for r in rows if r["status"] not in ("ok", "fail"))

def table(title, header, cell):
    print(f"\n### {title}\n")
    print("| config | " + " | ".join(h for _, h in TASKS) + " |")
    print("|---" * (len(TASKS) + 1) + "|")
    for cid, cname in CONFIGS:
        cells = [cell(cid, tid) for tid, _ in TASKS]
        print(f"| {cname} | " + " | ".join(cells) + " |")

def sel(cid, tid, only_pass=False):
    out = [r for r in graded if r["config"] == cid and r["task"] == tid]
    return [r for r in out if r["passed"] == "1"] if only_pass else out

table("Pass rate", None, lambda c, t: (
    f"{sum(1 for r in sel(c,t) if r['passed']=='1')}/{len(sel(c,t))}" if sel(c, t) else "-"))

table("Median wall-clock, passing runs (s)", None, lambda c, t: (
    f"{statistics.median(int(r['wall_ms']) for r in sel(c,t,True))/1000:.0f}"
    if sel(c, t, True) else "-"))

table("Median output tokens, passing runs", None, lambda c, t: (
    f"{statistics.median(int(r['output_tokens']) for r in sel(c,t,True)):,.0f}"
    if sel(c, t, True) else "-"))

table("Median thinking tokens, passing runs", None, lambda c, t: (
    f"{statistics.median(int(r['thinking_tokens']) for r in sel(c,t,True)):,.0f}"
    if sel(c, t, True) else "-"))

table("Median INPUT tokens, passing runs", None, lambda c, t: (
    f"{statistics.median(int(r['input_tokens']) for r in sel(c,t,True)):,.0f}"
    if sel(c, t, True) else "-"))

table("Median cache-read tokens, passing runs", None, lambda c, t: (
    f"{statistics.median(int(r['cache_read_tokens']) for r in sel(c,t,True)):,.0f}"
    if sel(c, t, True) else "-"))

print("\n### Cheapest config reaching full pass rate\n")
print("| task | config | median s |")
print("|---|---|---|")
for tid, tname in TASKS:
    best = None
    for cid, cname in CONFIGS:
        runs = sel(cid, tid)
        if runs and all(r["passed"] == "1" for r in runs):
            med = statistics.median(int(r["wall_ms"]) for r in runs) / 1000
            if best is None or med < best[1]:
                best = (cname, med)
    print(f"| {tname} | {best[0] if best else 'none passed all'} | {f'{best[1]:.0f}' if best else '-'} |")

if excluded:
    print("\n### Excluded from aggregates\n")
    for k, v in excluded.items():
        print(f"- {k}: {v}")
