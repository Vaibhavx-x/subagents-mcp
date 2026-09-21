# Is the delegated work any good?

`bench/ab/` measured what delegation **costs**. It could not measure whether
the work was any good, and said so — `bench/RESULTS.md` §3: *"23 of 23 graded
runs passed. This bench ranks cost and cannot rank quality."*

This one scores the output. Results in [`RESULTS.md`](RESULTS.md).

```bash
python bench/quality/preflight_all.py --real   # THE GATE. ~15 workers.
python bench/quality/inject.py                 # build the fixtures + keys
python bench/quality/inject.py --check         # prove the keys match
python bench/quality/run_quality.py --repeats 1 --dry --out -   # free rehearsal
python bench/quality/run_quality.py --repeats 4                 # spends tokens
python bench/quality/report.py
```

## The gate comes first

`preflight_all.py` runs every check that already exists and exits non-zero on
any red: the offline suite, the stdio smoke test, a clean clone, the client
config and `agy` binary, cache prune and migration, the historical replay, the
fixture key, and a dry run of this harness. `--real` adds the opt-in suites,
the headless MCP probe, the taint demo and the cold/warm measurement.

Nothing is measured until it is green. Every check in it is something that
would otherwise fail silently halfway through a measurement and be discovered
once the tokens were already spent.

Four things no automation reaches — the approval prompt, a clean decline,
deadline recovery, and the client restart after a registration change — are
printed as a checklist the script explicitly refuses to count as passing.

## Two tasks, graded mechanically

Both run against four modules copied from this repo, and both demand a strict
output format so nothing is scored on prose. Grading prose is bench defect D2.

**Defect audit** (`fixture/audit/`) — 12 defects planted across three modules;
the fourth is **clean**, as a control. Scored on recall: a `LINE <n>:` finding
counts when it names a line within 2 of a planted one. **Line numbers only** —
never the description. Precision is reported beside recall and never folded
in, because an agent that lists every line scores perfect recall and a single
number would rank it best.

**Answer-key extraction** (`fixture/extract/`) — 12 questions whose answers are
exact strings in the code, or derivable from it. No question names the module
its answer is in. **Two are deliberately cross-module**: their evidence lives
in a module other than the one the question is about, which is where
delegating by file should hurt. A question set without any would be built to
flatter the tool.

### The keys are generated, never written

`inject.py` builds both fixtures and both keys from one table of edits, and
reads the resulting line numbers back **out of the file it just wrote** rather
than predicting them. `--check` validates the *committed* fixture — a fresh
build passing says nothing about what is on disk — and proves five things:

1. every recorded line really contains the injected text;
2. no two defects sit within 5 lines, so the ±2 tolerance cannot credit one
   finding to two defects;
3. the clean source did not already contain the "defect";
4. every injected module still parses;
5. every non-derived answer is on the line its evidence names, and every
   derived one records the arithmetic behind it.

It earned its keep on the first run, rejecting a cross-module question whose
answer lived in `config.py` — not in the fixture, and so unanswerable by
either arm.

## Void versus zero

The line the whole report rests on.

**Void** — *this run does not measure what we think it does*: wrong arm state,
a cache hit, `agy` failing outright, an arm-C run that did not delegate.

**Zero** — *this run measured badly*: no output, or output in the wrong format.
It stays in the median.

Voiding bad output would be the comfortable choice and the wrong one. If one
arm follows the output format worse than the other — plausible, since each
arm-C worker gets one simple file — then dropping those runs erases a real
finding about the tool. `parse_ok` is reported as a per-arm rate instead.

A timeout is likewise **recorded and scored, not voided**. Arm A doing four
modules serially is genuinely likelier to hit a wall than arm C doing them at
once, and that is a result rather than an inconvenience.

## The ceiling, stated up front

Calibration ran each task once, solo, with a rule set before the data: rewrite
anything scoring above 90% or below 30%, because a ceiling measures nothing.

It fired twice. The first round was my fault — the prompt listed the defect
categories, the questions named their own modules, and every module was known
to be broken. The rewrite removed all three. The second round still scored
100%, though the audit's cost went from 147k parent tokens to **411k** for the
same result.

I stopped there rather than tune a third time, which would have been tuning
until the task produced the answer I wanted. See `NOTES.md` §44.

What that leaves is honest and still useful: the ceiling is on **arm A, the
control**, so any drop in the delegating arm is measurable against it. And
`aggregate.ceiling_warning()` prints CEILING into `RESULTS.md` whenever both
arms land above 90%, so a comparison that measures nothing says so in the
report rather than in my head.

## What the numbers are not

- **`parent_in` is cumulative input across turns, not peak context.** Ten
  turns of 20k and two of 100k both total 200k, and only the second fills a
  window. `num_turns` is recorded so input-per-turn sits beside it as the
  closer proxy.
- **Total tokens go up.** The claim is about the parent's window, not about
  spending less.
- **No delta whose ranges overlap is reported as an effect** — reused from
  `bench/ab/aggregate.py` rather than reimplemented.
- **Arm C is measured under close-to-best-case delegation.** Its prompt
  carries the decomposition and tells it not to read the source itself; a
  parent meeting the server cold spends ~16 tool calls exploring first
  (`NOTES.md` §37).
