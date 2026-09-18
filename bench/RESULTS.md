# Worker model benchmark — results

Four model configurations × three tasks × two repeats, run 2026-09-18 against
`agy` CLI 1.2.6, plus an 8-run re-run of t1 after the harness audit. Raw logs in [`logs/`](logs/), one JSON document per run; the
joined table is [`results.csv`](results.csv).

**Read the caveats in §3 before quoting any number here.** This bench ranks
*cost*. It does not have the statistical power to rank *quality*.

---

## 1. The table

Two different samples, kept apart on purpose. **Cost** medians use every run that
completed — a `usage` block is a valid measurement whether or not `check.sh`
graded the run. **Pass rate** uses only runs that were actually graded.

| config | cost n | graded | pass | med wall s | med out tok | med thinking | med input tok | med cache-read |
|---|---|---|---|---|---|---|---|---|
| **A · gemini-3.8-flash-low** | 6 | 6 | 6/6 | **58.2** | **1,735** | **0** | 137,022 | 80,979 |
| D · gemini-3.7-flash-medium | 6 | 6 | 6/6 | 72.6 | 4,170 | 2,282 | 161,322 | 248,926 |
| B · gemini-3.8-flash-medium | 6 | 6 | 6/6 | 118.4 | 6,915 | 4,530 | 239,682 | 303,302 |
| C · gemini-3.8-flash-high | 6 | **5** | 5/5 | 120.0 | 9,850 | 6,452 | 263,508 | 442,943 |

`med wall s` is externally measured and **includes ~9.7 s of CLI startup**. The
earlier published table used agy's internal `duration_seconds`, which excludes
it. Config C shows 5 graded rather than 6 because defect D2 voided one run's
grade while its tokens were still quoted; its cost figures are unaffected.

### The multi-file case, recovered

t1 was void under defect D1 and has now been re-run on the fixed harness:

| config | n | pass | med wall s | med out tok | med thinking |
|---|---|---|---|---|---|
| **A · 3.8 low** | 2 | 2/2 | **58** | **1,735** | **0** |
| D · 3.7 medium | 2 | 2/2 | 78 | 3,521 | 1,080 |
| C · 3.8 high | 2 | 2/2 | 107 | 4,863 | 2,110 |
| B · 3.8 medium | 2 | 2/2 | 128 | 4,645 | 1,898 |

**The ordering holds on the one task shape that was previously unmeasured.** A
renamed a symbol across four files, with every call site and docstring updated,
spending zero thinking tokens — and was the fastest configuration doing it. This
is the shape a fan-out worker actually performs, so it is the result that most
needed to exist.

Note C beats B here (107 s vs 128 s) — the only place the effort ladder is not
monotonic, and a reminder that n = 2 medians are soft.

### Decision

- **Default: `gemini-3.8-flash-low`.** It matched every other configuration's
  result while spending ~2.3× less wall-clock and ~6.5× fewer output tokens than
  3.8 high.
- **Escalate to `gemini-3.7-flash-medium` on worker failure or rate limit.**
  Second on both axes.
- **3.8 medium and 3.8 high are not used.** Both are dominated on both axes.

### The finding worth stating out loud

**More reasoning effort bought nothing on this workload.** Answer text is roughly
constant whatever the configuration; the entire effort dial buys *thinking*, and
thinking produced **zero additional correct answers**. Config A spent **0
thinking tokens on all four of its runs** and matched everything. The most
expensive single run — C/t2/rep1 — burned 15,851 output tokens, 13,661 of them
thinking, for a 20-line function.

This contradicts the prior the original role/effort matrix was built on. The
matrix is gone; there is one default config.

## 2. The input-token finding — the project's thesis

Across all 33 runs:

| | min | max |
|---|---|---|
| **input tokens / turn** | **110,323** | **305,321** |
| output tokens / turn | 988 | 15,851 |
| cache-read tokens | 12,145 | 739,928 |

Per-turn *input* exceeds output by one to two orders of magnitude in every
single run. **Context, not generation, is the dominant token cost.** That is the
orchestrator's entire argument, stated in measured form.

Two consequences for the demo:

1. **The headline metric is parent input tokens** — not output, not wall-clock.
   The claim is that a parent doing five sub-tasks itself accumulates context
   linearly, while a parent delegating them carries five summaries.
2. **Report cache-read separately.** It ranges 12k–740k, and cached input is
   cheaper than fresh input. Folding it into one number invites the correct
   objection that cached context was compared against fresh.

## 3. What this bench cannot tell you

- **Ceiling effect — the big one.** **23 of 23 graded runs passed**, across all
  four configurations and all three tasks. All eight t3 runs identified the root
  cause on their first turn. The bench therefore has **no power to separate
  these configurations on quality**; it ranks cost, and nothing else. A harder
  task set is the single most valuable improvement available.
- **n = 2.** A single flaky run moves a median by ~30%. C beating B on t1 is
  probably noise, not signal. Repeats should be 5.
- **Config C has 5 graded runs, not 6**, because of D2. Its cost medians are
  unaffected — the voided run completed and its `usage` block is valid.
- **No failure modes were observed at all.** All 33 logs are `status: SUCCESS`.
  There was never a rate limit or a timeout. **The escalation ladder is designed
  but entirely unexercised**, and the fallback path is therefore untested code.
- **One fixture, one language.** Everything here is a small synthetic Python
  package. Nothing generalises to large repos or other languages.

## 4. Harness defects found by audit

All six are fixed. The table above is computed post-fix from the original logs;
t1 was additionally **re-run on the fixed harness**, so its column is real data
rather than a recomputation.

| # | Defect | Severity | Fix |
|---|---|---|---|
| D1 | `setup.sh` ran `git add -A` with no `.gitignore`, committing `__pycache__/*.pyc` compiled from pre-rename source. `git checkout` restored them before every run, and t1's recursive grep matched `fetch_cfg` inside bytecode. t1 graded "did the agent run Python", not "was the rename correct". | blocking | `.gitignore` committed as part of the baseline; bytecode untracked; **both** of t1's greps filtered to `--include='*.py'`; reset changed to `git clean -qfdx`, since ignored files survive a bare `-fd`. `setup.sh` now hard-fails if bytecode is ever tracked again. |
| D2 | The rate-limit classifier grepped the whole log for `429`. A `conversation_id` containing `429` voided a passing run; the same grep also matches the model's own prose. | blocking | Classification reads the JSON `status` field via `parse_log.py`. |
| D3 | `run_all.sh` wrote the CSV header only when the file was absent; it already existed without one, so `DictReader` consumed row 1 as the header. **`report.py` had never once run successfully.** | blocking | Header presence is checked, not file existence. `report.py` now runs. |
| D4 | t3's prompt demanded the whole suite pass; its check runs only two files. Since `parse_duration` does not exist at baseline, `test_parser.py` fails by construction — so the prompt asked for t2's work as well. Three runs passed without doing it; five did the extra work for no credit. | design | t3's prompt narrowed to match its check. Widening the check instead would have made t3 a superset of t2 and confounded the two. |
| D5 | `log_bytes` was the cost proxy. It correlates with real cost at **r = 0.13** and inverts A and D. | minor | Real `input/output/thinking/cache_read` tokens recorded as columns; `log_bytes` retired. |
| D6 | Two rows shared `B/t1/repeat=1` — a manual smoke run three minutes before the batch, giving B a 7-run sample. | minor | Retained as evidence, marked `superseded_smoke`, excluded from aggregates. |

### Status values in `results.csv`

`ok` / `fail` are gradeable. Everything else is excluded from aggregates:
`void_d1` (t1, ungradeable), `regraded_d2` (the false rate-limit; completed
successfully but its fixture state is gone, so it cannot be re-graded without a
re-run), `superseded_smoke` (D6).

## 5. Outstanding

**t1 has been re-run and the multi-file column is recovered** — the gap that
blocked a complete table is closed.

What would still improve this, in order of value:

1. **A harder task set.** 23/23 passing means the bench cannot rank quality at
   all. Until some configuration fails something, "A matches the others" is a
   statement about the tasks, not the models.
2. **Repeats from 2 to 5** (~1 hour) — the cheapest way to firm up the medians.
3. **Exercise a failure.** Nothing has ever rate-limited or timed out here, so
   the escalation ladder has never run.

## 6. Reproducing

```bash
cd bench
./setup.sh                       # builds fixture_repo/ from fixture_src/
TASKS="t1" ./run_all.sh 5        # re-run one task at 5 repeats
python report.py                 # print the tables
```
