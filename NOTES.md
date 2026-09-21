# NOTES — assumptions that turned out wrong, and what overturned them

The running log of corrections. An entry earns its place only if a *reasonable*
assumption was checked against a primary source or a measurement and lost.

SDK-level verification lives separately in [`probe/NOTES.md`](probe/NOTES.md).

---

## 1. Misread a capability probe (protocol era)

The first probe used SDK v1 and reported protocol `2026-07-28` from an
`initialize` handshake. Reading the spec showed 2026-07-28 **has no handshake** —
a dual-era client probes `server/discover` first and falls back to `initialize`
only for legacy servers. The probe had measured the legacy path.

Cost avoided: about a day of building against the wrong protocol.

## 2. Inferred a UI that was never there (elicitation)

The client returned `action='cancel'` on every elicitation, which was first
written down as "a dialog appeared and was dismissed."

**The round-trip latency was 6–16 ms.** No human dismisses a dialog in 6 ms.
Confirmed against the session transcript: no prompt was rendered, there was
nothing to click. Traced to an unregistered per-surface callback — `McpManager`
exposes `RegisterElicitationCallback`/`Unregister`, the CLI registers none, and
the default result for an unregistered surface is a cancel.

**Lesson kept: `action='cancel'` is not evidence of a human. Check the latency.**

## 3. Benchmarked models and got the wrong answer twice

The first reading ranked 3.7 Flash medium first. A harness audit found three
blocking defects (D1–D3 in `bench/RESULTS.md`), including a cost proxy
(`log_bytes`) that correlates with real cost at **r = 0.13** and inverts the
ranking.

With real token counts from each log's `usage` block, **3.8 Flash *low* wins**:
same results, faster, ~7× fewer output tokens, and **zero thinking tokens on
every single run**. More reasoning effort bought nothing on this workload.

## 4. Measured an undocumented ceiling (180 s)

A hard 180 s tool-call deadline appears in no documentation. Found by binary
search with a heartbeat-instrumented tool: `stall(180)` completed at
`elapsed=180.0`; `stall(600)` was cancelled at exactly `t=180.0`. Then found the
`timeoutSeconds` client config that raises it, and confirmed a 600 s call
completing at `elapsed=604.2`.

---

## 5. Guessed a config key from a changelog instead of the schema

The plan proposed enabling background mode as:

```json
"tools": { "execute_plan": { "background": "always" } }
```

Both halves are wrong. Read out of the `agy` binary's embedded protobuf
descriptors:

```
protobuf:"bytes,14,rep,name=tool_config,json=toolConfig,proto3"
    protobuf_key:"bytes,1,opt,name=key" protobuf_val:"bytes,2,opt,name=value"
Background protobuf:"varint,1,opt,name=background,proto3,
    enum=exa.cortex_pb.McpToolBackgroundMode"
```

- The map's JSON name is **`toolConfig`** (or `tool_config`), never `tools`.
- The value is a **protobuf enum**, so protojson accepts only the full name:
  `MCP_TOOL_BACKGROUND_MODE_ALWAYS` (also `_OFF`, `_UNSPECIFIED`). Not
  `"always"`.

**The part that actually bites:** the config parser is *lenient*. Feeding
`agy mcp list` a bogus top-level key, the wrong map name, and the wrong enum
spelling all four parsed without complaint and listed the server normally. A
wrong key here does not error — it is **silently ignored**. That is worse than a
failure, and it is why guessing was never acceptable.

Status: correct spelling established from the schema; **not yet confirmed at
runtime**, because the lenient parser gives no feedback either way.

## 6. `instructions.md` — the right conclusion, reached from the wrong evidence

The MCP cache directory for the probe server contained only per-tool JSON
(`whoami.json`, `stall.json`, …) and **no `instructions.md`**, which looked like
the "free lever" claim was wrong.

It is not. `probe_server.py` constructs `MCPServer("probe", version="0.1.0")` and
**never passed `instructions=`** — so there was nothing to write. The binary
carries the agent-facing string that settles it:

> Each MCP server has a directory `%s` containing tool schemas
> (`<toolName>.json`) and optionally an `instructions.md` file with best
> practices.

So the lever is real and the file is optional. `instructions` is a genuine
`MCPServer.__init__` keyword in `mcp` 2.2.0. Still to verify at runtime: that
the file actually lands once a server sets it.

## 7. The bench's one "rate limited" run was never rate limited

`run.sh` classified by grepping the whole log for `429`. Exactly one run matched:

```
1789713666-C-t2-1   status=SUCCESS   429 found in: conversation_id
```

All 25 logs are `status: SUCCESS`. There was never a rate limit or a timeout in
the entire batch — the single exclusion was a UUID containing three digits.

It matters more than one lost row: that voided run is **the very run the plan
cites as its most expensive** ("15,851 output tokens, 13,661 of them thinking").
Its numbers were quoted while its row said `rate_limited`. Config C therefore had
**3 graded runs, not 4**, and the published table overstated its sample.

The cost figures survive: a `usage` block is a valid measurement whether or not
`check.sh` graded the run, so C's published token medians were right. What was
wrong was the claim about how many runs had been *checked*.

Fixed by classifying on the JSON `status` field via `bench/parse_log.py`. Never
grep a whole log for a status: the response prose contains numbers and words too.

## 8. The benchmark's wall-clock excluded process startup

The published per-task timings came from agy's own `duration_seconds`. Measuring
externally (`wall_ms`) against it across all 25 runs:

```
spawn overhead:  min 8.7 s   median 9.7 s   max 11.9 s
```

Constant across every model and task — it is CLI startup, not model time. So the
bench's "median 46.1 s" run actually occupies **~56 s of wall-clock**.

This lands directly on the Phase 3 budget: a worker costs
`--print-timeout + ~10 s`, and that overhead is paid **per wave**, not amortised
across a fan-out. A two-wave plan at a 600 s per-worker timeout needs
~1220 s — comfortably past the proposed `timeoutSeconds: 900`.

## 9. `.gitignore` alone would not have fixed the stale-bytecode defect

The proposed D1 fix was a `.gitignore` plus untracking, and a `--include='*.py'`
on t1's first grep. Two gaps:

1. t1's check greps **twice** — the second (`grep -ro "load_config" pkg/ | wc -l`,
   the ≥6 occurrence count) was left unfiltered and would still have counted
   bytecode.
2. Once `__pycache__` is gitignored, `git clean -fd` **stops removing it** —
   ignored files need `-x`. The reset between runs would have left stale `.pyc`
   in place, re-creating the exact contamination `.gitignore` was added to stop.

Both fixed. `setup.sh` now also fails loudly if bytecode is ever tracked again.

## 10. The fixture repo was left dirty after the last bench run

`run.sh` resets the fixture at the **start** of a run, so after the final run the
previous agent's edits stay on disk. `pkg/parser.py` and `pkg/cache.py` still
carried a passing implementation, and a bare `pytest` in the fixture reported
**8 passed** — which reads as "the baseline is green" when the baseline is not.

Baseline truth: `pkg/parser.py` contains no functions at all, so
`test_parser.py` fails by construction. That is what makes t3's original prompt
("the whole test suite must pass") unsatisfiable without also doing t2's work —
harness defect D4.

## 11. The re-run landed, and moved the limitation rather than removing it

t1 was re-run on the fixed harness: 8 runs, all four configurations, **8/8
passed**. The multi-file column is real data now rather than a recomputation,
and the ordering held — `gemini-3.8-flash-low` renamed a symbol across four
files, call sites and docstrings included, **spending zero thinking tokens**,
and was the fastest configuration doing it.

That was the result most worth having, because a scoped multi-file refactor is
the shape a fan-out worker actually performs.

But closing the gap exposed the real one. The bench now stands at **23 of 23
graded runs passed** — every configuration, every task. A bench where nothing
ever fails cannot rank quality; it ranks cost, and the "A matches the others"
claim is a statement about the tasks, not about the models.

So the honest phrasing is **"as good on tasks none of them failed"**, and the
most valuable thing this bench could gain is a task some configuration loses.
One non-monotonic result is already visible — C beat B on t1 (107 s vs 128 s) —
which at n = 2 is most likely noise, and is a reminder of how soft these medians
still are.

## 12. The probe's own pattern was already stale: `ctx.info()` is deprecated

`probe_server.py` is the proven-working v2 reference in this repo, so Phase 1
copied its shape -- including `await ctx.info(...)` for per-call logging. The
SDK answered immediately:

```
MCPDeprecationWarning: The logging capability is deprecated as of
2026-07-28 (SEP-2577).
```

The probe was written before that protocol revision landed, so "it worked in
the probe" was not evidence it is current. Logging now goes to the file logger
with `ctx.request_id` for correlation, and the test suite runs with
`error::DeprecationWarning` for this project's own modules so a future
reintroduction fails rather than warns.

*Lesson kept: a working reference implementation dates. Verified-once is not
verified-now, especially across a protocol revision.*

## 13. `instructions.md` confirmed, end to end

Entry 6 established from the binary that the file *should* be written. It now
demonstrably is. Registering the server and running one agy session produced:

```
~/.gemini/antigravity-cli/mcp/subagents/
  instructions.md     2546 bytes  -- byte-for-byte the instructions= string
  propose_plan.json   1398 bytes  -- the tool schema
```

The cached schema also confirms the SDK strips the `ctx: Context` parameter:
only `tasks_json` and `workspace_root` are exposed, both required. A `ctx`
leaking into the public schema would have invited the model to pass something
for it.

The last open question from the context transfer is closed.

## 14. Normcasing paths too early made the approval text wrong

Declared paths are normcased so that `README.md` and `readme.md` collide in
conflict detection and produce the same digest -- correct, since Windows treats
them as one file.

Applying that at parse time was wrong in a way only visible in the output: the
plan rendered `d:\projects\subagents-mcp\readme.md`. That text is what a human
is asked to approve, and it showed a filename that does not exist as written.

Real case is now kept on the Task for display, and normcasing happens inside
the digest and the conflict comparison. The distinction is between what the
code compares and what the human reads, and they are not the same thing.

## 15. `--add-dir` and `--sandbox` enforce nothing

The design rested on a sentence that was never tested: that `--add-dir` and
`--sandbox` constrain a worker, and that we were "trusting agy's enforcement,
not our own". Tested with a canary file outside the workspace:

| Configuration | Read outside | Write outside |
|---|---|---|
| `--add-dir` + `--dangerously-skip-permissions` | succeeded | succeeded |
| `--add-dir` + `--sandbox` | succeeded | succeeded |
| `--add-dir`, no skip flag | — | succeeded |

`--add-dir` is additive scope, not a boundary. `--sandbox` restricts terminal
commands only — its help says so literally, and it was read as "sandbox" anyway.
And `--print` mode auto-approves regardless of `--dangerously-skip-permissions`,
so a worker has no permission gate at all.

There was no enforcement to trust. The honest claim is that the human approval
at `execute_plan` is the only gate, and post-run hashing the only detection.

*Lesson kept: a flag named `--sandbox` is not a sandbox until you have watched
it refuse something.*

## 16. The approval prompt truncates, so parameter order is a safety property

The prompt renders and declining genuinely stops the call. But it shows only
about 40 characters of arguments, in **schema order**:

```
subagents/propose_plan({"tasks_json":"[{\"task_ref\"...) (ctrl+o to expand)
```

`execute_plan` was designed as `(plan_id, plan_digest, scope_summary)`. That
would have rendered as `({"plan_id":"b42ddb6e9399"...` — the human approving an
opaque id while the one readable argument sat past the cut.

Reordering to `(scope_summary, plan_id, plan_digest)` costs nothing and changes
the preview to:

```
subagents/execute_plan({"scope_summary": "edits pkg/config.py and pkg/serve...)
```

A test pins the order, because it looks cosmetic and is not.

**Superseded in part by entry 20.** The truncation is real; the claim that it follows *schema order* was not. It follows alphabetical order, and the reordering above bought nothing.

Also worth knowing: the prompt offers "always allow … (Persist to
settings.json)". A user who picks it permanently removes the only human gate in
the system.

## 17. Pipe EOF is not process exit

`asyncio`'s `proc.wait()` completes when the subprocess transport sees EOF on
the pipes — and a grandchild inherits those handles. So a worker that starts any
background process (a dev server, a watcher, a daemon left behind by a tool)
keeps the pipes open after exiting, and `proc.wait()` blocks until the timeout.

The worker would be recorded as a **timeout despite having succeeded**, its
transcript parsed correctly the whole time, and its process tree killed for no
reason. Caught by a test that spawned a grandchild and asserted success.

Fixed by polling process liveness directly. Two regression tests cover it: one
on the reported status, one on latency.

Related, and measured separately: `asyncio.wait_for(proc.communicate(), t)`
**discards** partial output when it times out. A child that flushed real work
before hanging yields `b''`. Draining into buffers owned by reader tasks keeps
it, which is what lets a timed-out worker still return what it managed to do.

## 18. `status: SUCCESS` does not mean the worker did anything

The worker classifier read agy's JSON `status` field and trusted it. Testing
the timeout and permission paths against real agy found two cases where agy
exits 0, reports `"status": "SUCCESS"`, returns real token usage, and has done
nothing at all:

**agy's own `--print-timeout` fires mid-turn:**

```
stderr: [agy] print timeout after 6s with turn in progress; returning partial output
stdout: {"status":"SUCCESS","response":"","duration_seconds":2.9,
         "usage":{"input_tokens":12715,"output_tokens":355,...}}
```

The requested file was never created.

**A tool was auto-denied in headless mode:**

```
stderr: jetski: no output produced -- a tool required the "command" permission
        that headless mode cannot prompt for, so it was auto-denied.
stdout: {"status":"SUCCESS","response":"",
         "denied_actions":[{"action":"command","display_name":"RunCommand"}]}
```

Both would have been recorded as successful workers. A parent would have been
told the sub-task was done.

The fix is a rule rather than more pattern matching: **a worker that produced no
answer did not succeed, whatever the status field says.** `denied_actions` and
the stderr banner then explain *why*, and the banner is matched against stderr
only — scoped the same way the D2 fix was, so a worker whose answer discusses
"print timeout" is not reclassified.

*Lesson kept: a success field is a claim, not evidence. Check the work.*

**Confirmed in production, 2026-09-20.** A real worker on a 30s budget returned:

```json
{"status":"SUCCESS","response":"","duration_seconds":29.03,
 "usage":{"input_tokens":164283,"output_tokens":2326,"cache_read_tokens":460430}}
```

with `[agy] print timeout after 30s with turn in progress` on stderr. Real
usage, real duration, `SUCCESS`, and no answer. The stderr rule caught it and
recorded `timeout -- agy hit its own --print-timeout mid-turn`. Reading the
status field would have filed an empty string as a completed 2000-word audit.

## 19. `--dangerously-skip-permissions` is required for workers, and my earlier
reading of it was wrong

Entry 15 concluded that print mode "auto-approves regardless", making the flag
pointless. That was true of the **file** tools I happened to test, and false in
general: the `command` tool is auto-**denied** in headless mode without it.

So a worker asked to run tests, a build, or git would be silently blocked — and
per entry 18, would report SUCCESS anyway.

Workers therefore pass the flag. It is not the blanket removal of safety the
name suggests: a worker has no human attached, so a permission prompt there
cannot be answered and a denial is a failure mode rather than a safeguard. The
human gate is the parent's `execute_plan` prompt, which is unaffected.

*Lesson kept: "I tested the flag" meant "I tested one tool class". The
generalisation was mine, not the measurement's.*

## 20. The prompt truncates in ALPHABETICAL order, not schema order

Entry 16 moved `scope_summary` to be `execute_plan`'s first parameter so the
human would read intent rather than an id, and pinned it with a test. The first
interactive `execute_plan` call showed what that actually bought:

```
subagents/execute_plan({"plan_digest":"30bccf8b20d6f83a9f5d80627072...
```

The entire ~40-character preview, spent on a hash. **Worse than the opaque id
the reorder was meant to avoid.**

The cause is visible in the client's own cache of our schema,
`~/.gemini/antigravity-cli/mcp/subagents/execute_plan.json`:

```json
"parameters":{"properties":{"plan_digest":{...},"plan_id":{...},"scope_summary":{...}},
"required":["scope_summary","plan_id","plan_digest"]}
```

`properties` is rewritten **sorted alphabetically**; `required` keeps our
declared order. The model emits its arguments in `properties` order, so
alphabetical is what reaches the prompt. Our own schema was correct the whole
time — `list(schema["properties"])[0] == "scope_summary"` passed, and measured
nothing that mattered.

The fix is the only lever left: a name that sorts ahead of `plan_*`. The
parameter is now **`affects`**, which also frees preview budget — every
character of the key is a character of scope the human does not read:

```
{"affects":"writes scratch/note_a.txt and s...     <- 28 chars of content
{"scope_summary":"writes scratch/no...             <- 22, if it sorted first
{"plan_digest":"30bccf8b20d6f83a9f5...             <- 0
```

Internally it is still `scope_summary` (variable, DB column, validator); only
the wire name is short. The test now pins the property that is true of the
*client*: the argument that sorts first must be the readable one. Declared
order is kept as well, in case a client ever honours it.

*Lesson kept: I pinned the half of the mechanism I could see from inside the
server. A test that passes in-process can still be measuring the wrong end of
the wire — the check had to run against the client's copy, not ours.*

## 21. The client keeps the server process alive across sessions

The first `execute_plan` attempt failed with `Unknown tool: execute_plan`, from
our own log, while `list_tools` in-process returned all three. The serving
process (PID 4956) had started at 10:20 that morning — before `execute_plan`
existed — and had outlived every chat session since.

The two caches are independent: `~/.gemini/antigravity-cli/mcp/subagents/*.json`
had been refreshed and *did* contain `execute_plan.json`, so the agent read a
schema for a tool the live process could not serve. Ending a chat does not
restart the server.

So: after changing tool registration, kill the server process or restart the
client. Otherwise the symptom is a tool that demonstrably exists, is documented
in the client's own cache, and returns "unknown" — which cost the parent agent
a long detour into reading `server.py`, `config.py` and 800 lines of log to
diagnose. Exactly the context burn this project exists to avoid, triggered by a
stale process.

## 22. An eager tool is visible, not preferred

`forceAllToolsEager: true` was added so the orchestrator would be in context at
decision time, on the reasoning that a deferred tool cannot be chosen. True, and
not enough.

Measured 2026-09-20. The same task was given twice in one session. Phrased as
*"Use the subagents MCP server. Do not do any of this work yourself. Call
propose_plan with..."* the parent delegated, and paid two summaries and a
handle. Phrased as a bare task description — the way anyone would actually ask —
the parent did it itself: it read `NOTES.md` (432 lines), `README.md` (248),
`CLAUDE.md` (209), all seventeen files under `subagents/`, `server.py` (267),
and two directory listings, then began writing the output directly. The
orchestrator was loaded, eager, and never considered.

So the honest shape of the claim is: eager registration removes one failure
(the tool being invisible), and does not create a preference. The parent's
default is to do the work, because doing the work is what it is for.

Levers that exist, and their limits:

- the server's `instructions.md` — verified to be read at call time, but the
  agent only reads it once it has decided to engage the server, so it shapes
  *how* delegation happens, never *whether* it does;
- how the request is phrased, which is the user's, not ours;
- a project-level agent instruction file, which **agy 1.2.6 does not have** —
  no flag in `agy --help`, and `agy agents` lists none.

*Lesson kept: I had written the config note as though visibility solved
adoption. It solves being choosable. The run that exposed this is also the
cleanest demonstration of the problem the project exists for — the parent
burned twenty-odd file reads on a task it could have delegated for a sentence.*

## 23. A BOM made `.env` do nothing, silently

Test 3 was meant to force a worker timeout. The worker was given a 45-second
budget, ran for **105.9 seconds**, and reported success. Nothing errored.

The `.env` had been written with `Out-File -Encoding utf8`, which in PowerShell
5.1 means **UTF-8 with a BOM**. `_load_dotenv` read it as plain `utf-8`, so the
first key parsed as `﻿SUBAGENTS_WORKER_TIMEOUT_S`, matched nothing, and
the 600-second default applied. A file that exists, is readable, contains the
right key spelled correctly, and has no effect whatsoever.

Three fixes, because one was not the problem:

1. `_load_dotenv` now reads `utf-8-sig`. A CRLF+BOM file round-trips, pinned by
   a test written from the exact bytes PowerShell produces.
2. Unknown `SUBAGENTS_*` keys are logged as warnings at startup. The README
   criticises the client's config parser for accepting typos silently; doing
   the same thing ourselves was worse than the thing being criticised.
3. The startup log now records the **effective** `worker_timeout`,
   `max_parallel` and `model`, not just paths. A config that failed to apply is
   otherwise indistinguishable from one that worked, and the difference only
   surfaces as a worker running for the wrong length of time.

Two other things had to be true at once for this to hide, and both were: the
process-kill command that should have restarted the server was mistyped
(`$_.ProcessId-Force`, no space), so the old process may have survived anyway —
and even a correct restart would have read the same dead file.

*Lesson kept: the failure was not that the setting was wrong. It was that
nothing in the system could tell me whether the setting had been read. Config
that cannot be observed after the fact is config you are guessing about — which
is the same complaint this project makes about the client, arrived at from the
other side.*

## 24. An estimate above its own ceiling

The same run printed:

```
estimate : ~70s expected, ~40s worst case
```

Expected exceeding worst case is impossible by construction — the worst case is
the worker's own deadline, and nothing can run longer than it. The cause was
that `expected` came from `EXPECTED_WORKER_S`, a benchmark median, which knows
nothing about the budget it is being spent under. With the usual 600s budget the
two numbers look sensible and the bug is invisible; it only surfaced because the
budget was dropped to 30s for an unrelated test.

`expected` is now `min(EXPECTED_WORKER_S, worker_timeout_s)` per worker.

Not cosmetic: the pair exists so a human can judge whether a plan fits the
client deadline. One of the two being arithmetically impossible discredits both,
and it is the sort of thing a reader notices immediately and a test suite never
does.

*Lesson kept: found by reading output during a test aimed at something else.
Every number the tool prints is a claim, including the ones nobody asked about.*

## 25. The cancellation handler I added does nothing on this machine

Cancelling a fan-out must not leave `agy` processes running, so `run_worker`
grew a `CancelledError` handler that terminates the process tree, and a test to
prove it. Then I deleted the handler and re-ran the test. **It still passed.**

On Windows the `finally` block already calls `group.close()`, which drops the
last handle to the job -- and `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE` takes the
tree down without being asked. The explicit `terminate()` is doing nothing here.

It is not dead code: on POSIX `close()` is a no-op and nothing else reaps the
tree, so the handler is the only thing that makes the two platforms behave the
same. But the comment I first wrote -- "without this a cancelled fan-out leaves
live processes" -- was false on the only machine that can run the test.

Both the comment and the test docstring now say which half is verified here and
which half is reasoned about. The test guards the outcome, not the mechanism.

*Lesson kept: the test passed, so the code looked justified. Deleting the code
and watching the test pass anyway is the only thing that told me otherwise --
and that check costs thirty seconds.*

## 26. An exhausted rate limit escalated to a more expensive model

A rate-limited worker is retried with backoff rather than escalated: a 429 is
the provider asking for less traffic, not a task the model got wrong.

The bounded-retry test then failed with `assert 6 == 3`. After three attempts
the rate limit was exhausted, the result was still a failure, and the
escalation path picked it up and ran it three more times on
`gemini-3.7-flash-medium` -- answering a quota refusal by sending a **more
expensive** request at the same quota.

Two rules that were individually right composed into something neither
intended. `should_escalate` now refuses a rate-limited result outright.

*Lesson kept: the bug was in the seam, not in either rule. It surfaced only
because the test asserted an exact attempt count rather than "eventually
failed" -- a looser assertion would have passed and the spend would have been
real.*

## 27. `blocked` and `tainted` had drifted from what the code does

`instructions.md` -- the text the parent agent actually reads -- described
`blocked[]` as "refused for scope" and `tainted[]` as "a file the task READ was
changed by another worker while it ran". Both were written in Phase 1 as
intentions. Phase 3 implemented neither.

What they mean now: **blocked** is a task that never ran because the task it
depends on failed, and **tainted** is a task that changed paths the plan did
not declare. A parent acting on the old text would re-propose the wrong task.

*Lesson kept: documentation written ahead of the code is a forecast, and a
forecast that is never reconciled becomes a lie told confidently to the one
reader who cannot check it.*

## 28. A different MCP server's log tainted every task in the plan

The first real multi-wave fan-out through the client worked: three workers, two
waves, the undeclared write caught exactly as designed. It also reported
something nobody wrote:

```
[seed-a] TAINTED: 1 undeclared path(s)
    d:\projects\subagents-mcp\probe\probe.log
[seed-b] TAINTED: 1 undeclared path(s)
    d:\projects\subagents-mcp\probe\probe.log
[merge]  TAINTED: 2 undeclared path(s)
    d:\projects\subagents-mcp\probe\probe.log
    d:\projects\subagents-mcp\scratch\notes.txt
```

`probe/probe.log` is written by **`probe/probe_server.py`** -- a second MCP
server registered in the same client, which agy keeps running and which appends
to its log inside the same workspace root. Every task in the plan was marked
dirty for a file no worker touched.

The ignore list covered our own database and log. It did not cover the general
case, which is the one that matters: **we cannot tell which process changed a
file, only that it changed.** An editor autosaving, a watcher rebuilding, a
second MCP server logging -- all of it looks exactly like a worker.

`DEFAULT_IGNORE_GLOBS` now excludes `*.log`, `*.tmp`, `*.swp`, `*.pyc` and
`*~`, and `SUBAGENTS_TAINT_IGNORE` takes extra globs for whatever a given
project generates. A test pins that a real source file is still caught, because
an ignore list is a hole and the hole must stay smaller than the thing it is
hiding.

*Lesson kept: I wrote in CLAUDE.md that "a detector that always fires is one
nobody reads", implemented the exclusion for the churn I could think of, and
shipped. The first real run found churn I had not thought of, from a process I
had forgotten was running. The rule was right and my list was short.*

## 29. Two numbers from the same run, both worth keeping

**Attribution held up live.** Wave 0 ran two workers and reported "1 undeclared
path(s) changed **in this wave**"; wave 1 ran one worker and reported "2
undeclared path(s) changed **by this task**". The wording tracked the actual
number of possible authors without anyone tuning it for the demo.

**The estimate was 4x high.** The plan predicted ~140s expected; the run took
**34.6s** (wave 0: 14.1s for two workers in parallel, wave 1: 19.4s). It also
warned that the worst case (~1220s) exceeded the 900s client deadline, which
was true and unhelpful -- worst case assumes every worker burns its full
600s budget. Both numbers are honest and the expected one is badly calibrated,
which is the standing argument for p90 over a benchmark median.

## 30. The escalation retry was not taint-checked

Found by re-reading the wave loop rather than by a failure, which is why it is
worth writing down: nothing would have reported it.

Taint is computed from a snapshot pair taken around the wave. Escalation runs
*after* the second snapshot -- deliberately, so a retry cannot pollute the
window it would be measured in. The consequence nobody wrote down: **a retry's
filesystem changes were never compared against anything.** A worker that failed
and then, on the stronger model, wrote files it had not declared came back
clean. The verdict stored against it was the one computed from the attempt that
did nothing.

That is a hole in the only control that survives the human approval, and it sat
in the exact path a failure takes -- which is when a worker is most likely to
do something unexpected.

Each retry now gets its own snapshot pair. It runs alone, so its attribution is
per-task rather than per-wave, which is strictly better than the wave it came
from. Verified by deleting the fix and watching the test fail.

Found alongside it: `started += len(result.escalated)` added the running total
each wave instead of that wave's retries, so `executions.workers_started`
claimed more workers than ever ran. Two waves with one escalation each recorded
three extra.

*Lesson kept: "escalation happens after the wave so a retry cannot pollute the
snapshot" was a correct reason for a decision whose consequence I never
followed through. The comment explained the choice and hid the gap.*

## 31. MCP tools are auto-DENIED in headless mode -- and there is a narrow escape

Phase 4 needs a parent measured with and without the server, which means a
parent running under `--print`. Entry 19 established that headless agy
auto-approves file tools and auto-denies the `command` tool. MCP tools had
never been tested, and the whole experiment depended on the answer.

Probed. They are **denied**, and the denial wears the same disguise as every
other failure in this project:

```
status: SUCCESS   response: ""   input_tokens: 39,949
denied_actions: [{"action": "mcp", "display_name": "CallMcpTool"}]
```

Forty thousand tokens spent, nothing done, `SUCCESS` reported. Our own §6b rule
catches this shape for workers; here it was the *parent*, and no rule of ours
was watching.

The stderr, which is where agy keeps the useful half of its output:

> a tool required the "mcp" permission that headless mode cannot prompt for,
> so it was auto-denied. Add an allow-rule under permissions.allow in
> settings.json (e.g. mcp(<target>)). Alternatively, re-run with
> --dangerously-skip-permissions to auto-approve all tools.

Two escapes, and the hint does not say what `<target>` is. Tried the plausible
spellings; the **most specific one works**:

```json
{ "permissions": { "allow": [
    "mcp(subagents/propose_plan)",
    "mcp(subagents/execute_plan)",
    "mcp(subagents/collect)"
] } }
```

in `~/.gemini/antigravity-cli/settings.json`. That matters beyond the
measurement: someone automating this server does **not** have to hand every
tool a blank cheque with `--dangerously-skip-permissions`. They can allow
exactly these three and leave every other gate standing. It belongs in the
README, because the alternative a user would otherwise find is the sledgehammer.

The A/B harness still uses `--dangerously-skip-permissions` uniformly across
all three arms -- identical invocation is what keeps the arms comparable, file
tools are auto-approved headlessly either way, and a fifteen-minute measurement
should not be mutating a global settings file it might crash halfway through.
Disclosed in the results rather than buried.

*Lesson kept: the gating unknown took one probe and twenty seconds. I had
listed three possible outcomes for it and the real answer was a fourth, printed
on stderr by the tool itself.*

## 32. The registration toll came out negative, which is a fact about noise

The three-arm A/B exists to separate two costs: what registering the server
takes from the parent's context before any work happens (schemas plus
`instructions.md`, loaded every turn), and what delegation then gives back.

Nine runs later:

```
A solo        98,304 parent input   (78,114-99,582)
B registered  76,412                (65,566-91,399)
C delegating  47,830                (34,438-64,160)
```

B - A = **-21,892**. Registering a server cannot reduce the parent's context.
The A and B ranges overlap across almost their whole width, so this is the
noise floor and nothing else -- agy's per-turn input varies enormously, which
the original bench already found (110k-305k).

The temptation was real: the number looks like a second finding, and "the
schemas are free" would have been a nicer sentence than "we cannot measure it".
The report now computes range overlap for every delta and prints *within
run-to-run variance* with an explicit **do not quote this as a saving**.

What the same nine runs DO support: A and C do not overlap at all (78,114 floor
against a 64,160 ceiling). The delegating parent carried 2.06x less input, and
that one is a measurement.

**Updated at n=5 (§35): the toll is still negative and still overlapping. The
A−C separation survived; the headline ratio did not.**

*Lesson kept: three runs produce a median, and a median always looks like a
result. The spread is what says whether it is one -- which is why the spread
column is printed next to every median rather than kept in a footnote.*

## 33. Delegation costs 2.23x the total tokens, and that belongs in the README

The same runs, counting everything rather than only the parent:

| arm | parent input | worker input | total |
|---|---|---|---|
| A solo | 98,304 | 0 | 98,304 |
| C delegating | 47,830 | 170,914 | **218,744** |

Four workers each re-read context the parent already held. **Total spend more
than doubles.**

This was predictable and is still worth stating plainly, because the project's
pitch -- "keeps the parent's context clean" -- is easy to read as "cheaper",
and it is not. What delegation buys is headroom in the window that actually
fills up and forces a compaction, paid for in tokens elsewhere. A reader who
discovers that ratio themselves after adopting the tool would be right to
distrust everything else in the README.

*Lesson kept: the caveats were written into the report generator before the
numbers existed, which is the only reason this one survived contact with a
table I wanted to look good.*

## 34. A dry run caught the bug that would have voided every measurement

Before spending a token, the A/B harness was rehearsed end to end against a
fake parent (`tests/fake_agy.py`). Arms A and B both came back **void**:

```
work not verified: worker.md names none of its module's definitions;
execution.md names none of its module's definitions; ...
```

The verifier checks that a document mentions at least one symbol its module
actually defines -- cheap evidence the file was read rather than the prose
invented. It extracted those symbols with string surgery:

```python
line.split("(")[0].removeprefix("def ").removeprefix("class ").strip()
```

For `def build_command(task, ...)` that yields `build_command`. For a base-less
class declaration -- `class WorkerResult:` -- there is no bracket to split on,
so it yields **`WorkerResult:`**, with the colon. No prose ever written
contains that string.

Every real run would have voided, the measurement would have produced zero
usable rows, and the cause would have looked like the workers failing rather
than the verifier being wrong.

*Lesson kept: the rehearsal cost two seconds and a fake process. The bug it
caught would have cost the entire measurement and, worse, might have been read
as a finding about the model.*

## 35. Two more runs per arm moved the headline 23%

The A/B results were published at three repeats per arm with a caveat written
before the data existed: *three runs cannot separate an effect smaller than the
spread.* Then two more repeats per arm were run, and the caveat collected.

| | n=3 | n=5 |
|---|---|---|
| A solo (median) | 98,304 | **99,582** |
| C delegating (median) | 47,830 | **62,551** |
| headline ratio | **2.06x** | **1.59x** |
| A range | 78,114–99,582 | 78,114–**144,008** |
| C range | 34,438–64,160 | 34,438–**66,162** |
| B−C separated? | yes | **no** |

Two runs of arm A came in at 142k and 144k — nearly double its previous
ceiling. Nothing changed but the dice. The delegation saving (B−C) collapsed
into overlap, separated by 596 tokens at n=3 and not separated at all at n=5.

What survived: **A and C still do not overlap** (78,114 floor against a 66,162
ceiling), so the claim that delegation reduces the parent's context is still a
measurement rather than a hope. It is just a 1.59x measurement, not 2.06x.

The honest reading is that agy's per-turn token usage has a very wide natural
spread — the original bench found 110k–305k and this is the same phenomenon —
and any single-digit sample will produce a confident-looking median that moves
when you look again.

*Lesson kept: I wrote "three runs cannot separate an effect smaller than the
spread" as a caveat, published a 2.06x headline underneath it, and then watched
it become 1.59x. The caveat was not hedging. It was the more accurate of the
two statements I made that day.*

## 36. `report_progress` is emitted correctly and rendered by nothing

Closed as a negative, which is still an answer.

The emission half is pinned by a test: the SDK client accepts a
`progress_callback`, and our notifications demonstrably arrive over the
protocol, one per wave. Worth having, because `ctx.report_progress` is a
**coroutine** and the version that forgot to await it sent nothing while
raising only a warning nobody reads.

The display half was checked twice against the real client, on a two-wave plan
chosen so the notifications land about fifteen seconds apart with a visible gap
to fill. Nothing appeared between the `execute_plan` call and its result,
either time.

The honest epistemic status: scrollback would not retain a transient spinner,
so this is "no evidence of rendering after two deliberate looks", not "proved
absent". It is recorded that way rather than as a flat fact.

Nothing depends on it. The calls stay, wrapped in a `try/except` that logs and
continues, because a client that does render progress costs us nothing and a
client that raises inside the callback must not lose a run.

*Lesson kept: half of this question was answerable by a test and half needed a
human watching a screen. Splitting it that way turned an open item into one
pinned invariant plus one cheap observation, instead of a vague "unknown" that
sat in the list for three phases.*

## 37. The parent reads the server's source before using it

The progress check was pasted without the framing line, so the parent got a
bare task description. It delegated anyway -- it found the server, read
`instructions.md`, and called `propose_plan` correctly.

But look at what it did first: `dir`, the client's MCP cache directory, all
three tool JSON schemas, `instructions.md`, `.env.example`, an attempted read
of `.env` (which does not exist), `server.py` twice, `config.py` three times,
`execution.py` twice, `worker.py` twice, and `Get-Command agy`. Roughly sixteen
tool calls investigating the orchestrator **before** using it.

Two things follow.

**It qualifies the A/B numbers.** Arm C's prompt says "do not do this work
yourself, and do not read the source files yourself", which suppresses exactly
this behaviour. So 1.59x is measured under close to best-case conditions for
delegation; a parent meeting the server for the first time with a vaguer prompt
pays an exploration cost we did not measure. Added to the caveats in
`bench/ab/RESULTS.md`.

**It is the §22 finding again from the other side.** There, a parent given no
framing did the work itself. Here, a parent given no framing delegated -- but
paid a large context toll deciding to. Both are the same underlying fact: what
the parent does with an unfamiliar tool is not something the tool controls.

*Lesson kept: I read the transcript looking for a progress spinner and found a
caveat about the measurement instead. The thing you went to check is rarely the
most interesting thing in the log.*

## 38. A table the schema promised and nothing ever wrote

`file_hashes` has been in `db.py` since Phase 1. It has columns for a path, a
phase (`pre`/`post`), and a sha256. `tools/inspect_db.py` was built to display
a run's evidence. `CONTEXT.md` section 5 lists it as part of the design.

It had **zero rows**, across 33 runs and four phases.

The hashing genuinely happens -- `execution.py` snapshots before and after
every wave, and the taint verdict is computed from real sha256 values. The
evidence was then discarded the instant the verdict was derived from it. So
`runs.tainted_paths` could say *that* a path changed and could never say what
it changed **from**, which is the difference between a verdict and a record.
Anyone auditing a flagged run afterwards had nothing to check.

Nothing failed, which is why it lasted. A table with no rows raises nothing,
returns cleanly, and reads as "no taint found" rather than "never looked".

Fixed by folding the hashes into the same transaction as the verdict --
deliberately one write, not two, because a run that ended with a verdict and
no evidence would be worse than either alone. Declared paths only: the
whole-tree manifest is a change detector, not an audit record, and persisting
one row per file in the workspace would make the table unreadable and every
wave slower. The docstring says so, so that nobody later "completes" it.

*Lesson kept: I found this by reading the schema against `grep` for its own
table name, while looking for somewhere to hang the cache. Four phases of
tests never noticed, because every test asserted on the verdict and none
asserted on the evidence behind it.*

## 39. The test suite had been writing to the production database

Found the same afternoon, from the same habit: check the number before quoting
it. `pytest` ran, and the repo's `subagents.db` gained a row.

`tests/smoke_stdio.py` spawns a **real** server subprocess against the **real**
repo root -- which is the entire point of it; that is what catches stdout
pollution that no in-memory test can see. But it spawned it with an inherited
environment, so the server loaded the real config, and its `propose_plan` round
trip persisted a real plan into the real database. Once per smoke run, since
Phase 1.

The count:

| | |
|---|---|
| plans on record | 104 |
| of which were this smoke test | **82** |
| plans that ever had a run | 14 |

**So a claim I published rested on a population that was 79% test artifacts.**
The worktree decision in Phase 4 was recorded as "zero write-write conflicts
across 87 plans". The conclusion survives -- zero conflicts is still zero
conflicts, and none of the 14 real plans had one -- but the *evidence* was
nothing like as strong as the sentence implied. 82 of those plans were a single
read-only task named `smoke-read`, which cannot produce a write-write conflict
even in principle. Quoting them as evidence about conflicts was quoting the
denominator of a question they were never asked.

Corrected in `CONTEXT.md` and `ZUDDL.md` to "across 14 plans that actually ran
workers", which is the honest number and is admittedly thin.

Fixed by passing `SUBAGENTS_DB_PATH` and `SUBAGENTS_LOG_FILE` into the child's
environment, merged over `os.environ` rather than replacing it -- a child with
no PATH cannot find its own interpreter's DLLs on Windows. The regression test
asserts the production database's mtime is unchanged by a smoke run.

*Lesson kept: this is the same failure as bench defect D1, one level up. There
the harness counted runs that did not do the work; here the project counted
plans that were never real work. Both times the number was bigger than the
evidence, and both times nothing raised an error -- a count is not a thing that
fails loudly when it is wrong.*

## 40. The cache never hit, and the fake agreed with it

The integration was written, the wave loop called `cache.lookup`, the tests
for the key all passed -- and not one end-to-end test hit. Twelve failures at
once, which is the good kind: a single cause.

`record` keyed the entry on `worker_result.model_used` -- what agy reported
back. `lookup` keyed on `task.model or config.model` -- what the plan asked
for. Under the fake worker those were `"fake"` and `"gemini-3.8-flash-low"`,
so every entry was filed under a name nothing would ever ask for.

The fix is one line and the reasoning is the useful part: **a lookup happens
before any worker exists**, so the requested model is the only thing it can
possibly know. Keying on what came back is keying on something unavailable at
the moment the question is asked.

It also settled a question I had been deferring. An escalation retry runs on
the stronger model, so it now records under `model_escalate` -- and a later
plan asking for the default model therefore MISSES and runs the task properly.
That is the conservative direction: the success on record was only achieved by
the stronger model, and serving it as though the cheap one had produced it
would be a promise the entry cannot keep.

*Lesson kept: the failure was loud only because the assertions were on
behaviour (`spawned == []`) rather than on the cache's own internals. A test
asserting `record` was called would have passed, and the feature would have
shipped doing nothing -- which is exactly how `file_hashes` survived four
phases (section 38).*

## 41. The prediction was zero and the answer was two

Written into the Phase 5 plan before any of it existed: the retrospective hit
rate would be **0 of 33**, because every A/B run materialises a fresh
workspace and the key covers the absolute root. A non-zero answer was to be
chased rather than accepted.

It came out at **2 of 33**, and the chase was worth it.

All 20 A/B worker runs were cold exactly as predicted. Both candidates are
ad-hoc runs from Phases 2-4, and both are the re-propose loop:

| | run 1 | run 2, 23 minutes later |
|---|---|---|
| status | `ok` | **`timeout`** |
| input tokens | 219,870 | **164,283** |
| result | wrote `scratch/audit.md` | nothing |

Same instruction, same three declared reads, same workspace. `git log` shows
nothing committed to those files in between, so the inputs were almost
certainly identical -- almost, because uncommitted working-tree edits in that
window cannot be reconstructed now.

And the second run is the transcript already quoted in section 18: `SUCCESS`,
empty response, real usage, no answer. **164,283 input tokens re-attempting
work that was already complete, and it still returned nothing.**

The number stays reported as an *upper bound* rather than a rate, because two
of the three conditions for a hit -- the read hashes and the write hashes --
have no data before this phase. A counted run might have hit; an uncounted one
could not have. Zero would have meant zero, which is what makes the bound
worth computing at all.

*Lesson kept: writing the prediction down first is what turned "2 of 33" from
a disappointing number into a finding. Without it I would have reported 6% and
moved on, instead of going and reading what those two runs actually were.*

## 42. A cache hit left the failed attempt's fingerprints on the row

Found by auditing rather than by a failure, which is the only way this one was
ever going to surface.

`persist_cached` writes a run row with no `started_at`/`finished_at` and zero
tokens, and the docstring states both as guarantees. The INSERT honoured them.
The `ON CONFLICT ... DO UPDATE` branch did not: it set the status, the reason
and the token counts, and left `started_at`, `finished_at` and `model_used`
exactly as the previous attempt had written them.

Reaching that branch takes a specific order, which is why no test found it:

1. plan P runs the task for real and **fails**, leaving a row with a real
   duration on it;
2. a different plan then does the same work successfully, recording an entry;
3. P is re-executed, and now hits.

Two consequences, and the second is worse than the first:

- `completed_worker_durations` selects on `status='ok'` and the timestamps
  being present. P's row was now `ok` and still carried the **failed**
  attempt's 100s duration, so a run that did not succeed became a sample in
  the p90 the estimator quotes.
- `model_used` still named the real model, so the A/B contamination guard --
  added in this same phase, and which looks for `model_used = 'cache'` --
  could not see the run at all. The guard I wrote to stop a warm cache
  silently improving the headline had a hole in exactly the case where the
  cache was doing something unusual.

Fixed by making the conflict branch a full overwrite: NULL both timestamps,
set `model_used`, clear the taint columns. The regression test constructs the
three-step order above and fails without it.

*Lesson kept: I wrote the INSERT and the ON CONFLICT clause in one statement
and tested only the INSERT. An upsert is two code paths wearing one set of
parentheses, and the happy path never reaches the second one.*

## 43. The probe server was still writing to its own evidence

`probe/probe.log` is committed -- it is the recorded capability-probe session
`probe/RESULTS.md` is derived from. `probe/probe_server.py` is also still
registered in the client, so it starts whenever the client does, and it logged
to that same file.

Three costs, none of which announced itself:

- the repo was permanently dirty, so "working tree clean" stopped meaning
  anything;
- the artifact the results cite kept changing under them;
- and the open handle made `git stash` fail outright -- `unable to unlink old
  'probe/probe.log'` -- mid-way through, leaving changes in a stash entry that
  `stash pop` then refused to apply. Recovering meant pulling the two files I
  wanted back out of the stash by hand.

That last one is how it was found. It had been true since Phase 1.

Fixed by pointing the live server at `probe/probe.live.log` (gitignored) and
restoring the committed file. `PROBE_LOG` still overrides. Evidence belongs in
git and must not move; a live log belongs somewhere git never looks.

*Lesson kept: this is the same server whose log tainted every task in the
first real fan-out (section 28). Both times the cause was one file being asked
to be two things at once -- a record and a running process's scratch space.*

## 44. The calibration gate fired, twice, and I stopped tuning

The Phase 6 plan put a gate before the spend: run each task once, solo, and if
it scores above 90% or below 30%, rewrite it before measuring anything. A
ceiling measures nothing, which is the failure `bench/RESULTS.md` section 3
already documents -- 23 of 23 graded runs passed, so that bench could rank
cost and could not rank quality.

**First calibration: 100% on both tasks.** The reasons were mine, not the
model's:

- the audit prompt listed the defect categories -- "inverted conditions,
  swapped operators, off-by-one errors, wrong constants" -- which turns an
  audit into a search-and-replace;
- every extraction question named the module its answer was in, so "In db.py,
  what is WRITE_ATTEMPTS" is a grep rather than a question;
- every module was known to contain defects, so an agent that assumes each
  file is broken and reports its most suspicious lines is right by
  construction.

Rewritten: the defect categories removed, the counts removed, one module left
**clean** as a control so precision has something to measure, module names
dropped from every question, and a third of the questions replaced with
answers that have to be derived rather than found (total backoff across the
retry series; how many `file_hashes` rows a 2-read 1-write task produces).

**Second calibration: 100% on both, again.** But not identically -- the audit's
parent input went from 147,556 tokens to **411,188** and its wall clock from
50s to 158s. The model worked nearly three times as hard for the same score.

At which point I stopped. A third pass would be tuning the task until it
produced the answer I was hoping for, and there is no version of that which is
honest. What the two rounds actually established is worth more than a
separable number: **on work this model does perfectly either way, the accuracy
axis cannot separate the arms** -- and the ceiling is on arm A, the control, so
any drop in the delegating arm is still measurable against it.

`aggregate.ceiling_warning` prints CEILING into the results whenever both arms
land above 90%, so a comparison that measures nothing says so in the report
rather than in my head.

*Lesson kept: the gate did its job by refusing to let me publish 100% against
100% as "no difference". The thing it could not do was tell me when to stop
making the task harder -- that had to be a decision about honesty rather than
about calibration.*

## 45. The estimate for my own measurement was out by two

Budgeted in the plan: ~3.3M tokens for 20 runs. Measured after one real
calibration run, the revised figure was ~6.4M -- almost exactly double.

The error was using the published A/B numbers (a four-module documentation
task, ~99k parent input solo) as the model for a four-module *audit*. An audit
re-reads, cross-references and reasons about every line; documentation
summarises it once. The audit arm measured **411,188** parent input tokens,
4.1x the documentation task on the same four files.

Cut to four repeats rather than five, and reported rather than absorbed. This
is the same rule as `propose_plan`'s estimator: a number whose provenance is
invisible is one nobody can challenge -- and an estimate quoted from a
different task shape is exactly that.

*Lesson kept: I have now been wrong about a duration estimate (section 24), a
p90 (section 0.3), and a token budget, each time by reusing a measurement from
a task that looked similar. The shape of the work matters more than the size
of the input.*

