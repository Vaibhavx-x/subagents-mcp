"""Wall-clock projection and the deadline warning.

The warning exists so an unconfigured install does not silently get cancelled
at 180s and conclude the project is broken. Since the server can now READ the
client's configured deadline, these assert the difference between a fact and a
guess: a definite warning when we know timeoutSeconds is missing, a hedged one
only when the config could not be read at all.
"""

from __future__ import annotations

from pathlib import Path

from conftest import make_task

from subagents.client_config import DEFAULT_DEADLINE_S, ClientTimeout
from subagents.config import EXPECTED_WORKER_S, SPAWN_OVERHEAD_S
from subagents.estimate import estimate_plan

CFG = Path("mcp_config.json")

CONFIGURED = ClientTimeout(CFG, "subagents", 900, readable=True)
UNSET = ClientTimeout(CFG, "subagents", None, readable=True)
UNREADABLE = ClientTimeout(CFG, None, None, readable=False)
UNREGISTERED = ClientTimeout(CFG, None, None, readable=True)


def waves(*sizes: int):
    out, counter = [], 0
    for size in sizes:
        wave = []
        for _ in range(size):
            wave.append(make_task(f"t{counter}"))
            counter += 1
        out.append(wave)
    return out


def est(*sizes: int, max_parallel: int = 4, worker_timeout_s: int = 600, client_timeout=None):
    return estimate_plan(
        waves(*sizes),
        max_parallel=max_parallel,
        worker_timeout_s=worker_timeout_s,
        client_timeout=client_timeout,
    )


# ------------------------------------------------------------------ costing
def test_single_worker_includes_spawn_overhead():
    e = est(1)
    assert e.expected_s == EXPECTED_WORKER_S + SPAWN_OVERHEAD_S
    assert e.worst_case_s == 600 + SPAWN_OVERHEAD_S


def test_workers_within_max_parallel_cost_one_batch():
    assert est(4).expected_s == est(1).expected_s


def test_batching_beyond_max_parallel():
    """Nine workers at max_parallel=4 is three batches, not one and not nine."""
    assert est(9, max_parallel=4).worst_case_s == 3 * (600 + SPAWN_OVERHEAD_S)


def test_waves_are_sequential_so_costs_add():
    assert est(1, 1).worst_case_s == 2 * est(1).worst_case_s


def test_spawn_overhead_is_paid_per_wave_not_once():
    assert est(1, 1).worst_case_s - est(1).worst_case_s == 600 + SPAWN_OVERHEAD_S


def test_counts_are_reported():
    e = est(2, 3)
    assert e.wave_count == 2 and e.worker_count == 5


def test_max_parallel_below_one_is_clamped():
    assert est(2, max_parallel=0).max_parallel == 1


def test_empty_plan_costs_nothing_and_fits():
    e = estimate_plan([], max_parallel=4, worker_timeout_s=600)
    assert e.expected_s == 0 and e.worst_case_s == 0
    assert e.fits_deadline
    assert e.warning == ""


# ----------------------------------------------------------------- deadline
def test_plan_within_configured_deadline_is_silent():
    e = est(1, worker_timeout_s=60, client_timeout=CONFIGURED)
    assert e.fits_deadline
    assert e.warning == ""


def test_configured_deadline_is_used_not_the_180s_default():
    """A 610s worst case fits a configured 900s but not the 180s default."""
    assert est(1, client_timeout=CONFIGURED).fits_deadline
    assert not est(1, client_timeout=UNSET).fits_deadline


def test_unset_timeout_produces_a_definite_warning():
    """We read the config and saw no timeoutSeconds, so do not hedge."""
    w = est(1, client_timeout=UNSET).warning
    assert w.startswith("WARNING:")
    assert "NO timeoutSeconds" in w
    assert "--fix-config" in w
    assert str(DEFAULT_DEADLINE_S) in w


def test_unreadable_config_hedges():
    """We could not read the config, so the deadline is an assumption."""
    w = est(1, client_timeout=UNREADABLE).warning
    assert w.startswith("NOTE:")
    assert "could not be read" in w


def test_unregistered_server_hedges_rather_than_claiming_a_value():
    w = est(1, client_timeout=UNREGISTERED).warning
    assert w.startswith("NOTE:")


def test_no_client_timeout_falls_back_to_the_default_deadline():
    e = est(1)
    assert e.deadline_s == DEFAULT_DEADLINE_S
    assert not e.deadline_known


def test_plan_exceeding_a_configured_deadline_says_so():
    """Configured correctly and still too big: the fix is the plan, not setup."""
    w = est(1, 1, client_timeout=CONFIGURED).warning
    assert w.startswith("WARNING:")
    assert "set in your config" in w
    assert "raise timeoutSeconds" in w or "split" in w
    assert "--fix-config" not in w, "do not tell them to fix what is already set"


def test_warning_distinguishes_likely_from_possible_cancellation():
    fits = est(1, 1, client_timeout=CONFIGURED).warning
    assert "may well complete" in fits

    # 14 sequential waves at ~70s expected each is ~980s, past the 900s deadline.
    doomed = est(*([1] * 14), client_timeout=CONFIGURED).warning
    assert "cancellation is likely" in doomed


def test_every_warning_tells_the_parent_to_collect_not_retry():
    for ct in (UNSET, UNREADABLE, CONFIGURED):
        w = est(1, 1, 1, client_timeout=ct).warning
        assert "collect(plan_id)" in w
        assert "do not retry" in w.lower()


# -------------------------------------------------------------- deadline_note
def test_deadline_note_names_the_source_when_configured():
    note = est(1, client_timeout=CONFIGURED).deadline_note
    assert "900s" in note and "timeoutSeconds" in note


def test_deadline_note_names_the_entry_when_unset():
    note = est(1, client_timeout=UNSET).deadline_note
    assert "180s" in note and "subagents" in note


def test_deadline_note_admits_when_it_could_not_read():
    assert "could not read" in est(1, client_timeout=UNREADABLE).deadline_note


def test_expected_never_exceeds_the_worst_case():
    """Observed live: a 30s worker budget printed "~70s expected, ~40s worst
    case". EXPECTED_WORKER_S is a benchmark median and knows nothing about the
    budget it is being spent under, so it has to be capped by it.

    An expectation above the ceiling is not a cosmetic error -- the two numbers
    exist so a human can judge whether the plan fits the client deadline, and
    one of them being impossible undermines both.
    """
    from conftest import make_task

    from subagents.estimate import estimate_plan

    e = estimate_plan([[make_task("a")]], max_parallel=4, worker_timeout_s=30)
    assert e.expected_s <= e.worst_case_s
    assert e.worst_case_s == 30 + SPAWN_OVERHEAD_S


def test_a_generous_budget_still_uses_the_benchmark_median():
    """The cap must not drag the estimate down when the budget is realistic."""
    from conftest import make_task

    from subagents.estimate import estimate_plan

    e = estimate_plan([[make_task("a")]], max_parallel=4, worker_timeout_s=600)
    assert e.expected_s == EXPECTED_WORKER_S + SPAWN_OVERHEAD_S


# ------------------------------------------------- p90 from this install's history
def seed_runs(cfg, durations, status="ok"):
    """Write finished runs straight into the database, bypassing execution."""
    import json
    import uuid
    from datetime import datetime, timedelta, timezone

    from subagents.db import init_db, write_transaction

    init_db(cfg.db_path).close()
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    with write_transaction(cfg.db_path) as conn:
        conn.execute(
            "INSERT OR IGNORE INTO plans (id, plan_digest, workspace_root, plan_json,"
            " wave_count, estimated_wall_s, worst_case_wall_s, created_at, expires_at, status)"
            " VALUES ('p1','d','w','{}',1,1,1,'t','t','complete')"
        )
        for index, seconds in enumerate(durations):
            started = base + timedelta(hours=index)
            conn.execute(
                "INSERT INTO runs (id, plan_id, task_ref, wave_index, instruction,"
                " declared_reads, declared_writes, status, started_at, finished_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?)",
                (uuid.uuid4().hex[:12], "p1", f"t{index}", 0, "x",
                 json.dumps([]), json.dumps([]), status,
                 started.isoformat(), (started + timedelta(seconds=seconds)).isoformat()),
            )


def test_a_fresh_install_falls_back_to_the_benchmark_constant(cfg):
    """Nothing about this behaviour changes until there is history worth using."""
    from subagents.estimate import observed_worker_s

    assert observed_worker_s(cfg.db_path) is None


def test_too_few_samples_is_not_enough(cfg):
    """Nine runs say more about which nine tasks happened than about the work."""
    from subagents.estimate import MINIMUM_SAMPLES, observed_worker_s

    seed_runs(cfg, [20.0] * (MINIMUM_SAMPLES - 1))
    assert observed_worker_s(cfg.db_path) is None


def test_enough_samples_gives_the_p90(cfg):
    from subagents.estimate import observed_worker_s

    # 1..20 seconds: nearest-rank p90 of twenty values is the 18th, which is 18.
    seed_runs(cfg, [float(n) for n in range(1, 21)])
    seconds, samples = observed_worker_s(cfg.db_path)
    assert samples == 20
    assert seconds == 18


def test_p90_is_not_the_median(cfg):
    """The two errors are asymmetric: underestimating cancels a plan mid-flight,
    overestimating prints a note nobody minds."""
    import statistics

    from subagents.estimate import observed_worker_s

    # Nearest-rank p90 of twenty samples is the 18th, so the tail has to be
    # wider than two values for the statistic to differ from the median at all.
    durations = [10.0] * 15 + [100.0] * 5
    seed_runs(cfg, durations)
    seconds, _ = observed_worker_s(cfg.db_path)
    assert statistics.median(durations) == 10.0
    assert seconds == 100, "p90 must reach into the slow tail, not sit at the median"


def test_timed_out_workers_are_excluded(cfg):
    """A timed-out worker ran for exactly its budget. Feeding those back in
    makes the estimator predict its own configuration rather than the work."""
    from subagents.estimate import observed_worker_s

    seed_runs(cfg, [600.0] * 20, status="timeout")
    assert observed_worker_s(cfg.db_path) is None, "timeouts must not count as history"


def test_failed_workers_are_excluded(cfg):
    from subagents.estimate import observed_worker_s

    seed_runs(cfg, [3.0] * 20, status="failed")
    assert observed_worker_s(cfg.db_path) is None


def test_the_estimate_uses_local_history_when_it_exists(cfg, workspace):
    from conftest import make_task

    from subagents.estimate import estimate_plan

    seed_runs(cfg, [float(n) for n in range(1, 21)])
    local = estimate_plan([[make_task("a")]], max_parallel=4,
                          worker_timeout_s=600, db_path=cfg.db_path)
    default = estimate_plan([[make_task("a")]], max_parallel=4, worker_timeout_s=600)

    assert local.expected_s != default.expected_s
    assert "p90 of 20 local run(s)" in local.expected_source
    assert "benchmark median" in default.expected_source


def test_the_source_of_the_estimate_is_stated(cfg, workspace):
    """An estimate whose provenance is invisible is one nobody can challenge."""
    from conftest import tasks_json, task_entry
    from subagents.planning import propose, render

    seed_runs(cfg, [float(n) for n in range(1, 21)])
    plan = propose(tasks_json(task_entry("a", reads=["README.md"])), str(workspace), cfg)
    assert "p90 of 20 local run(s)" in render(plan)


def test_a_local_p90_is_still_capped_by_the_worker_budget(cfg, workspace):
    """NOTES §24: expected must never exceed the worst case."""
    from conftest import make_task

    from subagents.estimate import estimate_plan

    seed_runs(cfg, [500.0] * 20)
    e = estimate_plan([[make_task("a")]], max_parallel=4,
                      worker_timeout_s=30, db_path=cfg.db_path)
    assert e.expected_s <= e.worst_case_s
