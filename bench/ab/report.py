"""Write bench/ab/RESULTS.md from runs.csv.

The caveats are generated alongside the numbers rather than added afterwards,
because a caveat you write once the table looks good is a caveat you are
tempted to soften.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(Path(__file__).parent))

from aggregate import ARMS, delta_verdict, deltas, read_rows, summarise  # noqa: E402
from harness import MODULES  # noqa: E402

RUNS = REPO / "bench" / "ab" / "runs.csv"
OUT = REPO / "bench" / "ab" / "RESULTS.md"


def n(value) -> str:
    if value is None:
        return "—"
    return f"{value:,.0f}"


def spread(pair) -> str:
    return "—" if not pair else f"{pair[0]:,.0f}–{pair[1]:,.0f}"


def build(rows: list[dict]) -> str:
    summary = summarise(rows)
    delta = deltas(summary)
    total_runs = len(rows)
    total_void = sum(summary[a]["void"] for a, _, _ in ARMS)

    lines: list[str] = []
    add = lines.append

    add("# A/B — what delegation costs, and what it saves\n")
    add(f"{total_runs} runs, {total_void} void. Each run asks for the same "
        f"deliverable: one Markdown document per module for "
        f"{len(MODULES)} modules ({', '.join(MODULES)}), each at least 300 bytes "
        "and naming something the module actually defines.\n")
    add("Three arms, because two would conflate two different costs:\n")

    add("| arm | n | void | parent input (median) | spread | parent output | cache-read | wall s |")
    add("|---|---|---|---|---|---|---|---|")
    for arm, name, _ in ARMS:
        s = summary[arm]
        add(f"| **{arm} · {name}** | {s['n']} | {s['void']} | **{n(s['parent_in'])}** | "
            f"{spread(s['parent_in_spread'])} | {n(s['parent_out'])} | "
            f"{n(s['parent_cache_read'])} | {n(s['wall_s'])} |")
    add("")

    for arm, name, desc in ARMS:
        add(f"- **{arm} · {name}** — {desc}")
    add("")

    add("## The two numbers the three arms exist to separate\n")
    add("Each difference is reported with whether the arms' observed ranges actually "
        "separate. Three runs per arm supports no meaningful significance test, but "
        "overlapping ranges are enough to say a difference is not measurable -- and "
        "saying so is the entire reason the spread column exists.\n")

    toll_verdict = delta_verdict(summary, "A", "B")
    save_verdict = delta_verdict(summary, "B", "C")
    net_verdict = delta_verdict(summary, "A", "C")

    add(f"- **Registration toll (B - A): {n(delta['toll'])} parent input tokens** -- "
        f"*{toll_verdict}.*")
    if "variance" in toll_verdict:
        add("")
        add("  Registering the server was expected to COST the parent context: the tool "
            "schemas and `instructions.md` load on every turn. This measurement cannot see "
            "that cost at this sample size, and the sign came out negative. That is a "
            "result about the noise floor, not evidence that registration is free. "
            "**Do not quote it as a saving.**")
        add("")
    add(f"- **Delegation saving (B - C): {n(delta['saving'])} parent input tokens** -- "
        f"*{save_verdict}.*")
    add(f"- **Net (A - C): {n(delta['net'])} parent input tokens** -- *{net_verdict}.* "
        "A two-arm test would report only this number and could not say which half of it "
        "is registration and which is delegation.")
    if delta.get("parent_ratio"):
        add(f"- The delegating parent carried **{delta['parent_ratio']:.2f}x less input** "
            "than the solo parent.")
    add("")

    add("## What this cost in total tokens\n")
    add("| arm | parent input | worker input | total input |")
    add("|---|---|---|---|")
    for arm, name, _ in ARMS:
        s = summary[arm]
        add(f"| {arm} · {name} | {n(s['parent_in'])} | {n(s['worker_in'])} | "
            f"**{n(s['total_in'])}** |")
    add("")
    if delta.get("total_cost_ratio"):
        add(f"**Delegation cost {delta['total_cost_ratio']:.2f}x the total tokens of doing it "
            "alone.** That is the honest shape of the trade and it is not a footnote: four "
            "workers each re-read context the parent already held. The claim this project "
            "makes is about the **parent's context window**, which is what fills up and "
            "forces a compaction — not about total spend, which goes up.")
        add("")

    void_rows = [(a, summary[a]) for a, _, _ in ARMS if summary[a]["void"]]
    if void_rows:
        add("## Void runs\n")
        add("A run that did not do the work never enters a median — that is bench defect D1, "
            "where voided runs were quoted in a published table.\n")
        for arm, s in void_rows:
            add(f"- **{arm}**: {s['void']} void — {'; '.join(s['void_reasons'])}")
        add("")

    add("## What this cannot tell you\n")
    add(f"- **n = {min(summary[a]['n'] for a, _, _ in ARMS)} per arm.** Bench runs varied "
        "110k–305k input tokens per turn. Three runs cannot separate an effect smaller than "
        "the spread column, and the spread is printed for exactly that reason.")
    add("- **The arms are not prompt-identical.** A and B receive the same prompt; C's tells "
        "it to delegate. That asymmetry *is* the intervention, and it means C is measured "
        "doing something the others were never asked to do.")
    add("- **All three arms ran with `--dangerously-skip-permissions`.** Without it a headless "
        "parent cannot call an MCP tool at all (`NOTES.md` §31). File tools are auto-approved "
        "headlessly regardless, so the flag changes nothing for A and B — but no human "
        "approval gate was exercised in any of these runs, and that is not the configuration "
        "a person running this interactively would have.")
    add("- **One fixture, one language, one model** — `gemini-3.8-flash-low` over four Python "
        "modules. Inherited from `bench/RESULTS.md` §3 and still true.")
    add("- **Cache-read is reported separately** and never folded into input tokens.")
    add("")

    add("## Reproducing\n")
    add("```bash\npython bench/ab/run_ab.py --repeats 3   # spends tokens\n"
        "python bench/ab/report.py\n```\n")
    add("`--dry` swaps in `tests/fake_agy.py` and spends nothing; arm C correctly voids there, "
        "because the fake speaks no MCP.")
    return "\n".join(lines) + "\n"


def main() -> int:
    if not RUNS.is_file():
        print(f"no runs at {RUNS} -- run bench/ab/run_ab.py first")
        return 1
    rows = read_rows(RUNS)
    if not rows:
        print("runs.csv is empty")
        return 1
    OUT.write_text(build(rows), encoding="utf-8")
    print(f"written: {OUT} ({len(rows)} runs)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
