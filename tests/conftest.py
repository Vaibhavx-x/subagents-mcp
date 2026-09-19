"""Shared fixtures.

Tests build their own throwaway workspace under tmp_path rather than pointing
at the repo, so they neither depend on repo contents nor can touch them.
"""

from __future__ import annotations

import json
import sys
from dataclasses import replace
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from subagents.config import load_config  # noqa: E402
from subagents.models import Task  # noqa: E402


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    """A realistic little workspace: a package, tests, and a README."""
    root = tmp_path / "ws"
    (root / "pkg").mkdir(parents=True)
    (root / "pkg" / "config.py").write_text("def fetch_cfg():\n    return {}\n", encoding="utf-8")
    (root / "pkg" / "server.py").write_text("from .config import fetch_cfg\n", encoding="utf-8")
    (root / "README.md").write_text("# ws\n", encoding="utf-8")
    (root / "test_config.py").write_text("def test_x():\n    assert True\n", encoding="utf-8")
    return root.resolve()


@pytest.fixture
def cfg(workspace: Path, tmp_path: Path):
    """Config allowlisting only the throwaway workspace, with its own database."""
    return replace(
        load_config(),
        allowed_roots=(workspace,),
        db_path=tmp_path / "plans.db",
        max_parallel=4,
        worker_timeout_s=600,
        plan_ttl_s=900,
    )


def make_task(ref: str, reads=(), writes=(), instruction: str = "do the thing", model: str = "m") -> Task:
    return Task(
        task_ref=ref,
        instruction=instruction,
        reads=tuple(sorted(reads)),
        writes=tuple(sorted(writes)),
        model=model,
    )


def tasks_json(*entries: dict) -> str:
    return json.dumps(list(entries))


def task_entry(ref: str, reads=(), writes=(), instruction: str = "do the thing") -> dict:
    return {
        "task_ref": ref,
        "instruction": instruction,
        "reads": list(reads),
        "writes": list(writes),
    }
