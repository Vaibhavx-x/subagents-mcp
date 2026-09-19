"""execute_plan and collect through the real MCP tool interface.

Covers what the unit tests cannot: argument marshalling, the published schema,
how refusals surface, and the parameter ORDER that decides what a human sees in
the approval prompt.
"""

from __future__ import annotations

import anyio
import pytest
from conftest import task_entry, tasks_json

from mcp import Client

import server
import subagents.execution as execution
from subagents.planning import propose
from subagents.worker import WorkerResult

GOOD_SCOPE = "edits pkg/config.py and pkg/server.py; no deletes"


@pytest.fixture(autouse=True)
def _use_test_config(cfg, monkeypatch):
    monkeypatch.setattr(server, "CONFIG", cfg)
    monkeypatch.setattr(server, "WORKER_DEPTH", "")


@pytest.fixture
def fake_worker(monkeypatch):
    """Substitute the real subprocess. Records what was asked for."""
    calls: list[str] = []

    async def runner(task_ref, command, *, timeout_s, cwd, model, env=None):
        calls.append(task_ref)
        return WorkerResult(
            task_ref=task_ref, status="ok", exit_reason="success",
            transcript='{"status":"SUCCESS"}', summary=f"{task_ref} done",
            agy_status="SUCCESS", model_used=model, returncode=0,
            started_at="2026-01-01T00:00:00+00:00",
            finished_at="2026-01-01T00:00:05+00:00",
            tokens_in=1000, tokens_out=42,
        )

    monkeypatch.setattr(execution, "run_worker", runner)
    return calls


def call(tool: str, args: dict):
    async def main():
        async with Client(server.mcp) as client:
            return await client.call_tool(tool, args)

    return anyio.run(main)


def text_of(result) -> str:
    return "".join(getattr(b, "text", "") for b in result.content)


def make_plan(cfg, workspace, *entries):
    return propose(tasks_json(*entries), str(workspace), cfg)


# ------------------------------------------------------------ tool surface
def test_all_three_tools_are_published():
    async def main():
        async with Client(server.mcp) as client:
            return await client.list_tools()

    listed = anyio.run(main)
    tools = listed.tools if hasattr(listed, "tools") else listed
    assert {"propose_plan", "execute_plan", "collect"} <= {t.name for t in tools}


def test_the_human_readable_argument_survives_prompt_truncation():
    """The approval prompt truncates arguments, and NOT in declaration order.

    An earlier version of this test asserted `scope_summary` was the first
    property in our schema. That was true and useless: measured against real
    agy on 2026-09-19, the client rewrites its cached copy of the schema with
    `properties` sorted ALPHABETICALLY (`required` keeps our order), and the
    model emits arguments in that sorted order. The prompt therefore opened

        subagents/execute_plan({"plan_digest":"30bccf8b20d6f83a...

    spending the whole preview on a hash -- strictly worse than the opaque id
    the original ordering was chosen to avoid.

    So what is pinned is the property that actually reaches the human: the
    argument that sorts FIRST must be the readable one. Declaration order is
    kept too, in case a client ever honours it.
    """
    async def main():
        async with Client(server.mcp) as client:
            return await client.list_tools()

    listed = anyio.run(main)
    tools = listed.tools if hasattr(listed, "tools") else listed
    schema = {t.name: t for t in tools}["execute_plan"].input_schema
    names = list(schema["properties"])

    assert sorted(names)[0] == "affects", (
        f"alphabetically first argument is {sorted(names)[0]!r}; the human would "
        "see that one, not the description of the work"
    )
    assert names[0] == "affects"
    assert set(schema["required"]) == {"affects", "plan_id", "plan_digest"}


def test_the_visible_argument_name_is_short():
    """Every character of the key eats the ~40-char preview budget.

    `{"affects":"writes scratch/note_a...` leaves room for paths;
    `{"scope_summary":"writes scr...` spends a third of the budget on the key.
    """
    async def main():
        async with Client(server.mcp) as client:
            return await client.list_tools()

    listed = anyio.run(main)
    tools = listed.tools if hasattr(listed, "tools") else listed
    schema = {t.name: t for t in tools}["execute_plan"].input_schema
    assert len(sorted(schema["properties"])[0]) <= 10


def test_ctx_is_not_exposed_as_a_tool_argument():
    async def main():
        async with Client(server.mcp) as client:
            return await client.list_tools()

    listed = anyio.run(main)
    tools = listed.tools if hasattr(listed, "tools") else listed
    for tool in tools:
        assert "ctx" not in (tool.input_schema.get("properties") or {})


# --------------------------------------------------------------- execution
def test_execute_then_collect(cfg, workspace, fake_worker):
    plan = make_plan(cfg, workspace, task_entry("a", reads=["README.md"]))
    result = call("execute_plan", {
        "affects": GOOD_SCOPE,
        "plan_id": plan.plan_id,
        "plan_digest": plan.plan_digest,
    })
    assert result.is_error is False, text_of(result)
    assert "complete" in text_of(result)
    assert fake_worker == ["a"]

    collected = call("collect", {"plan_id": plan.plan_id})
    assert collected.is_error is False
    body = text_of(collected)
    assert "a" in body and "ok" in body


def test_execution_output_is_a_summary_not_a_transcript(cfg, workspace, fake_worker):
    """The project's entire claim is that the parent carries summaries."""
    plan = make_plan(cfg, workspace, task_entry("a", reads=["README.md"]))
    body = text_of(call("execute_plan", {
        "affects": GOOD_SCOPE, "plan_id": plan.plan_id,
        "plan_digest": plan.plan_digest,
    }))
    # The real id, not the parameter name: the parent has to supply it, and
    # making it copy-pasteable is the difference between one call and two.
    assert f"collect({plan.plan_id})" in body
    assert len(body) < 2000


def test_bad_digest_is_refused(cfg, workspace, fake_worker):
    plan = make_plan(cfg, workspace, task_entry("a", reads=["README.md"]))
    result = call("execute_plan", {
        "affects": GOOD_SCOPE,
        "plan_id": plan.plan_id,
        "plan_digest": "0" * 64,
    })
    assert result.is_error is True
    assert "REFUSED" in text_of(result)
    assert fake_worker == [], "a worker ran despite the refusal"


def test_weak_scope_summary_is_refused(cfg, workspace, fake_worker):
    plan = make_plan(cfg, workspace, task_entry("a", reads=["README.md"]))
    result = call("execute_plan", {
        "affects": "stuff",
        "plan_id": plan.plan_id,
        "plan_digest": plan.plan_digest,
    })
    assert result.is_error is True
    assert "affects" in text_of(result)
    assert fake_worker == []


def test_unknown_plan_is_a_clean_error(cfg, fake_worker):
    result = call("execute_plan", {
        "affects": GOOD_SCOPE, "plan_id": "nope", "plan_digest": "0" * 64,
    })
    assert result.is_error is True
    assert "Traceback" not in text_of(result)


def test_collect_on_unknown_plan_is_a_clean_error(cfg):
    result = call("collect", {"plan_id": "nope"})
    assert result.is_error is True
    assert "NOT FOUND" in text_of(result)


def test_collect_before_execution_explains_itself(cfg, workspace):
    plan = make_plan(cfg, workspace, task_entry("a", reads=["README.md"]))
    body = text_of(call("collect", {"plan_id": plan.plan_id}))
    assert "nothing has run yet" in body.lower()
    assert "execute_plan" in body


# --------------------------------------------------------------- recursion
def test_a_worker_session_cannot_fan_out(cfg, workspace, fake_worker, monkeypatch):
    """A worker keeps every other MCP server, tool and skill -- it just cannot
    launch nested workers, which would make process growth unbounded."""
    monkeypatch.setattr(server, "WORKER_DEPTH", "1")
    plan = make_plan(cfg, workspace, task_entry("a", reads=["README.md"]))

    result = call("execute_plan", {
        "affects": GOOD_SCOPE,
        "plan_id": plan.plan_id,
        "plan_digest": plan.plan_digest,
    })
    assert result.is_error is True
    assert "sub-agent worker" in text_of(result)
    assert fake_worker == []


def test_a_worker_session_can_still_propose(cfg, workspace, monkeypatch):
    """Blocking execution, not reasoning: a worker may still validate a
    decomposition and report it back."""
    monkeypatch.setattr(server, "WORKER_DEPTH", "1")
    result = call("propose_plan", {
        "tasks_json": tasks_json(task_entry("a", reads=["README.md"])),
        "workspace_root": str(workspace),
    })
    assert result.is_error is False, text_of(result)
