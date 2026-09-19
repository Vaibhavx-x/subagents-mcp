"""Refusal and validation errors.

A refusal is not a crash. Every one of these carries a reason the parent agent
can act on -- "refused" without naming what to change is unactionable, and the
parent will simply retry the identical plan.
"""

from __future__ import annotations


class PlanRefused(Exception):
    """The plan is structurally unacceptable and must not execute."""

    def __init__(self, reason: str, detail: str = "") -> None:
        self.reason = reason
        self.detail = detail
        super().__init__(reason if not detail else f"{reason} -- {detail}")


class PathOutsideWorkspace(PlanRefused):
    """A declared path resolves outside the approved workspace root."""
