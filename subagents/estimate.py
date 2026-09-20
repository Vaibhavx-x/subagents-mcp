"""Wall-clock projection, checked against the client's ACTUAL deadline.

The server cannot set `timeoutSeconds`, but it can read it, so the deadline
side of this is a fact rather than a guess.

The duration side stays a guess, and deliberately so: a worker may call other
MCP servers, install packages, or run a test suite, none of which a plan can
predict. That asymmetry drives the design -- compare the plan's WORST CASE
(per-worker timeout, which is a real ceiling) against the client's configured
deadline (a real number), rather than betting on the expected time.

Spawn overhead is measured, not padding: agy CLI startup was 8.7-11.9s
(median 9.7s) across 33 benchmark runs, constant across models. It is paid
PER WAVE, since waves run sequentially.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path

from .client_config import DEFAULT_DEADLINE_S, ClientTimeout
from .config import EXPECTED_WORKER_S, SPAWN_OVERHEAD_S
from .db import completed_worker_durations
from .models import Task

# Below this many finished workers the local history says more about which
# three tasks happened to run than about how long work takes here.
MINIMUM_SAMPLES = 10

# p90 rather than the median, because the two errors are not symmetric:
# underestimating gets a plan cancelled at the client deadline mid-flight,
# overestimating prints a note nobody minds.
ESTIMATE_PERCENTILE = 0.9


@dataclass(frozen=True)
class Estimate:
    expected_s: int
    worst_case_s: int
    wave_count: int
    worker_count: int
    max_parallel: int
    deadline_s: int
    deadline_is_explicit: bool
    deadline_known: bool
    config_path: str = ""
    server_name: str = ""
    # Where expected_s came from. An estimate whose provenance is invisible is
    # one nobody can challenge.
    expected_source: str = "benchmark median, no local history"

    @property
    def fits_deadline(self) -> bool:
        return self.worst_case_s <= self.deadline_s

    @property
    def expected_exceeds_deadline(self) -> bool:
        return self.expected_s > self.deadline_s

    @property
    def deadline_note(self) -> str:
        """One line stating what the deadline actually is and where it came from."""
        if self.deadline_is_explicit:
            return f"client deadline : {self.deadline_s}s (timeoutSeconds in {self.config_path})"
        if self.deadline_known:
            return (
                f"client deadline : {DEFAULT_DEADLINE_S}s "
                f"(no timeoutSeconds set for {self.server_name!r} -- this is the default)"
            )
        return f"client deadline : assumed {DEFAULT_DEADLINE_S}s (could not read the client config)"

    @property
    def warning(self) -> str:
        if self.fits_deadline:
            return ""

        # A worker's duration is not predictable, so the honest framing is
        # about the ceiling, not the estimate.
        head = (
            f"this plan's worst case (~{self.worst_case_s}s) exceeds the "
            f"{self.deadline_s}s deadline"
        )
        tail = (
            "If the call is cancelled, call collect(plan_id) for the workers that "
            "finished -- do not retry execute_plan."
        )

        if self.deadline_is_explicit:
            # They configured a deadline and the plan still overruns it. The fix
            # is a smaller plan or a bigger number, not boilerplate about setup.
            likely = (
                f" Expected ~{self.expected_s}s is also over it, so cancellation is likely."
                if self.expected_exceeds_deadline
                else f" Expected ~{self.expected_s}s fits, so it may well complete."
            )
            return (
                f"WARNING: {head}, which is set in your config.{likely} "
                f"Either raise timeoutSeconds, lower max_parallel's workload, or split "
                f"the plan into fewer waves. {tail}"
            )

        if self.deadline_known:
            # Definite: we read the config and there is no timeoutSeconds.
            return (
                f"WARNING: {head}. Your client config ({self.config_path}) has NO "
                f"timeoutSeconds for {self.server_name!r}, so every call is cancelled at "
                f"{DEFAULT_DEADLINE_S}s. Add \"timeoutSeconds\": 900 to that entry and "
                f"restart the session -- or run: python server.py --fix-config. {tail}"
            )

        return (
            f"NOTE: {head} assumed for an unconfigured client. The client config could "
            f"not be read, so this may be wrong. If timeoutSeconds is unset, calls are "
            f"cancelled at {DEFAULT_DEADLINE_S}s. {tail}"
        )


def percentile(values: list[float], fraction: float) -> float:
    """Nearest-rank percentile. No interpolation, no numpy."""
    ordered = sorted(values)
    index = max(0, math.ceil(fraction * len(ordered)) - 1)
    return ordered[index]


def observed_worker_s(db_path: str | Path,
                      *, minimum_samples: int = MINIMUM_SAMPLES) -> tuple[int, int] | None:
    """(seconds, sample count) from this install's own finished workers.

    Returns None when there is not enough history, so a fresh install behaves
    exactly as it did before this existed.
    """
    durations = completed_worker_durations(db_path)
    if len(durations) < minimum_samples:
        return None
    return math.ceil(percentile(durations, ESTIMATE_PERCENTILE)), len(durations)


def _wave_cost(size: int, per_worker_s: int, max_parallel: int) -> int:
    batches = math.ceil(size / max_parallel) if size else 0
    return batches * (per_worker_s + SPAWN_OVERHEAD_S)


def estimate_plan(
    waves: list[list[Task]],
    *,
    max_parallel: int,
    worker_timeout_s: int,
    client_timeout: ClientTimeout | None = None,
    db_path: str | Path | None = None,
) -> Estimate:
    if max_parallel < 1:
        max_parallel = 1

    # This install's own p90 beats a benchmark median measured on someone
    # else's machine against someone else's tasks. Measured 2026-09-20: the
    # constant was 60s and the p90 over 20 real runs was 29s.
    expected_source = "benchmark median, no local history"
    base_expected = EXPECTED_WORKER_S
    if db_path is not None:
        observed = observed_worker_s(db_path)
        if observed is not None:
            base_expected, samples = observed
            expected_source = f"p90 of {samples} local run(s)"

    # A worker cannot run longer than its own deadline, so the expected figure
    # is capped by it. Without the cap, a short worker_timeout produces the
    # nonsense observed on 2026-09-20 with a 30s budget: "~70s expected, ~40s
    # worst case" -- an expectation exceeding the ceiling that makes it
    # impossible. EXPECTED_WORKER_S is a benchmark median and knows nothing
    # about the budget it is being spent under.
    per_worker_expected = min(base_expected, worker_timeout_s)

    expected = sum(_wave_cost(len(w), per_worker_expected, max_parallel) for w in waves)
    worst = sum(_wave_cost(len(w), worker_timeout_s, max_parallel) for w in waves)

    return Estimate(
        expected_s=expected,
        worst_case_s=worst,
        wave_count=len(waves),
        worker_count=sum(len(w) for w in waves),
        max_parallel=max_parallel,
        deadline_s=client_timeout.effective_s if client_timeout else DEFAULT_DEADLINE_S,
        deadline_is_explicit=bool(client_timeout and client_timeout.is_explicit),
        deadline_known=bool(client_timeout and client_timeout.known),
        config_path=str(client_timeout.config_path) if client_timeout else "",
        server_name=client_timeout.server_name or "" if client_timeout else "",
        expected_source=expected_source,
    )
