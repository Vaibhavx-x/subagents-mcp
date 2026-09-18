# NOTES — correcting the draft against the installed SDK

Installed: `mcp` **2.2.0**, Python 3.13.7, Windows 11.
Protocol negotiated in every run below: **2026-07-28**.

---

## Headline: the draft's imports and attribute names were all correct

The draft was written from documentation and never run, so the expectation was
one or two wrong import paths. There were none. `python -c "import
probe_server"` succeeded on the first attempt, with no edits.

I did not take that on faith — every name was checked against the installed
package before I accepted it. Below is what I checked and how.

### Names verified present, not assumed

| Draft wrote | Verified where | How |
| --- | --- | --- |
| `from mcp.server import MCPServer` | `mcp.server` | `dir(mcp.server)` lists `MCPServer` (re-export of `mcp.server.mcpserver.MCPServer`) |
| `AcceptedElicitation`, `CancelledElicitation`, `DeclinedElicitation`, `Context`, `Elicit`, `ElicitationResult`, `Resolve` | `mcp.server.mcpserver` | `dir(mcp.server.mcpserver)` — **all seven present** |
| `InputRequiredResult` | `mcp.types` | `[n for n in dir(mcp.types) if 'Input' in n]` |

The brief warned that `AcceptedElicitation` and friends "may live in
`mcp.server.elicitation` instead". A `mcp.server.elicitation` module does
exist, but `mcp.server.mcpserver` exports all three names directly, so the
draft's import is valid and I left it alone.

### Signatures verified, not assumed

- `Elicit.__init__(self, message: str, schema: type[T])` — positional
  `(message, schema)`. Draft's `Elicit(f"...", Confirm)` matches.
- `Resolve.__init__(self, fn)` — a bare marker. Matches.
- `AcceptedElicitation` fields are `action: Literal["accept"]` and `data`.
  The draft's `case AcceptedElicitation(data=Confirm(proceed=True))` pattern
  relies on `data` being the field name; confirmed via `model_fields`.
- `MCPServer.__init__` accepts `name` positionally and `version` as a keyword,
  and takes **no** transport arguments. `MCPServer.run(transport=...)` is where
  transport lives. Draft is correct on both counts.
- `Context` exposes `protocol_version`, `request_id`, `request_state`,
  `input_responses`, `client_capabilities`, `request_context`, `session`.

### Two dead attribute names in `describe_ctx` (harmless)

`describe_ctx` probes for `client_params` and `meta`. Neither exists on
`Context` in 2.2.0 — both come back `"<absent>"` in the logs. They are read via
`getattr(..., "<absent>")`, so nothing raises. I left them in: the function is
deliberately written not to assume names, and an `<absent>` line is itself
information when this runs against a different build.

`client_capabilities` **is** present and is the one that carries the
elicitation answer for Q2.

---

## Structural check: `Resolve(...)` vs `InputRequiredResult`

The draft's comment on `spin` claims "a tool that uses resolvers may not also
return `InputRequiredResult`". That is not folklore — it is enforced in the SDK
source at registration time:

`site-packages/mcp/server/mcpserver/tools/base.py:91`

```python
if resolved_params and returns_input_required(fn):
    raise InvalidSignature(
        f"Tool {func_name!r} combines Resolve(...) parameters with an InputRequiredResult "
        "return; a call has one input_required channel, so the multi-round flow is driven "
        ...
    )
```

with a second guard at runtime (`base.py:181`) for a body that returns one
without declaring it.

Neither tool violates it: `ask_once` has `Resolve(...)` and returns `str`;
`spin` returns `str | InputRequiredResult` and has no resolver parameters. The
clean five-tool registration is itself the proof — `InvalidSignature` would have
fired at import, not at call time.

---

## `request_state` sealing — checked because it affects `spin`'s counter

`spin` does `int(ctx.request_state)` on a value that went out to the client and
came back. `mcp/server/request_state.py` seals every outgoing value and verifies
every inbound echo (`RequestStateBoundary`, AES-GCM by default), and its module
docstring says handlers "only ever see plaintext they minted". So the round-trip
is transparent to the tool body and `int()` is safe. Confirmed empirically: the
log shows `SPIN round=1` through `SPIN round=5` with the counter incrementing.

---

## The only change I made to `probe_server.py`

Added a module-level `COUNTS: dict[str, int]` with `bump()` and
`reset_counts()`, and one `bump(...)` line at the top of the resolver and of
each tool body. The log lines already implied these counts; the dict just lets
the tests assert on them. No other edits.

---

## Commands used

```bash
python -c "import mcp.server as s; print([n for n in dir(s) if not n.startswith('_')])"
python -c "import mcp.server.mcpserver as m; print([n for n in dir(m) if not n.startswith('_')])"
python -c "import mcp.types as t; print([n for n in dir(t) if 'Input' in n or 'Elicit' in n])"
python -c "from mcp.server.mcpserver import Context; print([n for n in dir(Context) if not n.startswith('_')])"
python -c "import inspect; from mcp.server.mcpserver import MCPServer; print(inspect.signature(MCPServer.run))"
grep -n "InvalidSignature" site-packages/mcp/server/mcpserver/tools/base.py
```
