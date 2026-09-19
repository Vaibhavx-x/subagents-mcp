"""Action tier classification.

This is a RULES TABLE, evaluated in order, first match wins. It is deliberately
not scattered if/else, and it is deliberately not a prompt.

Classification is STRUCTURAL: it looks at declared paths and the operation
(read vs write) and nothing else. It must never inspect instruction text for
dangerous-looking words. "Instruct the worker not to delete files" is not an
enforcement mechanism -- a prompt cannot constrain a process, and a task whose
instruction merely mentions deletion is not thereby dangerous. A test locks
this in so a future well-meaning change cannot quietly add text scanning.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from enum import IntEnum
from pathlib import Path
from typing import Callable

from .models import Task
from .validate import is_inside


class Tier(IntEnum):
    """Ordered by severity so a task's tier is max() over its paths."""

    AUTO = 0     # reads inside the workspace
    GATED = 1    # writes inside declared paths
    NEVER = 2    # refused regardless of approval


# Basenames and directory components that mean credentials, even inside the
# workspace. Compared case-insensitively against every path component.
CREDENTIAL_NAMES = frozenset({
    ".env", ".ssh", "credentials", "credentials.json", "id_rsa", "id_ed25519",
    ".netrc", ".npmrc", ".pypirc", "secrets.json", ".aws", ".gnupg",
})

CREDENTIAL_SUFFIXES = (".pem", ".key", ".pfx", ".p12")


def _is_credential(path: str) -> bool:
    p = Path(path)
    parts = {part.lower() for part in p.parts}
    if parts & CREDENTIAL_NAMES:
        return True
    return p.suffix.lower() in CREDENTIAL_SUFFIXES


@dataclass(frozen=True)
class Rule:
    name: str
    tier: Tier
    reason: str
    # (path, operation, root) -> bool ; operation is "read" or "write"
    matches: Callable[[str, str, Path], bool]


# --- the table. Order is significant: first match wins. --------------------
RULES: tuple[Rule, ...] = (
    Rule(
        name="outside-workspace",
        tier=Tier.NEVER,
        reason="path resolves outside workspace_root",
        matches=lambda path, op, root: not is_inside(Path(path), root),
    ),
    Rule(
        name="credential-path",
        tier=Tier.NEVER,
        reason="path looks like credential material",
        matches=lambda path, op, root: _is_credential(path),
    ),
    Rule(
        name="write-inside-workspace",
        tier=Tier.GATED,
        reason="writes inside a declared path",
        matches=lambda path, op, root: op == "write",
    ),
    Rule(
        name="read-inside-workspace",
        tier=Tier.AUTO,
        reason="reads inside the workspace",
        matches=lambda path, op, root: op == "read",
    ),
)


def classify_path(path: str, operation: str, root: Path) -> tuple[Tier, str, str]:
    """Return (tier, rule_name, reason) for one declared path."""
    for rule in RULES:
        if rule.matches(path, operation, root):
            return rule.tier, rule.name, rule.reason
    return Tier.NEVER, "unclassified", "no rule matched; refusing by default"


def classify_task(task: Task, root: Path) -> tuple[Tier, list[str]]:
    """Return the task's tier and a reason line for anything NEVER-tier."""
    worst = Tier.AUTO
    never_reasons: list[str] = []

    for path in task.reads:
        tier, name, reason = classify_path(path, "read", root)
        worst = max(worst, tier)
        if tier is Tier.NEVER:
            never_reasons.append(f"{task.task_ref}: read {path} -- {reason} [{name}]")

    for path in task.writes:
        tier, name, reason = classify_path(path, "write", root)
        worst = max(worst, tier)
        if tier is Tier.NEVER:
            never_reasons.append(f"{task.task_ref}: write {path} -- {reason} [{name}]")

    return worst, never_reasons


def summarise(tiers: dict[str, Tier]) -> str:
    counts = {t: 0 for t in Tier}
    for tier in tiers.values():
        counts[tier] += 1
    return ", ".join(f"{t.name.lower()}={counts[t]}" for t in Tier)
