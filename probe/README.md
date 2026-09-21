# Capability probe

Five tools against the real Antigravity client. Half a day. Run this before
writing any orchestrator code — two of the outcomes change the architecture.

A live run lands in `probe.live.log`, which is gitignored. The committed
`probe.log` is the recorded session this directory's results come from and
is never appended to. Nothing is ever written to stdout.

---

## Setup

```bash
pip install "mcp>=2,<3"
python -c "import mcp; print(mcp.__version__ if hasattr(mcp,'__version__') else 'installed')"
```

Register it. Either:

```bash
agy mcp add probe -- python D:/Projects/subagents-mcp/probe/probe_server.py
```

or add it to Antigravity's MCP config by hand. Use an **absolute** path and the
absolute python executable if `python` is ambiguous on your machine.

Then start an Antigravity session and confirm the tool list shows five tools.

**If it shows zero tools**, the server crashed at import. Run
`python probe_server.py` directly: it will sit waiting on stdin, which is
correct, and any import error appears immediately. The likely culprits are the
`mcp.server.mcpserver` import names — fix them against the installed package and
log the fix.

---

## Run order

Run them in this order. Each one can invalidate the need for the ones after it.

| # | Prompt to the agent | Gap |
|---|---|---|
| 1 | "call the whoami tool" | Q1 era, Q2 capabilities, env allow-list |
| 2 | "call spawn_check" | can the server even launch agy |
| 3 | "call ask_once with topic auto" | resolver path, no round trip |
| 4 | "call ask_once with topic delete-build-dir" | Q3 MRTR retry loop |
| 5 | "call stall with seconds 30", then 120, then 300 | tool-call timeout |
| 6 | "call spin" | MRTR round cap |

---

## How each gap is confirmed

### Q1 — Is this a modern (2026-07-28) connection or a legacy handshake?

**Tool:** `whoami`

**Evidence:** the `protocol_version` line in the log.

| Log shows | Meaning | Consequence |
|---|---|---|
| `protocol_version='2026-07-28'` | Modern. Antigravity took the `server/discover` path. | Plan rev. 2 stands. Continue to Q3. |
| `protocol_version='2025-11-25'` (or earlier) | Legacy handshake. | **MRTR never fires.** Go back to the rev. 1 back-channel design: `ctx.elicit()` works, mid-call approval is possible again. |
| Missing / `<absent>` | The SDK exposes it under a different name. | Read the full `=== WHOAMI ===` block; `client_params` usually carries it. |

Cross-check: `MCPServer` answers `server/discover` on every transport including
stdio, so if the log shows a legacy version, that is Antigravity choosing the
handshake, not the server failing to offer the modern path.

This also resolves the anomaly in the September probe, where an `initialize`
carried `protocolVersion: 2026-07-28` — a combination the handshake cannot
negotiate. Whatever `whoami` reports now is the authoritative answer.

---

### Q2 — Does the client declare `elicitation` on a modern request?

**Tool:** `whoami` first, then `ask_once` as the real test.

**Evidence, weak:** a `client_capabilities` entry in the `=== WHOAMI ===` block
containing `elicitation`. Absence here is not proof — the SDK may not surface it
on the context.

**Evidence, definitive:** step 4. If elicitation is undeclared, the resolver
refuses before sending anything and the call fails with:

```
-32021  Client did not declare the form elicitation capability required by resolver 'probe_server:ask_to_proceed'
data: {"requiredCapabilities": {"elicitation": {"form": {}}}}
```

That error *is* the answer, and `requiredCapabilities` names exactly what is
missing. A `-32021` here means the whole approval design is unavailable and you
fall back to two tools (`propose_plan` / `execute_plan`).

---

### Environment allow-list — can the server find and run `agy`?

**Tools:** `whoami` (the `=== ENVIRONMENT ===` block) and `spawn_check`.

A stdio child does not necessarily inherit your shell environment; the Python
SDK's own stdio client passes a minimal allow-list and merges only explicit
`env=` on top. Antigravity may do something similar.

**Evidence:**

| Observation | Meaning |
|---|---|
| `env_var_count` is close to your shell's `env \| wc -l` | Full inheritance. No problem. |
| `env_var_count` is single digits | Allow-listed. Check what is missing. |
| `agy_on_path` is `null` | The server cannot find agy. `PATH` is truncated or absent. |
| `spawn_check` returns a version string | **Green light.** The core mechanism works. |
| `spawn_check` returns `NOT FOUND` | Pass the absolute path to agy via config, or set `env=` in the MCP server registration. |
| `spawn_check` returns a non-zero exit with an auth error | agy's credentials are not reachable from this process. Find which variable or profile path carries them and pass it explicitly. |

**This is the highest-value cheap test in the set.** If the server cannot spawn
agy, nothing else in the project works, and finding out on day one instead of
day two is worth the twenty minutes.

---

### Q3 — Does Antigravity drive the MRTR retry loop?

**Tools:** `ask_once` with `topic=auto`, then `topic=delete-build-dir`.

**Step 3 (`topic=auto`)** exercises the resolver without asking anything. Expect
one `RESOLVER ask_to_proceed ran` line, one `=== ASK_ONCE BODY ===` block, and
`ACCEPTED proceed=True`. If this fails, the problem is the resolver wiring, not
MRTR, and there is no point running step 4 yet.

**Step 4 (`topic=delete-build-dir`)** is the real test. What to look for in
`probe.log`:

| Log pattern | Meaning |
|---|---|
| `RESOLVER ... ran` **twice**, then one `ASK_ONCE BODY`, then a verdict | **Full MRTR loop works.** The resolver re-ran on the retry; the body ran once. Plan rev. 2 is confirmed end to end. |
| `RESOLVER ... ran` once, then nothing | The client received the question and never retried. **MRTR is not driven.** Fall back to two tools. |
| `RESOLVER ... ran` many times, dialog reappears | The question is not rendering identically across rounds, or the client is not echoing `request_state`. Since the message here is derived only from `topic`, suspect the client. |
| `-32021` | See Q2. |
| `Elicitation not supported` | The client has no elicitation handler at all. Note: this is a **failed call**, not a decline — there is no "user wasn't asked" outcome your tool receives. |

Also watch the UI: a form should appear. Note whether the message text is
readable and whether a long message truncates — that decides whether a plan
summary can go in the prompt or whether it needs to be a count plus a pointer.

**Decline/cancel:** run step 4 again and dismiss the dialog. The tool should
return `DECLINED` or `CANCELLED` rather than erroring, because the parameter is
annotated `ElicitationResult[Confirm]` rather than the bare model. Confirming
this matters: it is what lets the orchestrator report a refused plan instead of
crashing.

---

### Tool-call timeout

**Tool:** `stall`, at 30 then 120 then 300 seconds.

**Evidence:** for each call, either the tool returns `slept Ns` and the log has a
matching `STALL completed` line, or the client gives up.

The log line is the important half. If the client reports a timeout but
`STALL completed` still appears afterwards, the server kept running and only the
client stopped waiting — which means a long fan-out would finish invisibly and
its result would be lost.

| Ceiling found | Consequence |
|---|---|
| ≥ 300 s | Fine. One blocking `run_subagents` call is viable. |
| 60–300 s | Cap `max_parallel` and per-worker `--print-timeout` so the whole fan-out fits inside it, with margin. |
| < 60 s | **The blocking design is dead.** Split into `start_run` (returns a run id immediately) and `poll_run(run_id)`. Add a day. |

This is the single test most likely to force a redesign, which is why it runs
before any orchestrator code exists.

---

### MRTR round cap

**Tool:** `spin`

**Evidence:** count the `SPIN round=N` lines. The highest N before the client
stops is its cap.

The design needs exactly one round, so any cap ≥ 2 is sufficient. The number is
worth knowing as margin, and a cap of 1 would mean even single-question
approval is impossible.

Note the wall-clock time across the rounds too. The spec's backoff for a
`request_state`-only response is short (tens of milliseconds, doubling to a low
ceiling), so if you see seconds between rounds, Antigravity implements its own
pacing — relevant only if you ever consider polling, which the round cap
probably rules out anyway.

---

### C1 (stdout) — already answered, no test needed

Resolved from the docs: while serving, the SDK diverts *flushed* stray stdout to
stderr on a best-effort basis, but import-time output, buffered output draining
at interpreter exit, and anything a wrapper script emits still land on the
protocol stream, where one junk line can make the host drop the connection —
which some hosts display as a server with no tools.

Rule for `CLAUDE.md`: no `print()` anywhere in the server. Use `logging`.

### C4 (request_state TTL) — already answered, no test needed

The 600-second TTL is **per round, not per call**, and `request_state` is only
verified on inbound `tools/call` / `prompts/get` / `resources/read`. A long tool
body after the final round is never re-checked, so fan-out duration is bounded
by the client timeout (above), not by the seal.

### C2 (subprocess orphans) — not a doc question

The SDK section on this describes `stdio_client`, i.e. the SDK acting as a
client spawning a server. Your case is your own asyncio code spawning agy, and
whether Antigravity kills your server's grandchildren is Antigravity's
behaviour, undocumented anywhere.

Test it during Day 2 rather than here: spawn agy with a short timeout, kill on
timeout, then check `tasklist | findstr agy` for survivors. If orphans survive,
use a Windows Job Object.

---

## Recording the results

Fill in `RESULTS.md` as you go. That file is a submission artifact, not
bookkeeping: a candidate who probed a client's real behaviour and wrote down
what they found reads very differently from one who assumed.

Keep the whole of `probe.log` in the repo too -- it is the record, not a
scratch file, which is why a live run writes somewhere else.
