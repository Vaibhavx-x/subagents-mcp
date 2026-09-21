"""The accuracy harness: what it records, and what it refuses to record.

The bench shipped six defects because the harness had no tests while the
product code did. The rule since: measurement code is product code.

Most of what is asserted here is the void/zero line. Getting it wrong in the
comfortable direction -- voiding every run that scored badly -- would produce a
report where both arms look excellent and the difference between them has been
quietly deleted.
"""

from __future__ import annotations

import csv
import sys
from pathlib import Path

import pytest
from conftest import REPO_ROOT

# Imported by package path, never by bare name. `bench/ab/` and
# `bench/quality/` both define `harness` and `aggregate`, so a bare
# `import aggregate` resolves to whichever directory landed on sys.path first
# -- which depends on test collection order and fails somewhere else entirely.

from bench.quality.aggregate import (  # noqa: E402
    ceiling_warning,
    counted,
    deltas,
    per_turn,
    summarise_task,
    verdict,
)
from bench.quality.harness import (  # noqa: E402
    CSV_COLUMNS,
    MODULES,
    RunRecord,
    assignment_block,
    classify_void,
)
from bench.quality.run_quality import append_row  # noqa: E402


def record(**kw) -> RunRecord:
    base = dict(run_id="r1", task="audit", arm="A", repeat=1,
                server_enabled_observed=False, agy_status="SUCCESS")
    base.update(kw)
    return RunRecord(**base)


def row(arm: str, **kw) -> dict:
    base = {col: "" for col in CSV_COLUMNS}
    base.update(arm=arm, task="audit", void_reason="", parse_ok="True",
                hit_timeout="False", num_turns="4")
    base.update({k: str(v) for k, v in kw.items()})
    return base


# ------------------------------------------------------------------ CSV schema
def test_every_declared_column_is_populated(tmp_path):
    """A column that silently stays empty is a measurement nobody notices is
    missing until the table is published."""
    written = record(parent_in=1000, parent_out=50, num_turns=3, wall_s=12.3,
                     accuracy=0.5, score_hits=8, score_total=16).as_row()
    assert list(written) == CSV_COLUMNS

    out = tmp_path / "runs.csv"
    append_row(out, record(parent_in=1, num_turns=1))
    with open(out, newline="", encoding="utf-8") as handle:
        assert list(csv.DictReader(handle))[0].keys() == set(CSV_COLUMNS) | set()


def test_a_dash_output_path_discards_rows(tmp_path):
    """`--out -` is how preflight rehearses the harness. A rehearsal that
    appended to the real measurement would contaminate it with fake runs."""
    append_row(Path("-"), record())
    assert not (tmp_path / "-").exists()


# ------------------------------------------------------- void, and NOT void
def test_a_zero_score_is_not_void():
    """The line the whole report rests on. A run that scored nothing still
    measured something: that this arm scored nothing."""
    assert classify_void(record(accuracy=0.0, score_hits=0, score_total=16)) == ""


def test_unparseable_output_is_not_void():
    """If arm A follows the output format worse than arm C -- plausible, since
    each arm-C worker gets one simple file -- then voiding those runs deletes
    a real finding about the tool. It is reported as a rate instead."""
    assert classify_void(record(parse_ok=False, accuracy=0.0)) == ""


def test_a_timeout_is_not_void():
    """Arm A doing four modules serially is genuinely likelier to hit a wall
    than arm C doing them at once. That is a result, not an inconvenience."""
    assert classify_void(record(hit_timeout=True, accuracy=0.3)) == ""


def test_the_solo_arm_with_the_server_enabled_is_void():
    assert classify_void(record(arm="A", server_enabled_observed=True)) \
        == "server_was_enabled_for_solo_arm"


def test_the_delegating_arm_that_did_not_delegate_is_void():
    """The parent did the work itself -- NOTES section 22. Interesting, and
    not arm C."""
    assert classify_void(record(arm="C", server_enabled_observed=True,
                                delegated=False)) == "did_not_delegate"


def test_the_delegating_arm_with_the_wrong_worker_count_is_void():
    got = classify_void(record(arm="C", server_enabled_observed=True,
                               delegated=True, worker_count=2))
    assert f"expected {len(MODULES)} workers" in got


def test_a_cache_hit_voids_the_run():
    """Every run materialises its own workspace and the key covers the absolute
    root, so this should be unreachable -- which is why it is checked."""
    got = classify_void(record(arm="C", server_enabled_observed=True,
                               delegated=True, worker_count=len(MODULES),
                               cached_hits=1))
    assert "cache served 1" in got


def test_agy_failing_outright_is_void():
    """Infrastructure, not quality: the parent never got to attempt the task."""
    assert "ERROR" in classify_void(record(agy_status="ERROR"))


def test_a_good_run_is_not_void():
    assert classify_void(record(arm="C", server_enabled_observed=True,
                                delegated=True, worker_count=len(MODULES),
                                accuracy=0.6)) == ""


# ------------------------------------------------------------------ aggregation
def test_a_void_run_never_reaches_a_median():
    """The D1 regression, at the new schema: void runs quoted as results."""
    rows = [row("A", accuracy=0.8), row("A", accuracy=0.2),
            {**row("A", accuracy=1.0), "void_reason": "did_not_delegate"}]
    assert len(counted(rows, "A")) == 2
    assert summarise_task(rows)["A"]["accuracy"] == 0.5


def test_a_zero_scoring_run_does_reach_the_median():
    rows = [row("A", accuracy=0.8), row("A", accuracy=0.0)]
    assert summarise_task(rows)["A"]["accuracy"] == 0.4


def test_input_per_turn_is_computed_per_run_not_from_two_medians():
    """median(parent_in) / median(num_turns) mixes numbers from different runs
    and can land outside every run's actual value."""
    rows = [row("A", parent_in=100_000, num_turns=10),
            row("A", parent_in=20_000, num_turns=1)]
    assert sorted(per_turn(counted(rows, "A"))) == [10_000.0, 20_000.0]


def test_overlapping_ranges_are_not_reported_as_an_effect():
    rows = [row("A", accuracy=0.5), row("A", accuracy=0.9),
            row("C", accuracy=0.6), row("C", accuracy=0.8)]
    summary = summarise_task(rows)
    assert "not measurable" in verdict(summary, "accuracy")


def test_separated_ranges_are_reported_as_an_effect():
    rows = [row("A", accuracy=0.2), row("A", accuracy=0.3),
            row("C", accuracy=0.7), row("C", accuracy=0.8)]
    assert verdict(summarise_task(rows), "accuracy") == "ranges do not overlap"


def test_delegation_scoring_worse_is_reported_as_a_negative_delta():
    """The prediction for the extraction task. The report must be able to say
    the tool made things worse."""
    rows = [row("A", accuracy=0.8), row("A", accuracy=0.8),
            row("C", accuracy=0.5), row("C", accuracy=0.5)]
    assert deltas(summarise_task(rows))["accuracy_delta"] == pytest.approx(-0.3)


def test_a_ceiling_is_called_out_rather_than_reported_as_no_difference():
    """bench/RESULTS.md section 3: 23 of 23 passed, so that bench could rank
    cost and not quality. A repeat of that must announce itself."""
    rows = [row("A", accuracy=0.95), row("C", accuracy=0.97)]
    assert "CEILING" in ceiling_warning(summarise_task(rows))


def test_a_floor_is_called_out_too():
    rows = [row("A", accuracy=0.05), row("C", accuracy=0.0)]
    assert "FLOOR" in ceiling_warning(summarise_task(rows))


def test_a_usable_spread_is_not_flagged():
    rows = [row("A", accuracy=0.4), row("C", accuracy=0.6)]
    assert ceiling_warning(summarise_task(rows)) == ""


def test_the_format_failure_rate_is_reported_per_arm():
    rows = [{**row("A"), "parse_ok": "True"}, {**row("A"), "parse_ok": "False"}]
    assert summarise_task(rows)["A"]["parse_rate"] == 0.5


def test_timeouts_are_counted_beside_the_medians():
    rows = [{**row("A"), "hit_timeout": "True"}, {**row("A"), "hit_timeout": "False"}]
    assert summarise_task(rows)["A"]["timeouts"] == 1


# -------------------------------------------------------------------- prompts
def test_cross_module_questions_are_routed_by_the_module_they_name():
    """Not by where the answer lives. A parent delegating by module has no way
    to know Q11's answer is in worker.py when the question is about
    execution.py -- pre-routing it would delete the thing being tested."""
    key = {"questions": [
        {"id": 1, "module": "db", "text": "in db?"},
        {"id": 11, "module": "worker", "spans": "execution", "text": "in execution?"},
    ]}
    block = assignment_block(key)
    execution_section = block.split("execution --")[1]
    assert "Q11" in execution_section
    assert "Q11" not in block.split("execution --")[0]
