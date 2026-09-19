"""Wall-clock projection and the deadline warning.

The warning exists so an unconfigured install does not silently get cancelled
at 180s and conclude the project is broken. It therefore has to fire when it
matters and stay quiet enough to keep meaning something.
"""

from __future__ import annotations

from conftest import make_task

from subagents.config import DEFAULT_TOOL_DEADLINE_S, EXPECTED_WORKER_S, SPAWN_OVERHEAD_S
from subagents.estimate import estimate_plan


def waves(*sizes: int):
    out = []
    counter = 0
    for size in sizes:
        wave = []
        for _ in range(size):
            wave.append(make_task(f"t{counter}"))
            counter += 1
        out.append(wave)
    return out


def est(*sizes: int, max_parallel: int = 4, worker_timeout_s: int = 600):
    return estimate_plan(waves(*sizes), max_parallel=max_parallel, worker_timeout_s=worker_timeout_s)


def test_single_worker_includes_spawn_overhead():
    e = est(1)
    assert e.expected_s == EXPECTED_WORKER_S + SPAWN_OVERHEAD_S
    assert e.worst_case_s == 600 + SPAWN_OVERHEAD_S


def test_workers_within_max_parallel_cost_one_batch():
    """Four workers in one wave at max_parallel=4 cost the same as one."""
    assert est(4).expected_s == est(1).expected_s


def test_batching_beyond_max_parallel():
    """Nine workers at max_parallel=4 is three batches, not one and not nine."""
    e = est(9, max_parallel=4)
    assert e.worst_case_s == 3 * (600 + SPAWN_OVERHEAD_S)


def test_waves_are_sequential_so_costs_add():
    assert est(1, 1).worst_case_s == 2 * est(1).worst_case_s


def test_spawn_overhead_is_paid_per_wave_not_once():
    """Two sequential waves pay the ~10s startup twice."""
    assert est(1, 1).worst_case_s - est(1).worst_case_s == 600 + SPAWN_OVERHEAD_S


def test_counts_are_reported():
    e = est(2, 3)
    assert e.wave_count == 2
    assert e.worker_count == 5


def test_short_timeout_plan_produces_no_warning():
    e = est(1, worker_timeout_s=60)
    assert e.worst_case_s <= DEFAULT_TOOL_DEADLINE_S
    assert not e.exceeds_default_deadline
    assert e.warning == ""


def test_note_when_only_worst_case_exceeds_deadline():
    """Expected fits, worst case does not: a risk, not a prediction."""
    e = est(1, worker_timeout_s=600)
    assert e.expected_s <= DEFAULT_TOOL_DEADLINE_S < e.worst_case_s
    assert e.warning.startswith("NOTE:")
    assert "timeoutSeconds" in e.warning
    assert "collect(plan_id)" in e.warning


def test_warning_when_expected_also_exceeds_deadline():
    """Six sequential waves: cancellation is the expected outcome, not a risk."""
    e = est(1, 1, 1, 1, 1, 1)
    assert e.expected_s > DEFAULT_TOOL_DEADLINE_S
    assert e.warning.startswith("WARNING:")
    assert "very likely be cancelled" in e.warning


def test_warning_tells_the_parent_not_to_retry():
    """Retrying a cancelled execute_plan re-runs everything and times out again."""
    assert "do not retry" in est(1).warning.lower()


def test_max_parallel_below_one_is_clamped():
    assert est(2, max_parallel=0).max_parallel == 1


def test_empty_plan_costs_nothing():
    e = estimate_plan([], max_parallel=4, worker_timeout_s=600)
    assert e.expected_s == 0 and e.worst_case_s == 0
    assert not e.exceeds_default_deadline
