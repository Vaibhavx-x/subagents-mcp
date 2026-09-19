"""Reading (and deliberately writing) the client's MCP config.

Every test here uses a throwaway config under tmp_path. Nothing touches the
real ~/.gemini/config/mcp_config.json -- a test that edited a developer's live
client config would be a worse bug than anything it could catch.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from subagents.client_config import (
    DEFAULT_DEADLINE_S,
    apply_timeout,
    detect_timeout,
    recommended_block,
)


@pytest.fixture
def server_file(tmp_path: Path) -> Path:
    path = tmp_path / "server.py"
    path.write_text("# stand-in for the real server\n", encoding="utf-8")
    return path.resolve()


def write_config(tmp_path: Path, servers: dict) -> Path:
    path = tmp_path / "mcp_config.json"
    path.write_text(json.dumps({"mcpServers": servers}, indent=2), encoding="utf-8")
    return path


# ------------------------------------------------------------------ detect
def test_finds_own_entry_by_args_path(tmp_path, server_file):
    cfg = write_config(tmp_path, {"subagents": {"args": [str(server_file)], "timeoutSeconds": 900}})
    found = detect_timeout(server_file, cfg)
    assert found.known and found.server_name == "subagents"
    assert found.timeout_s == 900 and found.is_explicit
    assert found.effective_s == 900


def test_matches_regardless_of_path_spelling(tmp_path, server_file):
    """Configs are hand-written, so forward slashes are likely on Windows."""
    cfg = write_config(tmp_path, {"s": {"args": [str(server_file).replace("\\", "/")]}})
    assert detect_timeout(server_file, cfg).known


def test_ignores_other_servers(tmp_path, server_file):
    cfg = write_config(
        tmp_path,
        {
            "probe": {"args": ["D:/elsewhere/probe.py"], "timeoutSeconds": 30},
            "subagents": {"args": [str(server_file)], "timeoutSeconds": 900},
        },
    )
    found = detect_timeout(server_file, cfg)
    assert found.server_name == "subagents" and found.timeout_s == 900


def test_missing_timeout_is_unset_not_zero(tmp_path, server_file):
    cfg = write_config(tmp_path, {"subagents": {"args": [str(server_file)]}})
    found = detect_timeout(server_file, cfg)
    assert found.known
    assert found.timeout_s is None and not found.is_explicit
    assert found.effective_s == DEFAULT_DEADLINE_S


def test_unregistered_server_is_known_false(tmp_path, server_file):
    cfg = write_config(tmp_path, {"other": {"args": ["D:/elsewhere/x.py"]}})
    found = detect_timeout(server_file, cfg)
    assert found.readable and not found.known
    assert found.effective_s == DEFAULT_DEADLINE_S


def test_missing_config_is_unreadable(tmp_path, server_file):
    found = detect_timeout(server_file, tmp_path / "nope.json")
    assert not found.readable and not found.known
    assert found.effective_s == DEFAULT_DEADLINE_S


def test_malformed_config_is_unreadable_not_a_crash(tmp_path, server_file):
    bad = tmp_path / "mcp_config.json"
    bad.write_text("{ not json", encoding="utf-8")
    assert not detect_timeout(server_file, bad).readable


def test_garbage_entries_do_not_crash_detection(tmp_path, server_file):
    path = tmp_path / "mcp_config.json"
    path.write_text(
        json.dumps({"mcpServers": {"weird": "a string", "nulls": {"args": [None, 7]}}}),
        encoding="utf-8",
    )
    found = detect_timeout(server_file, path)
    assert found.readable and not found.known


def test_float_timeout_is_coerced(tmp_path, server_file):
    cfg = write_config(tmp_path, {"s": {"args": [str(server_file)], "timeoutSeconds": 900.0}})
    assert detect_timeout(server_file, cfg).timeout_s == 900


# ------------------------------------------------------------------- apply
def test_apply_sets_the_timeout_and_backs_up(tmp_path, server_file):
    cfg = write_config(tmp_path, {"subagents": {"args": [str(server_file)]}})
    message = apply_timeout(server_file, 900, cfg)

    updated = json.loads(cfg.read_text(encoding="utf-8"))
    assert updated["mcpServers"]["subagents"]["timeoutSeconds"] == 900
    assert (tmp_path / "mcp_config.json.bak").is_file()
    assert "Restart the agy session" in message


def test_apply_preserves_other_servers_and_keys(tmp_path, server_file):
    """It edits one field; it must not rewrite somebody else's server."""
    cfg = write_config(
        tmp_path,
        {
            "probe": {"args": ["D:/x/probe.py"], "command": "python", "disabled": False},
            "subagents": {"args": [str(server_file)], "cwd": "D:/ws", "forceAllToolsEager": True},
        },
    )
    apply_timeout(server_file, 900, cfg)
    updated = json.loads(cfg.read_text(encoding="utf-8"))["mcpServers"]

    assert updated["probe"] == {"args": ["D:/x/probe.py"], "command": "python", "disabled": False}
    assert updated["subagents"]["cwd"] == "D:/ws"
    assert updated["subagents"]["forceAllToolsEager"] is True


def test_apply_is_idempotent(tmp_path, server_file):
    cfg = write_config(tmp_path, {"s": {"args": [str(server_file)], "timeoutSeconds": 900}})
    assert "nothing changed" in apply_timeout(server_file, 900, cfg)


def test_apply_refuses_when_not_registered(tmp_path, server_file):
    """Inventing an entry means guessing the interpreter and command for
    somebody else's config; refusing and showing the block is safer."""
    cfg = write_config(tmp_path, {"other": {"args": ["D:/x.py"]}})
    with pytest.raises(RuntimeError) as exc:
        apply_timeout(server_file, 900, cfg)
    assert "Add one first" in str(exc.value)


def test_recommended_block_is_valid_json_with_the_required_keys(server_file):
    block = json.loads(recommended_block(server_file))
    entry = block["subagents"]
    assert entry["timeoutSeconds"] == 900
    assert entry["forceAllToolsEager"] is True
    assert entry["args"] == [str(server_file).replace("\\", "/")]
