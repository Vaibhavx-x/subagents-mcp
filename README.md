# subagents-mcp

An MCP server that lets a parent agent delegate sub-tasks to ephemeral `agy`
subprocesses running in parallel, so the parent's context stays clean.

The parent splits a task and calls `propose_plan`. The server validates it —
paths, tiers, read/write conflicts — and returns a readable plan plus an id and a
digest. The human approves via the client's own tool-permission prompt. The
parent calls `execute_plan`. Workers run as separate processes, each writing its
full output to SQLite the moment it finishes and returning only a short summary
and a handle. `collect` reads results back, **including after a timeout**.

> **Status: Phase 3 complete.** Workers in a wave run **concurrently** under a
> ceiling; every run is hashed before and after and reports what changed
> against what was declared; a failed worker is retried once on a stronger
> model and its dependants are blocked rather than fed stale state. Remaining:
> the tool-call cache and git worktrees, both deliberately cut.

---

## Why

Measured across 33 benchmark runs (`bench/RESULTS.md`):

| | min | max |
|---|---|---|
| **input tokens / turn** | **110,323** | **305,321** |
| output tokens / turn | 988 | 15,851 |

Per-turn input exceeds output by one to two orders of magnitude in every run.
**Context, not generation, is the dominant token cost.** A parent that performs
five sub-tasks itself accumulates all five tool transcripts; a parent that
delegates them carries five summaries.

## What works today

All three tools, end to end against a real worker, with the workers in a wave
running at the same time.

**Measured, six real workers on the same three-task plan:**

| `max_parallel` | wall clock |
|---|---|
| 4 | **12.2 s** |
| 1 | 33.3 s |

**2.73x**, and the mechanism is worth stating because it bounds the benefit:
`agy` costs ~9.7 s of process startup per worker (measured over 33 benchmark
runs), so three sequential workers pay that toll three times. Running them
together pays it once. Token usage was within noise of identical — 76.6k
against 68.4k input — because scheduling does not change how much work there
is, only how long you wait for it.

**`propose_plan(tasks_json, workspace_root)`** — validates a set of sub-tasks
and returns an executable plan. Read-only; spawns nothing.

It resolves every declared path and proves it lands inside the workspace
(after symlink resolution, and against the workspace root rather than the
process CWD), classifies each task's action tier from a rules table, refuses
plans where two tasks write the same file or whose read/write dependencies
form a cycle, groups the rest into waves — writers before readers — and
checks wall-clock against the deadline read from your client config.

**`execute_plan(affects, plan_id, plan_digest)`** — recomputes the digest
from the stored plan and refuses a stale, swapped or expired one **before
anything spawns**, then runs the plan wave by wave. Within a wave the workers
run concurrently under `max_parallel`; between waves nothing overlaps, because
a task that reads what another writes must not start until that writer has
exited. Each worker is killed as a process **tree** at its deadline (a plain
kill leaves grandchildren running on Windows), and its result is written the
moment it finishes.

A worker whose dependency failed is **blocked**, never spawned — it would read
state its writer never produced and have no way to tell, and a confident wrong
answer is worse than a failure. A worker that fails is retried once on the
escalation model; a worker that *timed out* is not, because it needed more time
rather than more reasoning; a rate-limited one is waited out rather than
answered with a more expensive request at the same quota.

`affects` carries the human-readable scope. It is named for the approval
prompt rather than for the code: the prompt truncates arguments after ~40
characters and shows them **alphabetically**, so the name has to sort ahead of
`plan_digest` and `plan_id` to be seen at all.

**`collect(plan_id)`** — reads results back, including after a cancellation.
Returns summaries and token usage, never full transcripts.

A worker is never trusted to report its own success: agy exits 0 with
`status: SUCCESS` both when its own timeout fires mid-turn and when a tool is
auto-denied, in each case having done nothing. A worker that produced no answer
is recorded as a failure, with the reason.

That is not theoretical. A real worker cut off at a 30-second budget returned
`{"status":"SUCCESS","response":"","usage":{"input_tokens":164283,…}}` — real
usage, real duration, no answer. It is recorded as `timeout — agy hit its own
--print-timeout mid-turn`, its partial output kept, and `collect` returns it.

```
PLAN b42ddb6e9ebb
  workspace_root : D:\Projects\subagents-mcp
  plan_digest    : cf50f7f9cbe1b7dc...
  tiers          : auto=1, gated=1, never=0
  schedule       : 2 worker(s) in 1 wave(s), max_parallel=4
  estimate       : ~70s expected, ~610s worst case
```

### Running the tests

```bash
python -m pytest                    # 300 tests, no API calls
python tests/smoke_stdio.py         # real subprocess over stdio; exit 0 = clean

SUBAGENTS_REAL_AGY=1 python -m pytest tests/test_real_worker.py   # spends tokens
```

Almost everything runs against `tests/fake_worker.py`, a stand-in that emits
controllable output, so the worker lifecycle is covered without API calls. The
opt-in suite is the part a fake cannot check: the real command shape, the real
output schema, whether a worker actually changes the file, whether a real wave
genuinely overlaps, and — the one that matters most — whether a real agent
writing outside its declaration is actually detected.

Two of them are about what happens when things are killed or collide: a
hard-killed server must take its workers' whole process tree with it (Windows
only -- the POSIX fallback offers no such guarantee, and the test says so
rather than pretending), and concurrent writers must all land, since a dropped
result would not raise, it would silently shorten `collect`.

The suite is built around the invariants later phases rest on rather than
around coverage: the digest is recomputed in subprocesses with differing
`PYTHONHASHSEED` (an unsorted set would be invisible in-process), a foreign-key
violation is forced rather than the pragma merely read back, and the stdio
smoke test is itself tested by injecting `print()` into a copy of the server
and requiring the smoke run to fail.

## Required client configuration

`agy` cancels every MCP tool call at **180 s** by default. That limit appears in
no documentation — it was found by binary search and is described in
`NOTES.md`. A real fan-out exceeds it, so a default install will see a plan
cancelled mid-flight.

Register the server in `~/.gemini/config/mcp_config.json`:

```json
{
  "mcpServers": {
    "subagents": {
      "command": "C:/Python313/python.exe",
      "args": ["D:/Projects/subagents-mcp/server.py"],
      "cwd": "D:/Projects/subagents-mcp",
      "timeoutSeconds": 900,
      "forceAllToolsEager": true
    }
  }
}
```

- **`timeoutSeconds: 900` is not optional.** Without it, calls die at 180 s.
  `propose_plan` warns when its own estimate exceeds 180 s, and `collect`
  recovers finished work after a cancellation — but that is graceful
  degradation, not the intended path.
- **Absolute interpreter path.** `python` is ambiguous on a machine with more
  than one Python; only the one with `mcp` installed will work.
- **`forceAllToolsEager: true`.** If tools can be deferred, the agent may decide
  to do the work itself before the orchestrator is even in context. Being
  invisible at decision time is the failure that matters — but note that this
  makes the tool *choosable*, not *preferred*: asked plainly for a task, the
  parent still does it itself. Measured, `NOTES.md` §22. Ask for delegation, or
  the orchestrator sits there unused.
- **`cwd` pinned.** It otherwise defaults to the workspace root, which changes
  where the SQLite file lands.

`agy mcp add` cannot set `timeoutSeconds`, `cwd`, or `toolConfig` — it has no
flags for them. These are hand-edits to the JSON.

### Check it actually took effect

The server reads the client's config and reports the deadline that will really
apply, so you never have to guess:

```bash
python server.py --check-config     # exit 0 = configured adequately
python server.py --fix-config       # set timeoutSeconds to 900 (backs the file up)
```

`--fix-config` is deliberate and never automatic. The client reads its config
when it *spawns* the server, so a server that edited the file mid-session could
not raise its own deadline for the session that noticed the problem — and it
would be silently editing a shared file that other MCP servers depend on.

Because the deadline is read rather than assumed, `propose_plan` states it as a
fact and warns only when the plan genuinely will not fit:

```
  estimate       : ~70s expected, ~610s worst case
  client deadline : 900s (timeoutSeconds in ~/.gemini/config/mcp_config.json)
```

This matters more than the estimate. A worker's duration is not predictable —
it may call other MCP servers, install packages, or run a test suite — so the
plan compares its worst case (the per-worker timeout, a real ceiling) against
the configured deadline (a real number) rather than betting on a median.

> The config parser is **lenient**: unknown keys and misspelled enum values are
> accepted silently rather than rejected. A typo here does not produce an error,
> it produces a setting that never takes effect. Verify behaviour, not syntax.

## Where the approval gate is

**The approval gate is the client's tool-permission prompt, which fires before
every MCP tool call.** Running *your* session with
`--dangerously-skip-permissions` disables it.

> Not to be confused with the workers: they are spawned with that flag, because
> a worker has no human attached and headless mode otherwise auto-**denies** the
> `command` tool — silently, while still reporting success. That does not touch
> your session's prompt, which is where the approval actually happens.

It is not elicitation. `agy` advertises elicitation support, drives the round
trip correctly, and then auto-cancels every request in 6–16 ms without rendering
anything — see `NOTES.md` §2. That is why there are three tools rather than one,
and why `execute_plan` takes a human-readable `affects` argument: so the permission
prompt shows intent rather than an opaque id.

**What the code enforces:** which tasks are allowed into a plan, what each worker
is instructed to touch, digest validation against the approved plan, and — by
hashing before and after each run — whether what actually changed matches what
was declared.

That hashing works at two levels. Declared paths get sha256, so both sides of
the comparison are real content. Everything else under the workspace root gets
a `(size, mtime_ns)` manifest, which catches the realistic failure — an
undeclared write *inside* the root — cheaply enough to run every time. The
manifest can over-report: a byte-identical rewrite still moves mtime. That is
the right direction for a detector.

Attribution is stated rather than implied. Write sets are provably disjoint, so
a declared path is attributable to one task; an **undeclared** change with
several workers running at once is attributable only to the wave, and the
report says "in this wave" rather than naming a worker it cannot identify.

It also cannot tell *which process* wrote a file — only that it changed. An
editor autosaving, a watcher rebuilding, or a second MCP server appending to
its log inside the workspace all look exactly like a worker, and the first real
fan-out proved it by flagging another server's log against every task
(`NOTES.md` §28). Logs and scratch files are excluded by default;
`SUBAGENTS_TAINT_IGNORE` takes extra globs for whatever your project
generates.

**What it does not: there is no filesystem containment.** Measured against real
`agy` with a canary file outside the workspace:

| Configuration | Read outside `--add-dir` | Write outside `--add-dir` |
|---|---|---|
| `--add-dir` + `--dangerously-skip-permissions` | succeeded | succeeded |
| `--add-dir` + `--sandbox` | succeeded | succeeded |
| `--add-dir`, no skip flag | — | succeeded |

`--add-dir` is *additive scope*, not a boundary — it tells the agent what to
look at and refuses nothing. `--sandbox` restricts **terminal commands only**.
And `--print` mode auto-approves tool calls regardless of
`--dangerously-skip-permissions`, so a worker has no permission gate at all.

An earlier version of this README claimed these flags constrained the worker
and that we were "trusting agy's enforcement". There is no enforcement to
trust, and an overclaimed boundary is worse than a stated absence of one.

So the real control points are: **which tasks enter a plan**, **the human
approval at `execute_plan`** — the only gate in the system — and **post-run
hashing**, which detects changes only to paths we thought to hash.

> **The approval prompt truncates arguments after ~40 characters, in
> alphabetical order** — not the order they are declared in. The client caches
> our schema with `properties` sorted, and the model emits arguments in that
> order, so with a parameter named `scope_summary` the prompt opened
> `{"plan_digest":"30bccf8b20d6f83a...` and the human approved a hash. The
> argument is therefore named `affects`: short, and it sorts first. See
> `NOTES.md` §20.
>
> Note also that the prompt offers "always allow … (Persist to settings.json)".
> Choosing that permanently removes the only human gate.
>
> **After changing tool registration, restart the client.** It keeps the server
> process alive across chat sessions while refreshing its tool-schema cache
> separately, so a newly added tool can be visible in the cache and still
> return `Unknown tool` from the running process (`NOTES.md` §21).

## Layout

| Path | What |
|---|---|
| `probe/` | Capability probe against the real client, with `RESULTS.md` and `probe.log`. Evidence — kept. |
| `bench/` | Worker model benchmark, with `RESULTS.md` and the audited harness. |
| `NOTES.md` | Every assumption that turned out wrong, and what overturned it. |
| `CLAUDE.md` | The v2-only SDK rules and design invariants the build must hold. |

## Environment

Windows 11, Python 3.13, `mcp` 2.2.0, `agy` CLI 1.2.6, MCP protocol
`2026-07-28`. SDK **v2** — see `CLAUDE.md`, the v1 API is a trap here.

Copy `.env.example` to `.env` and edit before running anything.
