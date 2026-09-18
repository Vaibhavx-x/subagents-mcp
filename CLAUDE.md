# CLAUDE.md — rules for working in this repo

Every constraint here was **measured against the real client**, not assumed.
If you are about to contradict one, re-measure first and write the result into
`NOTES.md`. Do not quietly "fix" one of these back to the obvious answer.

**Environment:** Windows 11, Git Bash, Python 3.13 (`C:\Python313\python.exe`),
`mcp` 2.2.0, `agy` CLI 1.2.6, MCP protocol `2026-07-28`.

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
- A task declaring an **empty read set** must be flagged at validation — a taint
  check over nothing passes silently and yields false confidence.

## 6. Async and subprocess invariants

- No blocking call inside an `async def` handler. (v2 runs sync `def` handlers
  on a worker thread, so this applies to the orchestrator specifically.)
- Read stdout and stderr **concurrently**. Sequential reads deadlock when a
  child fills the unread pipe — which only shows up under load, i.e. the demo.
- Write each worker's result to SQLite **as it lands**, never at the end.
  Writing at the end silently defeats `collect`, and only shows up on an overrun.
- Wrap per-worker result writes in `asyncio.shield`.
- Kill the **process group**, not just the parent. Cancellation propagates
  cleanly (measured); a hard kill is a different, still-untested path.

## 7. SQLite

```sql
PRAGMA journal_mode=WAL;
PRAGMA busy_timeout=5000;
PRAGMA foreign_keys=ON;   -- OFF BY DEFAULT. Omitting it silently voids every FK.
```

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

## 9. Cut order

Cut in this order if time runs out: tool-call cache, then `max_parallel` to 2,
then git worktrees.

**Never cut** `collect`, the cancellation handler, digest validation, or taint
hashing. Each is the only thing making a specific claim true.
