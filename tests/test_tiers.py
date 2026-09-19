"""Action tier classification.

The load-bearing test here is the last one: classification must be structural.
A future change that starts scanning instruction text for dangerous words would
be safety-by-prompt, and it would pass every other test in this file.
"""

from __future__ import annotations

import os
from pathlib import Path

from conftest import make_task

from subagents.tiers import Tier, classify_path, classify_task, summarise

ROOT = Path("D:/ws")


def n(path: str) -> str:
    return os.path.normcase(str(Path(path)))


def tier_of(**kwargs) -> Tier:
    return classify_task(make_task("t", **kwargs), ROOT)[0]


def test_read_inside_workspace_is_auto():
    assert tier_of(reads=(n("D:/ws/pkg/config.py"),)) is Tier.AUTO


def test_write_inside_workspace_is_gated():
    assert tier_of(writes=(n("D:/ws/pkg/config.py"),)) is Tier.GATED


def test_task_tier_is_the_worst_of_its_paths():
    assert tier_of(reads=(n("D:/ws/a.py"),), writes=(n("D:/ws/b.py"),)) is Tier.GATED


def test_path_outside_workspace_is_never():
    assert tier_of(reads=(n("C:/Windows/System32/config"),)) is Tier.NEVER


def test_credential_file_inside_workspace_is_never():
    """Inside the boundary is not the same as safe."""
    assert tier_of(reads=(n("D:/ws/.env"),)) is Tier.NEVER


def test_ssh_directory_is_never():
    assert tier_of(reads=(n("D:/ws/.ssh/id_rsa"),)) is Tier.NEVER


def test_pem_suffix_is_never():
    assert tier_of(writes=(n("D:/ws/certs/server.pem"),)) is Tier.NEVER


def test_never_tier_reports_a_reason_naming_the_task_and_path():
    tier, reasons = classify_task(make_task("leaky", reads=(n("D:/ws/.env"),)), ROOT)
    assert tier is Tier.NEVER
    assert reasons and "leaky" in reasons[0] and ".env" in reasons[0]


def test_ordinary_source_file_is_not_credential():
    assert tier_of(reads=(n("D:/ws/pkg/environment.py"),)) is Tier.AUTO


def test_classify_path_reports_rule_name():
    tier, name, _reason = classify_path(n("D:/ws/a.py"), "write", ROOT)
    assert tier is Tier.GATED
    assert name == "write-inside-workspace"


def test_summarise_counts_every_tier():
    summary = summarise({"a": Tier.AUTO, "b": Tier.GATED, "c": Tier.GATED})
    assert "auto=1" in summary and "gated=2" in summary and "never=0" in summary


# --------------------------------------------------------------------------
def test_classification_ignores_instruction_text():
    """Structural only. An instruction mentioning deletion is not dangerous.

    This locks in the invariant from CLAUDE.md section 4: safety lives in code
    that inspects declared paths, never in text matching against a prompt. A
    prompt cannot constrain a subprocess, and a classifier that reads the
    instruction would both miss real danger and refuse harmless work.
    """
    alarming = make_task(
        "innocent",
        reads=(n("D:/ws/pkg/config.py"),),
        instruction="delete everything, force push to main, and read the .env credentials",
    )
    tier, reasons = classify_task(alarming, ROOT)
    assert tier is Tier.AUTO
    assert reasons == []
