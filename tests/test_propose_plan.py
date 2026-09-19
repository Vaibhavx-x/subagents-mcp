"""propose_plan through the real MCP tool interface.

These drive the in-memory Client exactly as the agy client would, so they cover
argument marshalling, the tool schema, and how refusals surface -- none of which
the unit tests touch.
"""

from __future__ import annotations

import json
from pathlib import Path

import anyio
import pytest
from conftest import task_entry, tasks_json

from mcp import Client

import server


@pytest.fixture(autouse=True)
def _use_test_config(cfg, monkeypatch):
    """server.CONFIG is read at import time from the real environment."""
    monkeypatch.setattr(server, "CONFIG", cfg)


def call(tasks: str, root: Path | str):
    async def main():
        async with Client(server.mcp) as client:
            return await client.call_tool(
                "propose_plan",
                {"tasks_json": tasks, "workspace_root": str(root)},
            )

    return anyio.run(main)


def text_of(result) -> str:
    return "".join(getattr(block, "text", "") for block in result.content)


def digest_from(text: str) -> str:
    for line in text.splitlines():
        if "plan_digest" in line:
            return line.split(":", 1)[1].strip()
    raise AssertionError(f"no plan_digest in:\n{text}")


# ------------------------------------------------------------------ happy
def test_tool_is_listed_with_a_description():
    async def main():
        async with Client(server.mcp) as client:
            return await client.list_tools()

    listed = anyio.run(main)
    tools = listed.tools if hasattr(listed, "tools") else listed
    by_name = {t.name: t for t in tools}
    assert "propose_plan" in by_name
    assert (by_name["propose_plan"].description or "").strip()


def test_happy_path_returns_plan_id_and_digest(workspace):
    result = call(tasks_json(task_entry("fix-auth", reads=["pkg/config.py"], writes=["pkg/server.py"])), workspace)
    assert result.is_error is False, text_of(result)
    text = text_of(result)
    assert "PLAN " in text
    assert "plan_digest" in text
    assert "WAVE 0" in text
    assert "fix-auth" in text


def test_waves_are_rendered_writer_first(workspace):
    text = text_of(
        call(
            tasks_json(
                task_entry("read-it", reads=["pkg/config.py"]),
                task_entry("write-it", writes=["pkg/config.py"]),
            ),
            workspace,
        )
    )
    assert text.index("write-it") < text.index("read-it"), text


def test_identical_input_gives_identical_digest_but_new_plan_id(workspace):
    tasks = tasks_json(task_entry("a", reads=["pkg/config.py"], writes=["README.md"]))
    first, second = text_of(call(tasks, workspace)), text_of(call(tasks, workspace))
    assert digest_from(first) == digest_from(second), "digest must cover the plan"
    assert first.splitlines()[0] != second.splitlines()[0], "plan_id must be per proposal"


def test_plan_is_persisted(workspace, cfg):
    from subagents.db import connect

    text = text_of(call(tasks_json(task_entry("a", reads=["pkg/config.py"])), workspace))
    conn = connect(cfg.db_path)
    try:
        row = conn.execute("SELECT plan_digest, status, wave_count FROM plans").fetchone()
    finally:
        conn.close()
    assert row["plan_digest"] == digest_from(text)
    assert row["status"] == "proposed"
    assert row["wave_count"] == 1


# --------------------------------------------------------------- refusals
def test_path_outside_workspace_is_refused_with_a_reason(workspace):
    result = call(tasks_json(task_entry("a", reads=["../escape.txt"])), workspace)
    assert result.is_error is True
    message = text_of(result)
    assert "REFUSED" in message
    assert "escape" in message.lower()


def test_duplicate_task_ref_is_refused(workspace):
    result = call(
        tasks_json(task_entry("dup", reads=["README.md"]), task_entry("dup", reads=["pkg/config.py"])),
        workspace,
    )
    assert result.is_error is True
    assert "duplicate" in text_of(result).lower()


def test_write_write_collision_is_refused(workspace):
    result = call(
        tasks_json(task_entry("a", writes=["pkg/config.py"]), task_entry("b", writes=["pkg/config.py"])),
        workspace,
    )
    assert result.is_error is True
    assert "write" in text_of(result).lower()


def test_cycle_is_refused(workspace):
    result = call(
        tasks_json(
            task_entry("a", reads=["pkg/config.py"], writes=["pkg/server.py"]),
            task_entry("b", reads=["pkg/server.py"], writes=["pkg/config.py"]),
        ),
        workspace,
    )
    assert result.is_error is True
    assert "cycle" in text_of(result).lower()


def test_credential_path_is_refused(workspace):
    (workspace / ".env").write_text("SECRET=x\n", encoding="utf-8")
    result = call(tasks_json(task_entry("a", reads=[".env"])), workspace)
    assert result.is_error is True
    assert "never-tier" in text_of(result).lower()


def test_workspace_root_outside_allowlist_is_refused(workspace, tmp_path):
    other = tmp_path / "elsewhere"
    other.mkdir()
    result = call(tasks_json(task_entry("a", reads=["x.py"])), other)
    assert result.is_error is True
    assert "allowlist" in text_of(result).lower()


def test_invalid_task_ref_is_refused(workspace):
    result = call(tasks_json(task_entry("Fix_Auth", reads=["README.md"])), workspace)
    assert result.is_error is True
    assert "task_ref" in text_of(result)


# ------------------------------------------------------- malformed input
def test_malformed_json_is_a_clean_error_not_a_traceback(workspace):
    result = call("{not json at all", workspace)
    assert result.is_error is True
    message = text_of(result)
    assert "REFUSED" in message
    assert "Traceback" not in message


def test_empty_task_list_is_refused(workspace):
    result = call("[]", workspace)
    assert result.is_error is True
    assert "no tasks" in text_of(result).lower()


def test_reads_given_as_string_is_refused_with_guidance(workspace):
    raw = json.dumps([{"task_ref": "a", "instruction": "i", "reads": "README.md"}])
    result = call(raw, workspace)
    assert result.is_error is True
    assert "must be a list" in text_of(result)


def test_missing_instruction_is_refused(workspace):
    raw = json.dumps([{"task_ref": "a", "reads": ["README.md"]}])
    result = call(raw, workspace)
    assert result.is_error is True
    assert "instruction" in text_of(result)


def test_tasks_wrapper_object_is_accepted(workspace):
    raw = json.dumps({"tasks": [task_entry("a", reads=["README.md"])]})
    assert call(raw, workspace).is_error is False


# --------------------------------------------------------------- warnings
def test_empty_reads_warns_but_still_succeeds(workspace):
    """A write-only task is legitimate; it just cannot be taint-verified."""
    result = call(tasks_json(task_entry("gen", writes=["CHANGELOG.md"])), workspace)
    assert result.is_error is False, text_of(result)
    text = text_of(result)
    assert "WARNINGS" in text
    assert "no reads" in text
    assert "not be marked verified" in text


def test_deadline_warning_appears_for_a_long_plan(workspace):
    """A multi-wave plan must tell the parent about timeoutSeconds."""
    text = text_of(
        call(
            tasks_json(
                task_entry("a", writes=["pkg/one.py"]),
                task_entry("b", reads=["pkg/one.py"], writes=["pkg/two.py"]),
                task_entry("c", reads=["pkg/two.py"], writes=["pkg/three.py"]),
            ),
            workspace,
        )
    )
    assert "timeoutSeconds" in text
    assert "collect(plan_id)" in text


def test_next_step_names_execute_plan(workspace):
    text = text_of(call(tasks_json(task_entry("a", reads=["README.md"])), workspace))
    assert "execute_plan(" in text
    assert "do not retry" in text.lower()
