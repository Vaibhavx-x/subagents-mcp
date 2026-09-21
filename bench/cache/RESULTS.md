# Task reuse: what it saves, and how often it applies

Two numbers. The small one is first, because it is the one that decides
whether the feature was worth building.

Reproduce:

```bash
python bench/cache/replay.py     # the historical bound; free
python bench/cache/measure.py    # cold against warm; ONE worker
SUBAGENTS_REAL_AGY=1 python -m pytest tests/test_real_cache.py   # 5 workers
```

---

## 1. How often it would have applied: at most 2 of 33 runs

`replay.py` walks this install's entire run history oldest-first and asks, for
each run, whether an earlier one had already done that work.

```
runs     : 33
ttl      : 3600s

UPPER BOUND on the historical hit rate
  2 of 33 eligible run(s) -- at most 6%
    long-audit (23m after its predecessor)
    hello (2m after its predecessor)

why the rest could not have hit
    31  cold no predecessor
```

**It is a bound, not a rate.** A real hit needs three things: matching
identity, every declared read hashing the same, and every declared write still
in the state that run left it. Only the first is answerable from history,
because `file_hashes` was a table nothing wrote to until this phase
(`NOTES.md` §38). So a counted run *might* have hit; a run not counted could
not have. Zero would have meant zero.

**The prediction was zero and it was wrong**, which is why it was written down
first. The Phase 5 plan recorded "0 of 33, because every A/B run used a fresh
workspace" and said that a non-zero answer would be chased. It came out at 2,
and the reason is the interesting part: **all 20 A/B worker runs were cold
exactly as predicted** — each materialises its own workspace directory and the
key covers the absolute root. The two candidates are both ad-hoc runs from
Phases 2–4, and both are precisely the loop this was built for.

### The one worth reading

| | run 1 | run 2, 23 minutes later |
|---|---|---|
| status | `ok` | **`timeout`** |
| input tokens | 219,870 | **164,283** |
| result | wrote `scratch/audit.md` | nothing |

The same audit, the same three declared reads, the same workspace. `git log`
shows nothing committed to those files between the two runs, so the inputs
were almost certainly identical — "almost", because uncommitted working-tree
edits in that window are not reconstructable now, and that gap is why this is
a bound.

That second run is the transcript in `NOTES.md` §18: `status: SUCCESS`, empty
response, real token usage, no answer. **164,283 input tokens spent
re-attempting work that was already complete, and it still returned nothing.**

That is one anecdote, not a rate. But it is this project's own history, it was
not constructed to make the case, and it is the exact shape the feature
addresses.

---

## 2. What it saves when it does apply: the entire run

`measure.py`, two tasks, one real worker each, then the identical tasks
re-proposed under a new plan id:

| | wall clock | workers | input tokens |
|---|---|---|---|
| cold | **25.7 s** | 2 | **77,768** |
| warm | **0.1 s** | 0 | **0** |

There is no interesting ratio here. The warm run does nothing, so the saving
is the whole cold run. Reporting "273×" would dress up a tautology.

**No repeats, deliberately.** The warm side spawns no process and makes no
network call, so it has no variance to measure; repeating it would produce a
tighter interval around a structurally-zero number, which is the kind of rigour
that only looks like rigour. The cold side's spread is already measured, over
20 real runs, in the p90 the estimator uses.

---

## 3. What a hit does and does not assert

**Asserts:** the same instruction, model, workspace and declared paths; every
declared read hashing exactly as it did when the recorded run started; and
every declared write still hashing to what that run left behind. A hit does
not replay anything — it says the outputs are already there. Delete one and
the task runs. Verified against a live worker, not only a fake
(`tests/test_real_cache.py`): change one declared read and exactly that task
runs again; delete the output and it is rebuilt.

**Asserts nothing about** an effect outside the declared writes — a test suite
run, a package installed, a service restarted. Nothing records those, so
nothing can verify them, and a hit skips them silently. That is the one
remaining failure mode and `SUBAGENTS_CACHE_TTL_S` (default 3600) exists to
bound it rather than to tune performance.

**Never served** for a failed run, a tainted run, a task declaring no reads, or
an entry past its TTL. Each refusal has its own test in `tests/test_cache.py`,
because a cache is the easiest place in this system to return a confident
answer without doing anything.

---

## 4. What this does not change

**The Phase 4 headline is unaffected, and must stay that way.** That benchmark
is entirely first-time work: every arm materialises a fresh workspace, so
every run is a structural miss. A warm cache during an A/B run would not have
improved the result — it would have replaced it with a measurement of
something else. `bench/ab/harness.py` now voids any run with a cache hit and
names the count, and `tests/test_ab_harness.py` pins it. `demo_taint.py` sets
the TTL to zero outright, because its step 3 would otherwise be served from
cache on a second invocation while still printing "2 workers".

**It does not reduce total tokens for new work.** Delegation still costs 2.34×
the total tokens (`bench/ab/RESULTS.md`). This changes nothing about the first
run of anything; it only stops the second one.

**Scope of all of the above:** one machine, one model, one project's history,
33 runs. The historical bound in particular is a statement about how *I* have
used this tool, not about how anyone else would.
