"""The graded fixture, and the key that grades it.

If the key is wrong, every number downstream is wrong and nothing raises. So
the key is generated from the edit that produced it, and these tests prove the
generation held.
"""

from __future__ import annotations

import ast
import json
import subprocess
import sys
from pathlib import Path

import pytest
from conftest import REPO_ROOT

# Imported by package path, never by bare name. `bench/ab/` and
# `bench/quality/` both define `harness` and `aggregate`, so a bare
# `import aggregate` resolves to whichever directory landed on sys.path first
# -- which depends on test collection order and fails somewhere else entirely.

from bench.quality.inject import (  # noqa: E402
    DEFECTS,
    MIN_GAP,
    MODULES,
    QUESTIONS,
    build,
    check,
)

FIXTURE = REPO_ROOT / "bench" / "quality" / "fixture"

pytestmark = pytest.mark.skipif(
    not (FIXTURE / "key.json").is_file(),
    reason="fixture not built; run python bench/quality/inject.py",
)


@pytest.fixture(scope="module")
def key() -> dict:
    return json.loads((FIXTURE / "key.json").read_text(encoding="utf-8"))


def test_the_committed_fixture_matches_its_own_key():
    """The whole point. `--check` runs this too; here it gates the suite."""
    assert check(FIXTURE) == []


def test_every_recorded_line_really_holds_the_injected_text(key):
    replacement_of = {(m, k): r for m, _n, r, k in DEFECTS}
    for defect in key["defects"]:
        source = (FIXTURE / "audit" / "src" / f"{defect['module']}.py").read_text(
            encoding="utf-8").splitlines()
        line = source[defect["line"] - 1]
        assert replacement_of[(defect["module"], defect["kind"])] in line


def test_no_two_defects_sit_inside_the_grading_tolerance(key):
    """The grader credits a finding within +/-2 lines. Two defects closer than
    5 apart could therefore both be credited to one reported line, which would
    silently inflate recall for a lucky guess."""
    by_module: dict[str, list[int]] = {}
    for defect in key["defects"]:
        by_module.setdefault(defect["module"], []).append(defect["line"])
    for module, lines in by_module.items():
        ordered = sorted(lines)
        for earlier, later in zip(ordered, ordered[1:]):
            assert later - earlier >= MIN_GAP, f"{module}: {earlier} and {later}"


def test_the_clean_source_does_not_already_contain_the_defect():
    """Otherwise the "defect" was always there, and finding it proves nothing
    about the agent and nothing about the tool."""
    for module, _needle, replacement, kind in DEFECTS:
        clean = (REPO_ROOT / "subagents" / f"{module}.py").read_text(encoding="utf-8")
        assert replacement not in clean, f"{module}: {kind}"


def test_every_injected_module_still_parses():
    """A syntax error turns an audit task into a different task -- every agent
    would report line 1 and the scores would measure nothing."""
    for module in MODULES:
        text = (FIXTURE / "audit" / "src" / f"{module}.py").read_text(encoding="utf-8")
        ast.parse(text)


def test_injection_preserves_the_line_count():
    """Line numbers in the key are the fixture's own only if every edit stays
    on one line."""
    for module in MODULES:
        clean = (REPO_ROOT / "subagents" / f"{module}.py").read_text(encoding="utf-8")
        planted = (FIXTURE / "audit" / "src" / f"{module}.py").read_text(encoding="utf-8")
        assert len(clean.splitlines()) == len(planted.splitlines()), module


def test_the_extract_fixture_is_actually_clean():
    """The extraction task asks about real code. If a defect leaked into that
    copy, a correct answer would be graded wrong."""
    for module in MODULES:
        clean = (REPO_ROOT / "subagents" / f"{module}.py").read_text(encoding="utf-8")
        served = (FIXTURE / "extract" / "src" / f"{module}.py").read_text(encoding="utf-8")
        assert served == clean, module


def test_every_answer_is_where_its_evidence_says_it_is(key):
    for question in key["questions"]:
        source = (FIXTURE / "extract" / "src" / f"{question['module']}.py").read_text(
            encoding="utf-8")
        lines = [ln for ln in source.splitlines() if question["needle"] in ln]
        assert lines, f"Q{question['id']}: needle not found"
        if question.get("derived"):
            continue
        assert any(question["answer"] in ln for ln in lines), f"Q{question['id']}"


def test_a_derived_answer_writes_down_its_arithmetic(key):
    """A derived answer is not a literal anywhere, so nothing can check it
    mechanically. The derivation is what lets a reader disagree with the key
    rather than having to trust it."""
    derived = [q for q in key["questions"] if q.get("derived")]
    assert derived, "no derived questions -- every answer is a grep away"
    for question in derived:
        assert question.get("derivation"), f"Q{question['id']}"


def test_one_module_is_a_clean_control(key):
    """Calibration scored both arms 100% when every module was known to be
    broken: an agent that assumes each file has defects and reports its most
    suspicious lines is right by construction. A clean module gives the task a
    wrong answer to get (NOTES.md section 44)."""
    planted = {d["module"] for d in key["defects"]}
    clean = set(key["modules"]) - planted
    assert clean, "every module carries a defect -- precision measures nothing"


def test_some_questions_deliberately_span_modules():
    """This is where delegation should hurt: a worker given one module cannot
    answer a question whose evidence is in another. A question set without any
    would be built to flatter the tool."""
    spanning = [q for q in QUESTIONS if q.get("spans")]
    assert spanning, "no cross-module questions -- the test is rigged in the tool's favour"
    for question in spanning:
        assert question["spans"] != question["module"], (
            f"Q{question['id']}: `spans` names the module the answer is already in"
        )


def test_injection_is_deterministic(tmp_path):
    """A key that varies between builds could never be committed."""
    first = build(tmp_path / "a")
    second = build(tmp_path / "b")
    assert json.dumps(first, sort_keys=True) == json.dumps(second, sort_keys=True)
    for module in MODULES:
        assert ((tmp_path / "a" / "audit" / "src" / f"{module}.py").read_bytes()
                == (tmp_path / "b" / "audit" / "src" / f"{module}.py").read_bytes())


def test_check_catches_a_key_that_has_drifted(tmp_path):
    """The guard has to be able to fail, or it guards nothing."""
    build(tmp_path / "f")
    key_path = tmp_path / "f" / "key.json"
    data = json.loads(key_path.read_text(encoding="utf-8"))
    data["defects"][0]["line"] += 40          # point it at innocent code
    key_path.write_text(json.dumps(data), encoding="utf-8")
    assert check(tmp_path / "f") != []


def test_the_cli_check_exits_zero_on_the_committed_fixture():
    proc = subprocess.run(
        [sys.executable, "bench/quality/inject.py", "--check"],
        capture_output=True, text=True, cwd=str(REPO_ROOT), timeout=120,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
