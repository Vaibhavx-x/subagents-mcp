"""In-process tests for the capability probe.

`Client(mcp)` connects in memory: no subprocess, and it negotiates 2026-07-28
by default. Every assertion here is on a count or a branch, because those
counts are what get compared against the real client's behaviour.
"""

from __future__ import annotations

import os
import tempfile

# Point the server's log somewhere disposable BEFORE importing it: the log path
# is read at import time.
_LOG_FD, _LOG_PATH = tempfile.mkstemp(suffix=".probe.log")
os.close(_LOG_FD)
os.environ["PROBE_LOG"] = _LOG_PATH

import anyio  # noqa: E402
import pytest  # noqa: E402
from mcp import Client, MCPError  # noqa: E402
from mcp.types import ElicitResult, InputRequiredResult  # noqa: E402

import probe_server  # noqa: E402
from probe_server import COUNTS, mcp  # noqa: E402


def read_log() -> str:
    with open(_LOG_PATH, encoding="utf-8", errors="replace") as fh:
        return fh.read()


@pytest.fixture(autouse=True)
def _reset():
    probe_server.reset_counts()
    yield


def make_recorder(action: str, content: dict | None = None):
    """An elicitation_callback that records every call and answers `action`."""
    calls: list = []

    async def callback(context, params):
        calls.append(params)
        return ElicitResult(action=action, content=content)

    return callback, calls


def text_of(result) -> str:
    return "".join(getattr(b, "text", "") for b in result.content)


# ----------------------------------------------------------------- 1. whoami
def test_whoami_returns_and_logs():
    async def main():
        async with Client(mcp) as client:
            return await client.call_tool("whoami", {})

    result = anyio.run(main)

    assert result.is_error is False, text_of(result)
    assert COUNTS.get("whoami") == 1
    assert "=== WHOAMI ===" in read_log()


# ------------------------------------------------- 2. no-round-trip resolver
def test_ask_once_auto_takes_no_round_trip_path():
    """topic='auto' -> resolver answers itself; the client is never asked."""
    callback, calls = make_recorder("accept", {"proceed": True, "note": ""})

    async def main():
        async with Client(mcp, elicitation_callback=callback) as client:
            return await client.call_tool("ask_once", {"topic": "auto"})

    result = anyio.run(main)

    assert len(calls) == 0, f"client was asked {len(calls)} time(s); expected 0"
    assert COUNTS.get("resolver") == 1, COUNTS
    assert COUNTS.get("ask_once_body") == 1, COUNTS
    assert "ACCEPTED proceed=True" in text_of(result)


# ------------------------------------------------------- 3. the MRTR round
def test_ask_once_drives_full_mrtr_round():
    """Resolver twice, body once. This is the assertion that proves the retry.

    A resolver that ran once would mean the question was asked and never
    followed up; a body that ran twice would mean the whole call replayed
    rather than the resolver being re-driven.
    """
    callback, calls = make_recorder("accept", {"proceed": True, "note": ""})

    async def main():
        async with Client(mcp, elicitation_callback=callback) as client:
            return await client.call_tool("ask_once", {"topic": "x"})

    result = anyio.run(main)

    assert len(calls) == 1, f"elicitation callback ran {len(calls)} time(s); expected 1"
    assert COUNTS.get("resolver") == 2, f"resolver ran {COUNTS.get('resolver')} time(s); expected 2 -- {COUNTS}"
    assert COUNTS.get("ask_once_body") == 1, f"body ran {COUNTS.get('ask_once_body')} time(s); expected 1 -- {COUNTS}"
    assert "ACCEPTED proceed=True" in text_of(result)


# ------------------------------------------- 4. decline / cancel reach body
@pytest.mark.parametrize(
    ("action", "verdict"),
    [("decline", "DECLINED"), ("cancel", "CANCELLED")],
)
def test_decline_and_cancel_reach_the_tool_body(action, verdict):
    """ElicitationResult[Confirm] turns a refusal into a value, not an abort."""
    callback, calls = make_recorder(action, None)

    async def main():
        async with Client(mcp, elicitation_callback=callback) as client:
            return await client.call_tool("ask_once", {"topic": "x"})

    result = anyio.run(main)

    assert len(calls) == 1, f"callback ran {len(calls)} time(s)"
    assert COUNTS.get("ask_once_body") == 1, f"body did not run: {COUNTS}"
    assert result.is_error is False, f"refusal aborted the call: {text_of(result)}"
    assert verdict in text_of(result), text_of(result)


# ------------------------------------------------- 5. no callback -> failure
def test_no_elicitation_callback_fails_the_call():
    """A client that cannot be asked is a failed call, not a decline.

    Observed with mcp 2.2.0 in-memory: see `recorded` in the assertion message
    below -- the test records the real code/message/data rather than asserting
    a value from the docs.
    """
    recorded: dict = {}

    async def main():
        async with Client(mcp) as client:  # no elicitation_callback
            with pytest.raises(MCPError) as excinfo:
                await client.call_tool("ask_once", {"topic": "x"})
            err = excinfo.value
            recorded["code"] = getattr(err, "code", None)
            recorded["message"] = str(err)
            recorded["data"] = getattr(err, "data", None)

    anyio.run(main)

    assert recorded, "call_tool did not raise MCPError"
    # The tool body must never have run: there was no way to ask.
    assert COUNTS.get("ask_once_body") is None, COUNTS
    print_safe = f"code={recorded['code']!r} message={recorded['message']!r} data={recorded['data']!r}"
    assert recorded["code"] is not None, print_safe
    # Record the observed shape in the failure message of a deliberate check.
    assert recorded["code"] == -32021, f"expected -32021, got {print_safe}"
    assert "requiredCapabilities" in str(recorded["data"]), print_safe


# ------------------------------------------------------ 6. spin / MRTR cap
def test_spin_returns_input_required_and_increments():
    """Drive the multi-round loop by hand, capping at 5 rounds."""
    seen: list[str] = []

    async def main():
        async with Client(mcp) as client:
            state: str | None = None
            for _ in range(5):
                res = await client.session.call_tool(
                    "spin", {}, request_state=state, allow_input_required=True
                )
                if not isinstance(res, InputRequiredResult):
                    break
                assert res.request_state is not None
                state = res.request_state
                seen.append(state)

    anyio.run(main)

    assert COUNTS.get("spin") == 5, f"spin body ran {COUNTS.get('spin')} time(s); expected 5"
    assert len(seen) == 5, seen
    # request_state round-trips: the server sees its own prior value back, so
    # the counter it decodes increments 1..5 across the rounds.
    log = read_log()
    for n in range(1, 6):
        assert f"SPIN round={n}" in log, f"missing round {n} in log"
