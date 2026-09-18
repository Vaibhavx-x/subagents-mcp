
# Probe results

Client: `agy CLI 1.2.6` (run from an IDE integrated terminal — hence the
`VSCODE_*` env vars; schemas cache under `~/.gemini/antigravity-cli/`)
SDK version: `mcp 2.2.0`  Date: `2026-09-18`  OS: `Windows 11 (10.0.26200)`

Source: `probe.log`, the 13:49–14:23 window. The 12:55 entries are the local
in-process smoke test, not the client, and are excluded throughout. Server
restarts at 14:16:51 and 14:17:09 mark the point where the instrumented `stall`
build was loaded; everything after that is the current code.

---

## 1. whoami

- `protocol_version` reported: `2026-07-28`
- Era: ☑ modern (2026-07-28)  ☐ legacy (2025-11-25 or earlier)
- `elicitation` visible in capabilities: ☑ yes ☐ no ☐ not exposed on ctx
  - Full: `elicitation=ElicitationCapability(form=FormElicitationCapability(), url=UrlElicitationCapability())`
  - Both **form** and **url** elicitation are declared. Also `roots=RootsCapability(list_changed=True)`.
  - `sampling=None`, `experimental=None`, `extensions=None`, `tasks=None`.
- `env_var_count`: `67`   (shell comparison: `89`)
- `agy_on_path`: `C:\Users\vaibh\AppData\Local\agy\bin\agy.EXE`
- Inherited environment: ☑ full ☐ allow-listed ☐ unclear
  - 67 vars including the whole `VSCODE_*` block, `CHOCOLATEYINSTALL`, `ZES_ENABLE_SYSMAN`.
    This is the IDE's own environment passed through, not an allow-list.
  - `PATH_entry_count`: 41. `cwd`: `D:\Projects\subagents-mcp` (workspace root, not the server's dir).

**Consequence:** Q1 and Q2 both resolve favourably. This is a modern connection
and the client declares form elicitation, so the resolver design is available —
no `-32021` fallback needed. The environment is fully inherited, so `agy` and its
credentials are reachable without any `env=` in the registration. For contrast,
the SDK's own stdio client passes a 12-variable allow-list; Antigravity is far
more permissive than the pessimistic case the probe was designed to detect.

---

## 2. spawn_check

- returncode: `0`
- stdout (first line): `1.2.6`
- Verdict: ☑ agy spawns fine ☐ not found ☐ auth/credential failure ☐ other

Resolved to `C:\Users\vaibh\AppData\Local\agy\bin\agy.EXE` in 0.11 s, empty stderr.

**If it failed, what fixed it:** n/a — worked first time, unmodified.

---

## 3. ask_once (topic=auto)

- Resolver ran: `1` time(s)
- Tool body ran: `1` time(s)
- Result: `ACCEPTED proceed=True (topic='auto')`
- No-round-trip path works: ☑ yes ☐ no

`input_responses: null` and `request_state: null` in the body's context block
confirm no round trip occurred. Matches the in-process baseline exactly.

---

## 4. ask_once (topic=delete-build-dir)

- Form appeared in UI: ☐ yes ☑ **no — the client auto-cancels without showing anything**
- Resolver ran: `2` time(s)     ← 2 means the retry loop works
- Tool body ran: `1` time(s)    ← should be 1
- Second `tools/call` observed with identical arguments: ☑ yes
- Result on accept: **UNREACHABLE** — see below
- Result on decline: **UNREACHABLE** — see below
- Result on cancel: `CANCELLED (topic='delete-build-dir')`
- Any error code: `none`

**MRTR verdict:** ☑ transport works ☑ **but no human is ever asked**

### The round trip is real; the dialog is not

Three separate elicited rounds were recorded. Every one returned
`action='cancel' content=None`, and every one completed in **single-digit to
low-double-digit milliseconds**:

| Time | Topic | Round 1 → Round 2 |
|---|---|---|
| 13:53:14 | `delete-build-dir` | **14 ms** |
| 14:18:25 | `delete-build-dir` | **16 ms** |
| 14:23:48 | 1200-char digit ruler | **6 ms** |

No human reads a confirmation dialog and clicks Cancel in 6 ms. Antigravity
declares `ElicitationCapability(form=..., url=...)`, accepts the elicitation,
drives the retry correctly — and **auto-cancels it without ever presenting it to
the user**. The `accept` and `decline` branches are not untested; they are
unreachable through this client.

**Confirmed directly from the session transcript.** The agent called the tool
and received the result with no interruption, no prompt, and nothing for the
operator to click:

```
> call ask_once with topic delete-build-dir
● probe/ask_once(Call ask_once tool)
  Result:
    CANCELLED (topic='delete-build-dir')
```

**Correction to the previous revision of this file:** it recorded "Form appeared
in UI: yes", reasoning that only a rendered dialog could produce a cancel. That
inference was wrong; the millisecond timings disproved it and the transcript
above confirms it.

**Consequence — this is the finding that changes the architecture.** Mid-call
approval via elicitation is dead against Antigravity, despite the capability
being advertised. Fall back to the two-tool design: `propose_plan` returns the
plan and ends the call; the agent relays it; `execute_plan` runs on a separate,
explicitly-approved call. Rev. 2's single-call approval cannot work here.

The protocol plumbing being correct is still worth having: it means the same
server will work unmodified against a client that does render forms.

**Message rendering:** untestable — there is no UI to truncate. What the log
*does* establish is that a **1200-character topic reached the resolver intact on
both rounds**, byte-identical, so the client does not truncate tool arguments,
and the question hashed deterministically across rounds even at that length
(`Ssab_h90VY9bBh7csIyLrw`, versus `cA1C7xvQQrmtUTHMae6WeQ` for the short topic).
Message-length limits remain unknown and must be re-measured against whatever
client actually renders forms.

---

## 5. stall

| seconds | returned? | `STALL completed` in log? | client behaviour |
|---|---|---|---|
| 30 | yes | yes — `elapsed=30.0` | completed |
| 120 | yes | yes — `elapsed=120.0` | completed |
| 180 | yes | yes — `elapsed=180.0` | completed **on the exact boundary** |
| 240 | no | no | inconclusive — see note |
| 600 | no | `STALL CANCELLED at t=180.0s` | **cancelled at exactly 180 s** |

- Timeout ceiling: **`180 s` exactly (3 minutes)**
- Design impact: ☐ blocking call fine ☑ cap max_parallel ☐ split into start/poll

The instrumented 600 s run settles it precisely. Heartbeats every 10 s ran
cleanly to `t=170s`, then:

```
14:22:37,914 | STALL heartbeat t=170s
14:22:46,643 | STALL CANCELLED at t=180.0s (seconds=600)
```

Two things follow. First, the ceiling is a hard **180 s**. Second, the
`CancelledError` handler fired, so **cancellation propagates into the server
task** rather than the process being killed — in-flight `agy` children would
receive normal asyncio cancellation, not orphaning. That is the good failure
mode, and it also partially answers C2 without a separate test.

The client reports it explicitly, as a client-side deadline rather than a
transport failure:

```
MCP tool call to server "probe" timed out after 3m0s: context deadline exceeded
```

**The 180 s run is a warning, not a pass.** It completed at `elapsed=180.0`,
winning the race by milliseconds. Treat 180 s as the wall, not the budget.

**Note on the 240 s run:** it started 14:14:56, but the server restarted at
14:16:51 while it was still in flight, so it ran on the pre-heartbeat build and
produced no diagnostic line. The 600 s run supersedes it entirely.

**Sizing rule:** the whole fan-out must finish inside 180 s with real margin.
Budget ~150 s, set per-worker `--print-timeout` well below that, and cap
`max_parallel` so the slowest worker still lands inside it.

---

## 6. spin

- Highest `SPIN round=N`: `1`
- Approx. seconds across all rounds: `n/a — no multi-round sequence occurred`
- Sufficient for the one-round approval design: ☐ n/a — the approval design is dead for a different reason (§4)

Two `spin` calls were made, at 14:01:34 and 14:03:37. **Both logged `SPIN round=1
prior_state=None`.** The client never echoed `request_state` back, so each call
was an independent first round; the `InputRequiredResult` was treated as terminal
rather than as "not done yet, retry".

**This does not contradict result 4.** The two use different channels:

| Channel | Mechanism | Driven by Antigravity? |
|---|---|---|
| Elicitation (`ask_once`) | `input_requests` + `input_responses` | **Yes** — resolver ran twice (but auto-cancelled, §4) |
| Bare `request_state` (`spin`) | `request_state` only, no `input_requests` | **No** — never retried |

So Antigravity drives the retry loop when there is a question to put to the user,
and does not poll when the server merely says "not finished". The approval design
needs exactly one elicitation round and gets it, so this is sufficient — but any
design that depends on server-driven polling via `request_state` is dead.

---

## Decisions this probe settled

1. **Rev. 2's single-call approval is dead.** Antigravity advertises form
   elicitation, accepts the request and drives the retry loop correctly, but
   **auto-cancels every elicitation in 6–16 ms without showing a dialog** (§4).
   No human is ever asked. Fall back to the two-tool design: `propose_plan`
   ends its call with the plan, the agent relays it, and `execute_plan` runs on
   a separate explicitly-approved call.
2. **Keep `ElicitationResult[Confirm]` anyway.** It is what turned the
   auto-cancel into a returned `CANCELLED` value instead of an exception. With
   the bare model this probe would have surfaced a crash and the cause would
   have been much harder to see. It also means the server works unmodified
   against a client that does render forms.
3. **The tool-call ceiling is a hard 180 s**, measured exactly (§5). The 180 s
   run completed at `elapsed=180.0` — it won by milliseconds. Budget ~150 s for
   a whole fan-out, set per-worker `--print-timeout` below that, and cap
   `max_parallel` so the slowest worker still lands inside it.
4. **Cancellation propagates into the server task** rather than the process
   being killed, so `agy` children get normal asyncio cancellation. Partially
   answers C2 in the affirmative.
5. **Never rely on `request_state` polling.** Antigravity ignores a bare
   `InputRequiredResult` and treats it as terminal (§6).
6. **The environment is fully inherited and `agy` spawns cleanly** (rc 0,
   v1.2.6, 67 env vars). No `env=` needed in the registration.

## Still open

- **Does the Antigravity IDE agent panel behave differently?** Everything here
  is the `agy` **CLI** (schemas cache under `~/.gemini/antigravity-cli/`, and the
  transcript is CLI output). The GUI agent panel is a separate client and is
  untested. If it renders forms, single-call approval survives for GUI-driven
  runs — but the CLI is what the orchestrator would drive, so plan for the
  fallback regardless.
- **Is the auto-cancel configurable?** No approval/trust setting was located. A
  sweep of `agy` config for an elicitation policy is worth twenty minutes before
  committing.
- Elicitation message-length limits — unmeasurable until a client renders forms.
  Tool *arguments* are not truncated at 1200 chars.
- C2 orphan survivors under a hard kill (as opposed to the cancellation observed
  in §5).

## Things the probe itself got wrong

1. **Nothing in the code.** The draft imported and registered all five tools
   unmodified against `mcp` 2.2.0 on the first attempt — see [NOTES.md](NOTES.md).
2. Two cosmetic dead names: `describe_ctx` probes for `client_params` and `meta`,
   neither of which exists on `Context` in 2.2.0. Both log `"<absent>"` via a
   `getattr` default and were left in deliberately.
3. Edits made, both additive and both in service of measurement: module-level
   `COUNTS` / `bump()` / `reset_counts()` for the test assertions, and a
   heartbeat + `CancelledError` handler in `stall` — the latter is what turned a
   120–300 s bracket into an exact 180 s.

## Things this file got wrong

1. The previous revision recorded **"Form appeared in UI: yes"** for §4, inferring
   a rendered dialog from the `cancel` action. The millisecond round-trip timings
   disprove it. Corrected in §4. The lesson worth keeping: an `action='cancel'`
   is not evidence of a human — check the latency.

## Incidental finding: per-server `instructions.md`

Before calling the tool, the agent read — or tried to read —
`~/.gemini/antigravity-cli/mcp/probe/instructions.md`. The file does not exist,
because the probe constructs `MCPServer("probe", version="0.1.0")` with no
`instructions=`. Antigravity evidently caches a server's MCP `instructions`
string to that path alongside the tool schemas, and its agent reads it before
using the server.

That is a free lever for the orchestrator: pass `instructions=` to `MCPServer`
and the agent gets server-level guidance (when to fan out, how to size a run,
what `propose_plan`/`execute_plan` mean) without it having to be crammed into
every tool description. Worth confirming by setting it once and checking the
file appears.

## Registration gotchas found on the way

- The `probe` entry initially pointed at a stale `D:/Projects/probe.py`. Fixed.
- `agy mcp list` reads `~/.gemini/config/mcp_config.json`; the IDE panel showing
  `Plugins (~\.gemini\config\plugins)` is a **separate** plugin-scoped source that
  does not exist here. A global server never appears in that panel — this is not
  a fault. Discovery is confirmed instead by the cached schemas in
  `~/.gemini/antigravity-cli/mcp/probe/*.json`.
- `command: "python"` is ambiguous on this machine — `C:\Python313` (has mcp) and
  `Python311` (does not) are both on PATH. It happened to resolve to 3.13.7 in
  every logged run, but pin the absolute interpreter before trusting a negative.
- The server is respawned per session, so a code edit does not take effect until
  the session restarts. The 240 s `stall` run was lost to exactly this.
