"""Scoring, and the ways a scorer can quietly lie.

The two failures worth guarding: a grader that reads prose (bench defect D2),
and a grader that rewards an agent for reporting everything. Both produce
numbers that look like accuracy and are not.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from conftest import REPO_ROOT

sys.path.insert(0, str(REPO_ROOT / "bench" / "quality"))

from grade import (  # noqa: E402
    TOLERANCE,
    grade_audit,
    grade_extract,
    normalise,
    parse_findings,
)

KEY = {
    "defects": [
        {"module": "alpha", "line": 10, "kind": "one"},
        {"module": "alpha", "line": 40, "kind": "two"},
        {"module": "beta", "line": 25, "kind": "three"},
        {"module": "beta", "line": 80, "kind": "four"},
    ],
    "questions": [
        {"id": 1, "module": "alpha", "needle": "x", "answer": "6", "text": "?"},
        {"id": 2, "module": "alpha", "needle": "y", "answer": "WAL", "text": "?"},
        {"id": 3, "module": "beta", "needle": "z", "answer": "0.05", "text": "?"},
    ],
}


def audit_root(tmp_path: Path, reports: dict[str, str]) -> Path:
    (tmp_path / "findings").mkdir(parents=True, exist_ok=True)
    for module, body in reports.items():
        (tmp_path / "findings" / f"{module}.md").write_text(body, encoding="utf-8")
    return tmp_path


def extract_root(tmp_path: Path, body: str | None) -> Path:
    (tmp_path / "answers").mkdir(parents=True, exist_ok=True)
    if body is not None:
        (tmp_path / "answers" / "answers.md").write_text(body, encoding="utf-8")
    return tmp_path


# ------------------------------------------------------------------ parsing
def test_findings_are_parsed_from_the_required_format():
    text = "LINE 10: broken\n- LINE 40: also broken\n* line 25: lowercase is fine\n"
    assert parse_findings(text) == [10, 40, 25]


def test_prose_without_the_format_is_not_a_finding():
    """The format was asked for. Something that is not it is not a finding,
    and guessing at prose is how a grader starts depending on its own mood."""
    assert parse_findings("I think line 10 is wrong, and also around line 40.") == []


# -------------------------------------------------------------------- audit
def test_a_perfect_report_scores_full_recall(tmp_path):
    root = audit_root(tmp_path, {
        "alpha": "LINE 10: a\nLINE 40: b\n",
        "beta": "LINE 25: c\nLINE 80: d\n",
    })
    score = grade_audit(root, KEY)
    assert score.hits == 4 and score.total == 4
    assert score.accuracy == 1.0 and score.precision == 1.0


def test_the_tolerance_is_two_lines_and_no_more(tmp_path):
    near = audit_root(tmp_path / "near", {"alpha": "LINE 12: a\n"})
    assert grade_audit(near, KEY).hits == 1, f"+{TOLERANCE} should hit"

    far = audit_root(tmp_path / "far", {"alpha": "LINE 13: a\n"})
    assert grade_audit(far, KEY).hits == 0, "+3 should miss"


def test_an_empty_report_scores_zero_and_is_not_void(tmp_path):
    """Nothing here voids. A run that produced an empty report did the work
    and delivered nothing, which is a result about the run."""
    score = grade_audit(audit_root(tmp_path, {"alpha": ""}), KEY)
    assert score.hits == 0 and score.accuracy == 0.0
    assert score.parse_ok is True, "the file existed; it was just empty"


def test_a_missing_report_scores_zero_and_says_so(tmp_path):
    score = grade_audit(audit_root(tmp_path, {}), KEY)
    assert score.accuracy == 0.0
    assert score.parse_ok is False
    assert "no findings file" in score.detail


def test_reporting_every_line_is_punished_by_precision(tmp_path):
    """The shotgun. An agent listing every line scores perfect recall, so
    recall alone would rank it best. Precision is what makes it visible, and
    is why the two are never folded into one number."""
    root = audit_root(tmp_path, {
        "alpha": "\n".join(f"LINE {n}: maybe" for n in range(1, 101)),
        "beta": "\n".join(f"LINE {n}: maybe" for n in range(1, 101)),
    })
    score = grade_audit(root, KEY)
    assert score.accuracy == 1.0
    assert score.precision < 0.05
    assert score.false_positives > 190


def test_one_defect_cannot_be_found_twice(tmp_path):
    """Three reports clustered on one defect are one hit, not three."""
    root = audit_root(tmp_path, {"alpha": "LINE 9: a\nLINE 10: a\nLINE 11: a\n"})
    score = grade_audit(root, KEY)
    assert score.hits == 1
    assert score.false_positives == 2


def test_one_reported_line_cannot_claim_two_defects(tmp_path):
    """The mirror of the spacing rule in the fixture: even if two defects were
    somehow close, a single line is credited to exactly one of them."""
    key = {"defects": [{"module": "alpha", "line": 10, "kind": "a"},
                       {"module": "alpha", "line": 11, "kind": "b"}],
           "questions": []}
    score = grade_audit(audit_root(tmp_path, {"alpha": "LINE 10: x\n"}), key)
    assert score.hits == 1


def test_findings_for_a_module_that_was_not_asked_about_are_ignored(tmp_path):
    root = audit_root(tmp_path, {"alpha": "LINE 10: a\n", "gamma": "LINE 10: a\n"})
    assert grade_audit(root, KEY).hits == 1


# ------------------------------------------------------------------ extract
def test_a_perfect_answer_sheet_scores_full(tmp_path):
    root = extract_root(tmp_path, "Q1: 6\nQ2: WAL\nQ3: 0.05\n")
    score = grade_extract(root, KEY)
    assert score.hits == 3 and score.accuracy == 1.0


def test_case_and_whitespace_are_normalised_but_meaning_is_not(tmp_path):
    lenient = extract_root(tmp_path / "a", "Q1:   6  \nQ2: wal\nQ3: 0.05\n")
    assert grade_extract(lenient, KEY).hits == 3

    wrong = extract_root(tmp_path / "b", "Q1: 5\nQ2: DELETE\nQ3: 0.5\n")
    score = grade_extract(wrong, KEY)
    assert score.hits == 0, "plausible-but-wrong answers score zero"
    assert score.parse_ok is True


def test_an_answer_with_units_around_it_still_counts(tmp_path):
    """"20 seconds" answers "what is KILL_GRACE_S". A wrong number is still
    wrong, which is the part that matters."""
    root = extract_root(tmp_path, "Q1: 6 attempts\nQ2: WAL mode\nQ3: 0.05 seconds\n")
    assert grade_extract(root, KEY).hits == 3


def test_a_missing_answer_sheet_scores_zero_and_is_not_void(tmp_path):
    score = grade_extract(extract_root(tmp_path, None), KEY)
    assert score.accuracy == 0.0 and score.parse_ok is False
    assert "no answers file" in score.detail


def test_prose_instead_of_the_format_scores_zero_and_is_flagged(tmp_path):
    """parse_ok False is the signal that this run failed on FORMAT rather than
    on knowledge -- reported per arm, because one arm failing the format more
    often than the other is itself a finding about the tool."""
    root = extract_root(tmp_path, "The answer to the first one is 6, and WAL for the next.")
    score = grade_extract(root, KEY)
    assert score.hits == 0
    assert score.parse_ok is False


def test_partially_answered_sheets_get_partial_credit(tmp_path):
    root = extract_root(tmp_path, "Q1: 6\nQ3: 0.05\n")
    score = grade_extract(root, KEY)
    assert score.hits == 2
    assert "Q2" in score.detail


def test_normalise_strips_trailing_punctuation_but_not_content():
    assert normalise("  WAL.  ") == "wal"
    assert normalise("`0.05`") == "0.05"
    assert normalise("BEGIN   IMMEDIATE") == "begin immediate"


# ---------------------------------------------------- against the real key
def test_the_real_key_grades_a_perfect_sheet_perfectly():
    """A sanity check against the committed fixture rather than a synthetic
    one: if the real key cannot be satisfied, no run ever could be."""
    fixture = REPO_ROOT / "bench" / "quality" / "fixture"
    if not (fixture / "key.json").is_file():
        pytest.skip("fixture not built")
    key = json.loads((fixture / "key.json").read_text(encoding="utf-8"))

    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "answers").mkdir()
        (root / "answers" / "answers.md").write_text(
            "\n".join(f"Q{q['id']}: {q['answer']}" for q in key["questions"]),
            encoding="utf-8")
        assert grade_extract(root, key).accuracy == 1.0

        (root / "findings").mkdir()
        by_module: dict[str, list[int]] = {}
        for defect in key["defects"]:
            by_module.setdefault(defect["module"], []).append(defect["line"])
        for module, lines in by_module.items():
            (root / "findings" / f"{module}.md").write_text(
                "\n".join(f"LINE {n}: found" for n in lines), encoding="utf-8")
        score = grade_audit(root, key)
        assert score.accuracy == 1.0 and score.precision == 1.0
