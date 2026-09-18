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
