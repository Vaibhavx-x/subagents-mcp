"""Tests for the measurement code.

`bench/RESULTS.md` §4 lists six defects found by auditing the first bench
harness AFTER it had published numbers. The harness had no tests; the product
code did. These are the same defect classes, guarded before any tokens are
spent rather than after.
"""

from __future__ import annotations

import csv
import json
import sys
from pathlib import Path

import pytest
from conftest import REPO_ROOT

sys.path.insert(0, str(REPO_ROOT / "bench" / "ab"))

from aggregate import counted, deltas, median_of, spread_of, summarise, voided  # noqa: E402
from harness import (  # noqa: E402
    CSV_COLUMNS,
    MIN_DOC_BYTES,
    MODULES,
    RunRecord,
    ServerToggle,
    classify_void,
    prompt_for,
    verify,
)


def record(**kw) -> RunRecord:
    base = dict(run_id="r1", arm="A", repeat=1, verified=True,
                server_enabled_observed=False, files_written=len(MODULES))
    base.update(kw)
    return RunRecord(**base)


# ------------------------------------------------------------------ CSV schema
def test_every_declared_column_is_populated(tmp_path):
    """A column that silently stays empty is a measurement nobody notices is
    missing until the table is already published."""
    row = record(parent_in=1000, parent_out=50, parent_thinking=0,
                 parent_cache_read=10, wall_s=12.3).as_row()

    assert list(row) == CSV_COLUMNS
    path = tmp_path / "runs.csv"
    with open(path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_COLUMNS)
        writer.writeheader()
        writer.writerow(row)

    back = list(csv.DictReader(open(path, newline="", encoding="utf-8")))[0]
    assert back["arm"] == "A" and back["parent_in"] == "1000"


# ------------------------------------------------------- D1: void runs and medians
def test_a_void_run_never_reaches_a_median():
    """Bench defect D1, as a regression test.

    A run voided by a harness bug still has a valid-looking `usage` block. In
    the original bench those figures went into the published table while the
    run's own row said it had failed.
    """
    rows = [
        {"arm": "A", "parent_in": "100", "void_reason": ""},
        {"arm": "A", "parent_in": "100", "void_reason": ""},
        {"arm": "A", "parent_in": "999999", "void_reason": "work not verified"},
    ]
    assert len(counted(rows, "A")) == 2
    assert len(voided(rows, "A")) == 1
    assert median_of(counted(rows, "A"), "parent_in") == 100


def test_the_void_count_is_reported_not_hidden():
    rows = [
        {"arm": "C", "parent_in": "10", "void_reason": ""},
        {"arm": "C", "parent_in": "20", "void_reason": "did_not_delegate"},
    ]
    summary = summarise(rows)
    assert summary["C"]["n"] == 1
    assert summary["C"]["void"] == 1
    assert summary["C"]["void_reasons"] == ["did_not_delegate"]


def test_spread_is_available_for_every_median():
    """Three runs cannot separate an effect smaller than the spread, and a
    median of three hides exactly that."""
    rows = [{"arm": "A", "parent_in": v, "void_reason": ""} for v in ("100", "300", "200")]
    assert median_of(rows, "parent_in") == 200
    assert spread_of(rows, "parent_in") == (100, 300)


def test_a_median_of_nothing_is_none_not_zero():
    """Zero would quietly become the best score in the table."""
    assert median_of([], "parent_in") is None
    assert spread_of([], "parent_in") is None


# --------------------------------------------------- arm integrity (the new D-class)
def test_arm_b_that_delegated_is_void():
    """Interesting, and not arm B. The parent reached for a tool nobody told it
    about, so the run measures something other than what its label says."""
    assert classify_void(record(arm="B", delegated=True), "") == "unexpected_delegation"


def test_arm_c_that_did_not_delegate_is_void():
    assert classify_void(record(arm="C", delegated=False), "") == "did_not_delegate"


def test_arm_c_with_the_wrong_worker_count_is_void():
    voided_reason = classify_void(
        record(arm="C", delegated=True, worker_count=2), ""
    )
    assert "expected 4 workers" in voided_reason


def test_arm_a_with_the_server_still_enabled_is_void():
    """The toggle is read back from the file; if it did not take, the run is
    not arm A whatever the label says."""
    assert classify_void(
        record(arm="A", server_enabled_observed=True), ""
    ) == "server_was_enabled_for_solo_arm"


def test_unverified_work_is_void_with_the_reason_kept():
    reason = classify_void(record(arm="A", verified=False), "worker.md missing")
    assert reason.startswith("work not verified")
    assert "worker.md missing" in reason


def test_a_good_run_is_not_void():
    assert classify_void(record(arm="C", delegated=True, worker_count=4), "") == ""


# ------------------------------------------------------------------ verification
def make_workspace(tmp_path: Path, docs: dict[str, str]) -> Path:
    root = tmp_path / "ws"
    (root / "src").mkdir(parents=True)
    (root / "docs").mkdir()
    for name in MODULES:
        (root / "src" / f"{name}.py").write_text(
            f"def do_{name}():\n    return 1\n", encoding="utf-8"
        )
    for name, text in docs.items():
        (root / "docs" / f"{name}.md").write_text(text, encoding="utf-8")
    return root


def good_doc(name: str) -> str:
    return f"# {name}\n\nThis module defines do_{name}. " + "Detail. " * 60


def test_a_complete_workspace_verifies(tmp_path):
    root = make_workspace(tmp_path, {n: good_doc(n) for n in MODULES})
    written, ok, problems = verify(root)
    assert written == len(MODULES) and ok, problems


def test_a_missing_document_fails_verification(tmp_path):
    docs = {n: good_doc(n) for n in MODULES[:-1]}
    root = make_workspace(tmp_path, docs)
    written, ok, problems = verify(root)
    assert written == len(MODULES) - 1 and not ok
    assert "missing" in problems


def test_a_stub_document_fails_verification(tmp_path):
    """Otherwise a worker that wrote `# worker` scores the same as one that did
    the work, and the arms become uncomparable."""
    docs = {n: good_doc(n) for n in MODULES}
    docs[MODULES[0]] = "# stub\n"
    root = make_workspace(tmp_path, docs)
    _, ok, problems = verify(root)
    assert not ok and f"under {MIN_DOC_BYTES} bytes" in problems


def test_a_document_naming_nothing_from_its_module_fails(tmp_path):
    """Cheap evidence that the file was read rather than the prose invented."""
    docs = {n: good_doc(n) for n in MODULES}
    docs[MODULES[0]] = "# plausible\n\nThis module does things. " + "Padding. " * 60
    root = make_workspace(tmp_path, docs)
    _, ok, problems = verify(root)
    assert not ok and "names none of its module" in problems


# ----------------------------------------------------------------- the prompts
def test_only_the_delegating_arm_is_told_about_the_server():
    """The asymmetry IS the intervention, and it has to be visible in the code
    as plainly as it is disclosed in the results."""
    solo = prompt_for("A", Path("/tmp/ws"))
    registered = prompt_for("B", Path("/tmp/ws"))
    delegating = prompt_for("C", Path("/tmp/ws"))

    assert solo == registered, "arms A and B must receive the identical prompt"
    assert "subagents" not in solo.lower()
    assert "propose_plan" in delegating and "execute_plan" in delegating


def test_both_prompts_ask_for_the_same_deliverable():
    """Compared with whitespace normalised: the prompts are hard-wrapped at
    different points, and a line break must not read as a difference in what
    was asked for."""
    solo = " ".join(prompt_for("A", Path("/tmp/ws")).split())
    delegating = " ".join(prompt_for("C", Path("/tmp/ws")).split())
    for name in MODULES:
        assert name in solo and name in delegating
    for requirement in ("300 characters", "docs/", "Do not modify anything under src/"):
        assert requirement in solo, requirement
        assert requirement in delegating, requirement


# ------------------------------------------------------------------ the toggle
def fake_config(tmp_path: Path, disabled: bool = False) -> Path:
    path = tmp_path / "mcp_config.json"
    path.write_text(json.dumps({
        "mcpServers": {"subagents": {"command": "python", "disabled": disabled}}
    }), encoding="utf-8")
    return path


def test_the_toggle_reads_the_flag_back_rather_than_assuming(tmp_path):
    path = fake_config(tmp_path)
    with ServerToggle(path) as toggle:
        assert toggle.set_enabled(False) is False
        assert json.loads(path.read_text())["mcpServers"]["subagents"]["disabled"] is True
        assert toggle.set_enabled(True) is True


def test_the_config_is_restored_when_the_harness_raises(tmp_path):
    """A measurement that crashes halfway must not leave the user's client
    configured differently than it found it."""
    path = fake_config(tmp_path)
    before = path.read_text(encoding="utf-8")

    with pytest.raises(RuntimeError):
        with ServerToggle(path) as toggle:
            toggle.set_enabled(False)
            raise RuntimeError("boom")

    assert path.read_text(encoding="utf-8") == before


def test_the_backup_is_cleaned_up_on_success(tmp_path):
    path = fake_config(tmp_path)
    with ServerToggle(path) as toggle:
        assert toggle.backup.is_file()
        toggle.set_enabled(False)
    assert not toggle.backup.is_file()


def test_a_missing_config_refuses_to_start(tmp_path):
    with pytest.raises(FileNotFoundError):
        with ServerToggle(tmp_path / "nope.json"):
            pass


def test_an_unknown_server_name_is_an_error_not_a_silent_no_op(tmp_path):
    path = fake_config(tmp_path)
    with ServerToggle(path, name="not-registered") as toggle:
        with pytest.raises(KeyError):
            toggle.set_enabled(True)


# ------------------------------------------------------------------- the deltas
def test_the_three_arms_separate_the_toll_from_the_saving():
    rows = (
        [{"arm": "A", "parent_in": "200000", "worker_in": "0", "void_reason": ""}] * 3
        + [{"arm": "B", "parent_in": "210000", "worker_in": "0", "void_reason": ""}] * 3
        + [{"arm": "C", "parent_in": "60000", "worker_in": "180000", "void_reason": ""}] * 3
    )
    result = deltas(summarise(rows))
    assert result["toll"] == 10000, "registration cost"
    assert result["saving"] == 150000, "delegation saving"
    assert result["net"] == 140000
    assert result["parent_ratio"] == pytest.approx(200000 / 60000)


def test_total_cost_ratio_shows_delegation_costing_more_overall():
    """The honesty constraint, as a test. Four workers re-read context the
    parent already had; the claim is about the parent's WINDOW, not total
    spend, and a report that hides this is dishonest."""
    rows = (
        [{"arm": "A", "parent_in": "200000", "worker_in": "0", "void_reason": ""}]
        + [{"arm": "B", "parent_in": "210000", "worker_in": "0", "void_reason": ""}]
        + [{"arm": "C", "parent_in": "60000", "worker_in": "300000", "void_reason": ""}]
    )
    result = deltas(summarise(rows))
    assert result["total_cost_ratio"] > 1, "total tokens went UP and must be reported"


def test_a_base_less_class_declaration_is_extracted_without_its_colon(tmp_path):
    """Caught by a dry run before any tokens were spent.

    The obvious string surgery -- split on "(", strip the "class " prefix --
    leaves the trailing colon on `class WorkerResult:`, yielding
    `WorkerResult:`. That appears in no prose ever written, so every real run
    would have voided for "naming none of its module's definitions" and the
    measurement would have produced zero usable rows.
    """
    from harness import public_symbols

    source = "class WorkerResult:\n    pass\n\ndef build_command(a, b):\n    return a\n"
    assert public_symbols(source) == ["WorkerResult", "build_command"]


def test_a_document_mentioning_a_base_less_class_verifies(tmp_path):
    root = tmp_path / "ws"
    (root / "src").mkdir(parents=True)
    (root / "docs").mkdir()
    for name in MODULES:
        (root / "src" / f"{name}.py").write_text(
            f"class {name.title()}Result:\n    pass\n", encoding="utf-8"
        )
        (root / "docs" / f"{name}.md").write_text(
            f"# {name}\n\nDefines {name.title()}Result. " + "Detail. " * 60,
            encoding="utf-8",
        )
    _, ok, problems = verify(root)
    assert ok, problems


# --------------------------------------------------- honesty about small samples
def test_overlapping_ranges_are_not_reported_as_an_effect():
    """A median of three invites exactly this trap: two arms whose runs are
    interleaved still produce different medians, and the difference reads as a
    finding unless something checks whether the ranges separate at all."""
    from aggregate import delta_verdict, overlaps

    assert overlaps((78000, 99000), (65000, 91000))
    assert not overlaps((78000, 99000), (34000, 64000))

    summary = {
        "A": {"parent_in_spread": (78000, 99000)},
        "B": {"parent_in_spread": (65000, 91000)},
        "C": {"parent_in_spread": (34000, 64000)},
    }
    assert "variance" in delta_verdict(summary, "A", "B")
    assert "do not overlap" in delta_verdict(summary, "A", "C")


def test_a_missing_spread_is_treated_as_overlapping():
    """No data is not evidence of separation."""
    from aggregate import overlaps

    assert overlaps(None, (1, 2))
    assert overlaps((1, 2), None)


# ------------------------------------------------- the cache must never warm here
def test_a_run_served_from_cache_is_void():
    """The A/B numbers compare the cost of doing four modules' worth of work.
    A cached task is not that work, so a run containing one is measuring
    something else and must not reach a median.

    Structurally unreachable -- every run materialises its own workspace under
    a unique path and the cache key covers the absolute root -- which is
    precisely why it is asserted. "Impossible" that nothing checks is how a
    warm cache silently improves a headline.
    """
    run = record(arm="C", delegated=True, worker_count=4, cached_hits=1)
    assert "cache served 1 task(s)" in classify_void(run, "")


def test_a_cold_run_is_not_void_for_the_cache():
    run = record(arm="C", delegated=True, worker_count=4)
    assert run.cached_hits == 0
    assert classify_void(run, "") == ""


def test_the_demo_disables_the_cache_rather_than_hoping():
    """demo_taint.py re-runs the same instruction against the same rebuilt
    workspace at step 3. Left on, a second invocation would serve that step
    from cache, spawn one worker instead of two, and still print "2 workers".
    """
    source = (REPO_ROOT / "bench" / "ab" / "demo_taint.py").read_text(encoding="utf-8")
    assert "cache_ttl_s=0" in source
