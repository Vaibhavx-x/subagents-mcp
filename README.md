# subagents-mcp

An MCP server that lets a parent agent delegate sub-tasks to ephemeral `agy`
subprocesses running in parallel, so the parent's context stays clean.

The parent splits a task and calls `propose_plan`. The server validates it —
paths, tiers, read/write conflicts — and returns a readable plan plus an id and a
digest. The human approves via the client's own tool-permission prompt. The
parent calls `execute_plan`. Workers run as separate processes, each writing its
full output to SQLite the moment it finishes and returning only a short summary
and a handle. `collect` reads results back, **including after a timeout**.

> **Status: pre-implementation.** The capability probe and the model benchmark
> are complete and their results are in this repo. The server itself is not
> built yet. Everything below that describes runtime behaviour is design, not a
> claim about running code.

---

## Why

Measured across 25 benchmark runs (`bench/RESULTS.md`):

| | min | max |
|---|---|---|
| **input tokens / turn** | **110,323** | **305,321** |
| output tokens / turn | 988 | 15,851 |

Per-turn input exceeds output by one to two orders of magnitude in every run.
**Context, not generation, is the dominant token cost.** A parent that performs
five sub-tasks itself accumulates all five tool transcripts; a parent that
delegates them carries five summaries.

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
  invisible at decision time is the failure that matters.
- **`cwd` pinned.** It otherwise defaults to the workspace root, which changes
  where the SQLite file lands.

`agy mcp add` cannot set `timeoutSeconds`, `cwd`, or `toolConfig` — it has no
flags for them. These are hand-edits to the JSON.

> The config parser is **lenient**: unknown keys and misspelled enum values are
> accepted silently rather than rejected. A typo here does not produce an error,
> it produces a setting that never takes effect. Verify behaviour, not syntax.

## Where the approval gate is

**The approval gate is the client's tool-permission prompt, which fires before
every MCP tool call.** `--dangerously-skip-permissions` disables it.

It is not elicitation. `agy` advertises elicitation support, drives the round
trip correctly, and then auto-cancels every request in 6–16 ms without rendering
anything — see `NOTES.md` §2. That is why there are three tools rather than one,
and why `execute_plan` takes a human-readable `scope_summary`: so the permission
prompt shows intent rather than an opaque id.

**What the code enforces:** which tasks are allowed into a plan, what each worker
is instructed to touch, digest validation against the approved plan, and — by
hashing declared paths before and after each run — whether what actually changed
matches what was declared.

**What it does not:** the server does not mediate worker file access. A worker is
an `agy` process with its own tools; `--add-dir` and `--sandbox` are agy's
enforcement, not this server's. Stated plainly rather than dressed up as a
sandbox.

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
