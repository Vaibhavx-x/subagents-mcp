"""Wall-clock projection for a proposed plan.

The server cannot read the client's `timeoutSeconds`, but it can size its own
plan and say so. Without that, a default install runs a large fan-out, gets
cancelled at 180s with no explanation, and the project looks broken.

Spawn overhead is real and measured, not padding: agy CLI startup was 8.7-11.9s
(median 9.7s) across 33 benchmark runs, constant across models and tasks. agy's
own `duration_seconds` excludes it. It is paid PER WAVE, not amortised across a
fan-out, because waves run sequentially.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from .config import DEFAULT_TOOL_DEADLINE_S, EXPECTED_WORKER_S, SPAWN_OVERHEAD_S
from .models import Task


@dataclass(frozen=True)
class Estimate:
    expected_s: int
    worst_case_s: int
    wave_count: int
    worker_count: int
    max_parallel: int
    exceeds_default_deadline: bool

    @property
    def expected_exceeds_deadline(self) -> bool:
        return self.expected_s > DEFAULT_TOOL_DEADLINE_S

    @property
    def warning(self) -> str:
        """Warn on worst case, but scale the language to the actual risk.

        With a 600s per-worker timeout every plan has a worst case above 180s,
        so an undifferentiated warning would fire on all of them and stop
        carrying information. The distinction that matters to the parent is
        whether cancellation is LIKELY (the expected time already exceeds the
        deadline) or merely POSSIBLE (only the worst case does).
        """
        if not self.exceeds_default_deadline:
            return ""

        fix = (
            'Set "timeoutSeconds": 900 in mcp_config.json. If this call is '
            "cancelled anyway, call collect(plan_id) -- do not retry execute_plan."
        )
        if self.expected_exceeds_deadline:
            return (
                f"WARNING: this plan will very likely be cancelled. Expected ~{self.expected_s}s "
                f"already exceeds the {DEFAULT_TOOL_DEADLINE_S}s default tool-call deadline "
                f"(worst case ~{self.worst_case_s}s). {fix}"
            )
        return (
            f"NOTE: expected ~{self.expected_s}s is within the {DEFAULT_TOOL_DEADLINE_S}s default "
            f"deadline, but the worst case is ~{self.worst_case_s}s, so a slow worker could be "
            f"cancelled. {fix}"
        )


def _wave_cost(size: int, per_worker_s: int, max_parallel: int) -> int:
    batches = math.ceil(size / max_parallel) if size else 0
    return batches * (per_worker_s + SPAWN_OVERHEAD_S)


def estimate_plan(
    waves: list[list[Task]],
    *,
    max_parallel: int,
    worker_timeout_s: int,
) -> Estimate:
    if max_parallel < 1:
        max_parallel = 1

    expected = sum(_wave_cost(len(w), EXPECTED_WORKER_S, max_parallel) for w in waves)
    worst = sum(_wave_cost(len(w), worker_timeout_s, max_parallel) for w in waves)

    return Estimate(
        expected_s=expected,
        worst_case_s=worst,
        wave_count=len(waves),
        worker_count=sum(len(w) for w in waves),
        max_parallel=max_parallel,
        exceeds_default_deadline=worst > DEFAULT_TOOL_DEADLINE_S,
    )
