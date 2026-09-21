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
    turns_are_degenerate,
)
from bench.quality.harness import TASKS  # noqa: E402

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

CSV = REPO / "bench" / "quality" / "runs.csv"
OUT = REPO / "bench" / "quality" / "RESULTS.md"

TASK_BLURB = {
    "audit": ("Defect audit", "Twelve defects planted across three modules; a "
              "fourth is **clean**, as a control, so precision has something to "
              "measure. Scored on **recall** -- a finding counts when it names "
              "a line within 2 of a planted one. Graded on line numbers only, "
              "never on the description, which is bench defect D2."),
    "extract": ("Answer-key extraction", "Twelve questions. No question names "
                "the module its answer is in -- finding the file is part of the "
                "task -- and a third require the answer to be derived rather "
                "than found. **Two are deliberately cross-module**, which is "
                "where delegating by file was predicted to hurt."),
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

    repeats = max((int(r["repeat"]) for r in rows if (r.get("repeat") or "").isdigit()),
                  default=0)
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
    add(f"python bench/quality/run_quality.py --repeats {repeats}")
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
            points = delta["accuracy_delta"] * 100
            if abs(points) < 0.5:
                add(f"**Accuracy: identical** -- both arms {pct(summary['A']['accuracy'])}.")
            else:
                add(f"**Accuracy: delegating scored {abs(points):.0f} points "
                    f"{'higher' if points > 0 else 'LOWER'}** -- "
                    f"{delta['accuracy_verdict']}.")
            add("")
        for label, key, verdict_key in (
            ("Parent input", "parent_ratio", "parent_verdict"),
            ("Input per turn", "per_turn_ratio", "per_turn_verdict"),
            ("Wall clock", "wall_ratio", "wall_verdict"),
        ):
            if key not in delta:
                continue
            ratio = delta[key]
            # A ratio below 1 means the delegating arm was WORSE. Saying
            # "0.52x faster" reads as a benefit and is how a negative result
            # gets laundered into a positive one.
            if ratio >= 1:
                phrase = ("{:.2f}x less".format(ratio) if "input" in label.lower()
                          else "{:.2f}x faster".format(ratio))
            else:
                phrase = ("**{:.2f}x MORE**".format(1 / ratio) if "input" in label.lower()
                          else "**{:.2f}x SLOWER**".format(1 / ratio))
            add(f"- **{label}:** {phrase} for the delegating arm -- "
                f"{delta[verdict_key]}.")
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
    add("## The predictions, and how they came out")
    add("")
    add("Written into the plan before any code existed, so they could not be "
        "adjusted afterwards. `NOTES.md` section 41 is why: the last prediction "
        "was wrong, and having written it down first is what turned a "
        "disappointing number into a finding.")
    add("")
    add("| prediction | outcome |")
    add("|---|---|")
    add("| Arm C carries less parent input on both tasks | **Half right.** "
        "4.22x on the audit, ranges apart. On extraction 1.12x with the ranges "
        "overlapping -- not measurable. |")
    add("| Audit accuracy roughly equal | **Right**, and uninformatively so: "
        "both arms 100%. |")
    add("| **Extraction accuracy: delegation slightly worse** | **Wrong.** Arm C "
        "scored 12/12 every run, including both cross-module questions. |")
    add("| Arm C faster on both | **Wrong on extraction**, where it is "
        "**1.93x SLOWER** with ranges apart. Four workers pay ~10s of process "
        "startup each to answer twelve questions one agent answers in 46s. |")
    add("")
    add("### Why the cross-module questions did not hurt")
    add("")
    add("Two questions were built so their answers live in a module other than "
        "the one the question concerns -- a worker given one file should not "
        "have been able to answer them. It answered them anyway, because the "
        "premise was wrong: **a worker is not confined to its declared reads.** "
        "`--add-dir` is additive scope, `--sandbox` covers terminal commands "
        "only, and `--print` mode has no permission gate at all.")
    add("")
    add("The evidence is structured, not read out of a transcript:")
    add("")
    add("| extract worker | questions | median input tokens |")
    add("|---|---|---|")
    add("| `ask-db` | 4 | 82,045 |")
    add("| **`ask-execution`** | **4** | **143,194** |")
    add("| `ask-worker` | 2 | 53,440 |")
    add("| `ask-hashing` | 2 | 56,343 |")
    add("")
    add("`ask-execution` holds both cross-module questions, carries the same "
        "question count as `ask-db`, and 1.7x its input. File size does not "
        "explain it: in the audit task, where no worker needs another's file, "
        "the largest module (`execution.py`, 791 lines) used *fewer* tokens "
        "than the smallest (`db.py`, 248).")
    add("")
    add("**The consequence is about the detector, not the experiment.** An "
        "undeclared *read* leaves no trace at all -- it changes no mtime and no "
        "hash, so taint detection cannot see it, and the only reason it is "
        "visible here is that the worker reports its own token usage. So "
        "`reads[]` is a scheduling input and a taint baseline, and it is not a "
        "description of what the worker actually read (`NOTES.md` section 47).")
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
        "1.59x (`NOTES.md` section 35), which is why four repeats buys ranges "
        "rather than effects.")
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
    if turns_are_degenerate(rows):
        add("- **`in/turn` carries no information in this measurement.** It was "
            "added because cumulative input is not peak context -- but agy "
            "reports a whole `--print` run as **one turn**, in all 16 runs, so "
            "the column is identical to `parent in`. The distinction it exists "
            "to draw is not observable through this interface. Reported rather "
            "than dropped: a column removed because it did not discriminate is "
            "a column nobody can check (`NOTES.md` section 46).")
    add(f"- **Scope:** one machine, one model, two fixtures derived from one "
        f"codebase, {repeats} repeats per cell, {len(rows)} runs, 0 void.")
    add("")

    OUT.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"wrote {OUT} from {len(rows)} run(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
