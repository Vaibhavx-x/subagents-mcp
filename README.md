# subagents-mcp

An MCP server that lets a parent agent delegate sub-tasks to ephemeral `agy`
subprocesses running in parallel, so the parent's context stays clean.

The parent splits a task and calls `propose_plan`. The server validates it —
paths, tiers, read/write conflicts — and returns a readable plan plus an id and a
digest. The human approves via the client's own tool-permission prompt. The
parent calls `execute_plan`. Workers run as separate processes, each writing its
full output to SQLite the moment it finishes and returning only a short summary
and a handle. `collect` reads results back, **including after a timeout**.

> **Status: Phase 2 complete.** All three tools work end to end against the
> real client and a real worker. Workers run **one at a time** -- parallelism
> within a wave, taint hashing and the escalation ladder are Phase 3.

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

All three tools, end to end against a real worker. Workers run **one at a
time**; parallelism within a wave is Phase 3.

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
anything spawns**, then runs the workers in wave order. Each worker is killed
as a process **tree** at its deadline (a plain kill leaves grandchildren
running on Windows), and its result is written the moment it finishes.

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
python -m pytest                    # 221 tests, no API calls
python tests/smoke_stdio.py         # real subprocess over stdio; exit 0 = clean

SUBAGENTS_REAL_AGY=1 python -m pytest tests/test_real_worker.py   # spends tokens
```

Almost everything runs against `tests/fake_worker.py`, a stand-in that emits
controllable output, so the worker lifecycle is covered without API calls. The
opt-in suite is the part a fake cannot check: the real command shape, the real
output schema, and whether a worker actually changes the file.

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
hashing declared paths before and after each run — whether what actually changed
matches what was declared.

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
