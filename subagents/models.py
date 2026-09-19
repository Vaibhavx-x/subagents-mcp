"""Normalised plan structures.

A Task here has already been validated: its task_ref matches the charset, and
its reads/writes are absolute, resolved, normcased paths proven to sit inside
the workspace root. Everything downstream (digest, tiers, waves) consumes this
form, so no module re-derives paths and they cannot drift apart.
"""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Task:
    task_ref: str
    instruction: str
    reads: tuple[str, ...]      # sorted, absolute, REAL case (for display)
    writes: tuple[str, ...]     # sorted, absolute, REAL case (for display)
    model: str

    # Comparison and hashing use the normcased view, because Windows paths are
    # case-insensitive: 'README.md' and 'readme.md' are the same file and must
    # collide in conflict detection and produce the same digest. Display uses
    # the real case, so a human reading a plan sees the actual filename.
    @property
    def reads_norm(self) -> tuple[str, ...]:
        return tuple(sorted(os.path.normcase(p) for p in self.reads))

    @property
    def writes_norm(self) -> tuple[str, ...]:
        return tuple(sorted(os.path.normcase(p) for p in self.writes))

    @property
    def declares_no_reads(self) -> bool:
        """Taint verification is vacuous for such a task -- the post-run hash
        compares nothing, so a result would look verified while never checked."""
        return not self.reads
