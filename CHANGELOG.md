# Changelog

What each phase delivered, and what it measured. Every number here is from
this repo's own runs; the corrections behind them are in `NOTES.md`.

## v1.0 — Phase 5, ship

**Task reuse across plans.** A plan cancelled at the client deadline, or
tainted and re-declared, comes back under a *new plan id*, so the existing
within-plan skip could not help and every finished task was spawned again.
`subagents/cache.py` keys a finished task on its workspace, instruction,
model, declared paths and the sha256 of every declared **read**, and serves it
only while every declared **write** still hashes to what that run left behind.
A hit asserts the outputs are already in place; it never replays them.

Never served for a failed run, a tainted run, a task declaring no reads, or an
entry past `SUBAGENTS_CACHE_TTL_S` (default 3600, `0` disables). The TTL bounds
the one thing the check cannot cover: an effect *outside* the declared writes.

**Measured:** the retrospective hit rate over this install's own history, and
the cold-versus-warm re-propose, in `bench/cache/RESULTS.md`. The first number
is zero, and it is reported first.

**`file_hashes` now has rows in it.** The table had been in the schema since
Phase 1 and nothing ever wrote to it, so `tainted_paths` could say a path
changed and never what it changed *from* (`NOTES.md` §38).

**The test suite had been writing to the production database.** 82 of the 104
plans on record were `tests/smoke_stdio.py`. The Phase 4 worktree finding was
published as "zero conflicts across 87 plans"; the honest population is the 14
plans that ran workers (`NOTES.md` §39).

**Ship items.** `--check-config` resolves and runs `agy`; `--prune-cache`;
the platform claim narrowed to what is actually tested.

## v0.4 — Phase 4, measurement

Three arms on the same four-module task, 15 runs, 0 void: solo **99,582**
parent input tokens, registered-but-unused 83,782, delegating **62,551**.
**1.59× less parent context** with non-overlapping ranges — at **2.34× the
total tokens**, because four workers each re-read context the parent had.

Two deltas reported as *not measurable* rather than as results: the
registration toll came out negative (§32), and the delegation saving overlaps
at this sample size. Going from three repeats per arm to five moved the
headline down 23% and collapsed one delta into noise (§35).

The estimator now reads this install's own finished runs and takes their
**p90** (29 s, against a hard-coded 60), and states its provenance. Plus the
scripted out-of-scope demo and a clean-clone install check.

## v0.3 — Phase 3, parallelism and detection

Workers within a wave run concurrently under a ceiling: the same three-task
plan took **12.2 s** against **33.3 s** serialised — 2.73×, because `agy`
charges ~9.7 s of process startup per worker and running them together pays it
once.

Every run is hashed before and after: sha256 for declared paths, a
`(size, mtime_ns)` manifest for the rest of the tree. A real worker told to
write a file it had not declared **was detected**, and the verdict reached the
database. Nothing prevented the write — there is no filesystem containment —
so noticing afterwards is the entire control.

Also: a failed worker retried once on a stronger model, its dependants blocked
rather than fed stale state, rate limits waited out rather than escalated, and
the whole process tree killed at a deadline.

## v0.2 — Phase 2, one worker end to end

`propose_plan → execute_plan → collect` against the real client, verified
across a declined approval (nothing spawned, no rows), an approved plan, and a
worker cut off at its deadline with its partial output kept.

The finding that shaped everything after it: `agy` exits 0 with
`{"status":"SUCCESS","response":"","usage":{"input_tokens":164283}}` — real
usage, real duration, no answer. A worker is never trusted to report its own
success (§18).

## v0.1 — Phase 1, validation

Plan validation, path resolution against the workspace root after symlink
resolution, a rules-table tier classifier, write–write refusal, wave
scheduling with cycle detection, the plan digest, and the wall-clock estimate
checked against the deadline read from the client's own config.
