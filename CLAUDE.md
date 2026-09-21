# CLAUDE.md — rules for working in this repo

Every constraint here was **measured against the real client**, not assumed.
If you are about to contradict one, re-measure first and write the result into
`NOTES.md`. Do not quietly "fix" one of these back to the obvious answer.

**Environment:** Windows 11, Git Bash, Python 3.13 (`C:\Python313\python.exe`),
`mcp` 2.2.0, `agy` CLI 1.2.7, MCP protocol `2026-07-28`.
(`probe/` and `bench/` say 1.2.6 and should stay that way -- those numbers
were measured on it.)

---

## 1. SDK v2 only — the single most likely error

Training data is saturated with v1. Every one of these is wrong here:

| Never write | Correct |
|---|---|
| `from mcp.server.fastmcp import FastMCP` | `from mcp.server import MCPServer` |
| `FastMCP(...)` | `MCPServer(...)` |
| `get_context()` | declare a `ctx: Context` parameter |
| `ctx.fastmcp` | `ctx.mcp_server` |
| `ctx.elicit(...)` | not used — see §3 |
| `ctx.info/warning/error/debug/log` | **deprecated** (SEP-2577, protocol 2026-07-28) — use the file logger. `ctx.report_progress` is NOT deprecated |
| `ctx.session.create_message(...)` | not used (sampling deprecated) |
| `McpError(ErrorData(...))` | `MCPError(code, message)` |
| `FastMCPError` | `MCPServerError` |
| `tool.inputSchema`, `result.isError` | `tool.input_schema`, `result.is_error` |
| `MCPServer("x", port=...)` | transport args go on `run()` |
| `roots/list`, `list_changed` | not used — `workspace_root` is a parameter |

`client_params` and `meta` do **not** exist on `Context` in 2.2.0.

Verify against the installed package rather than guessing:

```bash
/c/Python313/python.exe -c "import mcp.server.mcpserver as m; print([n for n in dir(m) if not n.startswith('_')])"
```

**Every v1-SDK slip that happens goes into `NOTES.md`.** That log is a
deliverable, not housekeeping.

## 2. `print()` is a protocol violation

stdout **is** the MCP transport. While serving, the SDK best-effort diverts
*flushed* stray stdout to stderr — but import-time output, buffered output
draining at interpreter exit, and anything a wrapper script emits still land on
the protocol stream. One junk line makes the host drop the connection, which
some hosts render as "a server with zero tools".

Use `logging` to a file and to stderr. Never `print()`.

## 3. Elicitation is unusable on this client — do not design around it

`agy` advertises `ElicitationCapability`, drives the round trip correctly, then
**auto-cancels every request in 6–16 ms without rendering anything.** No config
knob exists; the cause is an unregistered per-surface callback.

The approval gate is therefore **agy's own tool-permission prompt**, which is
why there are three tools rather than one. `--dangerously-skip-permissions`
disables that prompt; say so plainly in the README rather than overclaiming.

If an elicitation schema is ever written anyway, `T` in `ElicitationResult[T]`
must be a **single bool or single enum** — agy's proto has only
`confirmation_request` and `multiple_choice_request`, no general form.

## 4. Safety lives in code, never in a prompt

Tier classification is a **rules table**. Reject, every time, any suggestion to
"instruct the worker not to delete files". A prompt is not an enforcement
mechanism.

| Model decides | Code decides |
|---|---|
| How to split the task | Spawning, killing, timeouts |
| Each worker's instruction | Path validation |
| `task_ref` names | Tier classification |
| — | Conflict detection, wave scheduling |
| — | Digest validation, taint verification |
| — | All database writes |

**Honest limitation, to be stated and not dressed up:** the server does not
mediate worker file access. A worker is an `agy` process with its own tools.
`--add-dir` and `--sandbox` are *agy's* enforcement, not ours. What our code
enforces is which tasks enter a plan, what each worker is told to touch, and —
by post-run hashing — whether what changed matches what was declared.

## 5. Concurrency invariants

- `write ∩ write` between two tasks → **refuse the plan.**
- `A.reads ∩ B.writes` → **serialize into separate waves** (topological sort;
  a cycle refuses the plan and names it).
- Hash every declared read and write path **before and after** each worker.
  Hash *after the process has fully exited*, not when it reports done.
- Attribution is **wave-level** for undeclared changes whenever more than one
  worker ran. Declared paths are attributable per task (write sets are disjoint
  by construction); an undeclared change with four workers running is not. The
  report must say which, and never imply a precision it does not have.
- The ignore list is load-bearing, not tidiness: the server writes
  `subagents.db` (plus WAL sidecars) and `subagents.log` **inside** the
  workspace root while the run is happening. Counting those marks every run
  tainted, and a detector that always fires is one nobody reads.
- A task whose dependency failed is **blocked, never spawned.** It would read
  state its writer never produced and could not tell -- a confident wrong
  answer is worse than a failure.
- **Never retry a timeout** (it needed time, not reasoning), a tainted run (a
  second run compounds the undeclared change), a spawn error (no model fixes a
  missing binary), or an exhausted rate limit (that answers a quota refusal
  with a more expensive request -- measured, `NOTES.md` section 26).
- A task declaring an **empty read set** must be flagged at validation — a taint
  check over nothing passes silently and yields false confidence.

## 6. Async and subprocess invariants

All of these were measured in Phase 2. Each one has a regression test, and each
describes a bug that the obvious implementation has.

- No blocking call inside an `async def` handler. (v2 runs sync `def` handlers
  on a worker thread, so this applies to the orchestrator specifically.)
- **Never `asyncio.wait_for(proc.communicate(), t)`.** On timeout it DISCARDS
  partial output — a child that flushed real work before hanging yields `b''`.
  Drain both streams with reader tasks that own their buffers.
- **Never `proc.wait()` to detect exit.** It completes on **pipe EOF, not
  process exit**, and a grandchild inherits the pipes — so a worker that starts
  any background process blocks until the timeout and is recorded as a timeout
  despite having succeeded. Poll process liveness (`subagents/worker.py`).
- **Kill the process TREE, not the process.** `proc.kill()` leaves
  grandchildren running on Windows (measured). Use `subagents/jobobject.py`; a
  failed attach must raise, never degrade silently.
- Write each worker's result to SQLite **as it lands**, never at the end.
  Writing at the end silently defeats `collect`, and only shows up on an overrun.
- Wrap per-worker result writes in `asyncio.shield`.

## 6b. Never trust a worker's self-report

`agy` exits **0** with `"status": "SUCCESS"` and real token usage in at least
two cases where it did nothing:

- its own `--print-timeout` fires mid-turn (banner on **stderr**);
- a tool was auto-denied in headless mode (`denied_actions` in the JSON).

Both leave `response` empty. **A worker that produced no answer did not
succeed.** Classify on the status field plus evidence of work — never by
grepping the transcript, which is bench defect D2.

Workers are spawned with `--dangerously-skip-permissions`: headless mode
auto-**denies** the `command` tool without it, so a worker asked to run tests is
silently blocked. A worker has no human to prompt; a denial there is a failure
mode, not a safeguard.

## 6c. There is no filesystem containment — do not re-add the claim

Measured with a canary file outside the workspace: `--add-dir` and `--sandbox`
both allow reads *and* writes outside it, with or without
`--dangerously-skip-permissions`. `--add-dir` is additive scope; `--sandbox`
restricts terminal commands only.

The docs used to say these "constrain it, but that is agy's enforcement, not
ours". That was wrong and is corrected. **Do not restore it.** What is true:
the human approval at `execute_plan` is the only gate, and post-run hashing the
only detection — for paths we hash.

The approval prompt truncates arguments to ~40 characters, and it does **not**
truncate in schema order — the client rewrites its cached copy of our schema
with `properties` sorted **alphabetically**, and the model emits arguments in
that order. Declaration order never reaches the human.

So `execute_plan`'s human-readable argument is named **`affects`**: it must
sort ahead of `plan_digest`/`plan_id`, and it must be short, because every
character of the key is a character of scope the human does not read. Do not
rename it to something tidier and longer; two tests pin both properties.
Internally it is still `scope_summary` (variable, DB column, validator) — only
the wire name is short. Measured 2026-09-19; see `NOTES.md` §20.

## 6d. Restart the client after changing tool registration

`agy` keeps the server **process** alive across chat sessions and refreshes its
tool-schema cache (`~/.gemini/antigravity-cli/mcp/subagents/*.json`)
independently. A newly registered tool can therefore be present in the cache,
readable by the agent, and still answer `Unknown tool` from the process that is
actually serving. Measured 2026-09-19: a process from 10:20 served three
sessions that day, one of them after `execute_plan` was added.

Symptom to recognise before debugging anything else: `list_tools` in-process
returns the tool, the client says it does not exist. Kill the server process.

## 7. SQLite

```sql
PRAGMA journal_mode=WAL;
PRAGMA busy_timeout=5000;
PRAGMA foreign_keys=ON;   -- OFF BY DEFAULT. Omitting it silently voids every FK.
```

Every write goes through `db.write_transaction`, never a bare `connect()` plus
`INSERT`. It takes the lock up front with **`BEGIN IMMEDIATE`** and retries on
lock with bounded backoff. Both halves matter: `busy_timeout` does *not* cover
a deferred transaction that has already read and then tries to upgrade — SQLite
returns `SQLITE_BUSY` there without ever calling the busy handler — and taking
the lock first is what makes a retry safe, since nothing has been written yet.
The body of a write transaction is **not** idempotent (it inserts a `results`
row); never retry it after a partial write.

There is deliberately **no `approvals` table**: approval happens in the client's
permission prompt, outside this server's visibility. A row claiming we recorded
an approval we never observed would be a lie in our own audit trail.

## 8. Timing

- Default tool-call deadline is **180 s**; `timeoutSeconds` in client config
  raises it. The server cannot set it, so `propose_plan` must warn when its own
  estimate exceeds 180 s.
- `agy` process spawn overhead is **~9.7 s** (measured range 8.7–11.9 s over 25
  runs), constant across models. Budget per worker = `--print-timeout` + ~10 s.
- The cancellation path is the graceful degradation for an unconfigured install.
  It must keep working regardless of what the config says.

## 8b. Measurement rules

The bench shipped six defects because the harness had no tests while the
product code did. Measurement code is product code here.

- **A run that did not do the work never enters a median.** Verify the
  deliverable, record a `void_reason`, and report the void count beside the
  numbers.
- **Classify from structured fields** -- agy's JSON `status`, the `runs` table,
  files on disk. Never from prose or a whole-log grep; that is bench defect D2.
- **Never report a delta whose ranges overlap as an effect.** With single-digit
  samples there is no significance test worth running, but overlap is free to
  check and is enough to say "not measurable". Print the spread next to every
  median. Measured twice now: at n=3 the headline was 2.06x, at n=5 it was
  1.59x, and a second delta vanished (`NOTES.md` §35).
- **Report parent tokens and total tokens together.** Delegation reduces the
  parent's window and increases total spend; showing only the first is the most
  tempting dishonesty available to this project.
- **Rehearse against a fake before spending.** The dry run caught a verifier bug
  that would have voided every real run (`NOTES.md` §34).
- **An estimate states its provenance** -- "p90 of 20 local run(s)" or
  "benchmark median, no local history". A number nobody can trace is a number
  nobody can challenge.

## 9. Cut order

Cut in this order if time runs out: tool-call cache, then `max_parallel` to 2,
then git worktrees.

**Never cut** `collect`, the cancellation handler, digest validation, or taint
hashing. Each is the only thing making a specific claim true.
