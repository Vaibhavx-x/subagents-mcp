"""Write bench/quality/RESULTS.md from runs.csv.

    python bench/quality/report.py

The caveats below are written into the generator, not added to the output by
hand afterwards. That ordering is the point: a caveat you can only add after
seeing the numbers is a caveat you can also decide not to add.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))
# Imported by package path, never by bare name. `bench/ab/` and
# `bench/quality/` both define `harness` and `aggregate`, so a bare
# `import aggregate` resolves to whichever directory landed on sys.path first
# -- which depends on test collection order and fails somewhere else entirely.

from bench.quality.aggregate import (  # noqa: E402
    ARMS,
    ceiling_warning,
    deltas,
    for_task,
    read_rows,
    summarise_task,
)
from bench.quality.harness import TASKS  # noqa: E402

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

CSV = REPO / "bench" / "quality" / "runs.csv"
OUT = REPO / "bench" / "quality" / "RESULTS.md"

TASK_BLURB = {
    "audit": ("Defect audit", "Four modules with 16 planted defects, four per "
              "module. Scored on **recall** -- a finding counts when it names a "
              "line within 2 of a planted one. Graded on line numbers only, "
              "never on the description."),
    "extract": ("Answer-key extraction", "Twelve questions whose answers are "
                "exact strings in the code. **Two are deliberately "
                "cross-module**: their evidence lives in a module other than "
                "the one the question names, which is where delegating by file "
                "should hurt."),
}


def pct(value: float | None) -> str:
    return "--" if value is None else f"{value * 100:.0f}%"


def num(value: float | None, digits: int = 0) -> str:
    if value is None:
        return "--"
    return f"{value:,.{digits}f}"


def span(pair, digits: int = 0, scale: float = 1.0) -> str:
    if not pair:
        return "--"
    return f"{pair[0] * scale:,.{digits}f}-{pair[1] * scale:,.{digits}f}"


def main() -> int:
    if not CSV.is_file():
        print(f"no runs at {CSV} -- run: python bench/quality/run_quality.py")
        return 1
    rows = read_rows(CSV)
    if not rows:
        print("runs.csv is empty")
        return 1

    lines: list[str] = []
    add = lines.append

    add("# Is the delegated work any good?")
    add("")
    add("`bench/ab/RESULTS.md` measured what delegation **costs**. It could not "
        "measure whether the work was any good, and said so: *\"23 of 23 graded "
        "runs passed. This bench ranks cost and cannot rank quality.\"*")
    add("")
    add("This one scores the output. Two tasks, each graded mechanically "
        "against a key generated from the edit that produced it, run with the "
        "tool (**C**) and without it (**A**).")
    add("")
    add("```bash")
    add("python bench/quality/inject.py --check      # the key matches the fixture")
    add("python bench/quality/run_quality.py --repeats 5")
    add("python bench/quality/report.py")
    add("```")
    add("")

    for task in TASKS:
        task_rows = for_task(rows, task)
        if not task_rows:
            continue
        summary = summarise_task(task_rows)
        delta = deltas(summary)
        title, blurb = TASK_BLURB[task]

        add("---")
        add("")
        add(f"## {title}")
        add("")
        add(blurb)
        add("")

        warning = ceiling_warning(summary)
        if warning:
            add(f"> **{warning}**")
            add("")

        add("| arm | accuracy | spread | wall clock | parent in | in/turn | total in |")
        add("|---|---|---|---|---|---|---|")
        for arm, name, _ in ARMS:
            s = summary[arm]
            add(f"| **{arm} - {name}** | **{pct(s['accuracy'])}** | "
                f"{span(s['accuracy_spread'], 0, 100)}% | "
                f"{num(s['wall_s'], 1)}s | {num(s['parent_in'])} | "
                f"{num(s['in_per_turn'])} | {num(s['total_in'])} |")
        add("")

        if "accuracy_delta" in delta:
            direction = ("better" if delta["accuracy_delta"] > 0
                         else "worse" if delta["accuracy_delta"] < 0 else "level")
            add(f"**Accuracy: delegating scored {abs(delta['accuracy_delta']) * 100:.0f} "
                f"points {direction}** -- {delta['accuracy_verdict']}.")
            add("")
        for label, key, verdict_key, fmt in (
            ("Parent input", "parent_ratio", "parent_verdict", "{:.2f}x less"),
            ("Input per turn", "per_turn_ratio", "per_turn_verdict", "{:.2f}x less"),
            ("Wall clock", "wall_ratio", "wall_verdict", "{:.2f}x faster"),
        ):
            if key in delta:
                add(f"- **{label}:** " + fmt.format(delta[key])
                    + f" for the delegating arm -- {delta[verdict_key]}.")
        if "total_cost_ratio" in delta:
            add(f"- **Total tokens: {delta['total_cost_ratio']:.2f}x** -- "
                f"delegation spends more overall, because each worker re-reads "
                f"context the parent already had.")
        add("")

        add("| arm | n | void | timeouts | format failures | precision |")
        add("|---|---|---|---|---|---|")
        for arm, name, _ in ARMS:
            s = summary[arm]
            fails = ("--" if s["parse_rate"] is None
                     else f"{(1 - s['parse_rate']) * 100:.0f}%")
            add(f"| {arm} | {s['n']} | {s['void']} | {s['timeouts']} | {fails} | "
                f"{pct(s['precision'])} |")
        add("")
        for arm, _, _ in ARMS:
            for reason in summary[arm]["void_reasons"]:
                add(f"- arm {arm} void: {reason}")
        add("")

    add("---")
    add("")
    add("## What these numbers are not")
    add("")
    add("- **`parent in` is cumulative input across turns, not peak context.** "
        "Ten turns of 20k and two of 100k both total 200k, and only the second "
        "fills a window. `in/turn` sits beside it as the closer proxy for "
        "context pressure; neither alone answers \"did the parent's context "
        "stay clean\".")
    add("- **Total tokens go up.** The claim is about the parent's window, not "
        "about spending less. A reader who discovered that ratio for themselves "
        "after adopting this would be right to distrust everything else here.")
    add("- **No delta whose ranges overlap is reported as an effect.** At five "
        "repeats there is no significance test worth running, but overlap is "
        "free to check. Measured twice already: n=3 said 2.06x and n=5 said "
        "1.59x (`NOTES.md` section 35).")
    add("- **A zero-scoring run is in these medians.** Only runs that do not "
        "measure what we think they do are void -- wrong arm state, a cache "
        "hit, agy failing outright. Dropping runs that scored badly would have "
        "been the comfortable choice and would have deleted the difference "
        "between the arms.")
    add("- **Arm C is measured under close-to-best-case delegation.** Its "
        "prompt carries the decomposition and tells it not to read the source "
        "itself. A parent meeting this server cold spends ~16 tool calls "
        "exploring first (`NOTES.md` section 37), and that is not in these "
        "numbers.")
    add("- **Scope:** one machine, one model, two fixtures derived from one "
        "codebase, five repeats.")
    add("")

    OUT.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"wrote {OUT} from {len(rows)} run(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
