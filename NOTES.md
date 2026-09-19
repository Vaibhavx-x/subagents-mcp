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
