"""Score what an agent actually produced, against a generated key.

Two rules shape all of this.

**Never grade prose.** The audit task is scored on **line numbers only** --
never on whether the description sounds right. That is bench defect D2, where a
whole-log grep for "429" matched a conversation_id and voided a run that had
succeeded. A grader that reads descriptions is a grader whose results depend on
how the grader feels.

**A bad answer is a zero, not a void.** Missing or malformed output scores 0 and
stays in the median. Voiding it would be the more comfortable choice and the
wrong one: if one arm follows the output format worse than the other -- entirely
plausible, since a delegating arm gives each worker one simple file -- then
dropping those runs erases a real finding about the tool. Void is reserved for
"this run does not measure what we think it does"; see `harness.classify_void`.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

# A finding within this many lines of a planted defect counts as a hit.
# `inject.MIN_GAP` guarantees defects are further apart than 2*TOLERANCE, so
# one reported line can never be credited to two defects.
TOLERANCE = 2

_FINDING = re.compile(r"^\s*(?:[-*]\s*)?LINE\s+(\d+)\s*:", re.IGNORECASE | re.MULTILINE)
_ANSWER = re.compile(r"^\s*(?:[-*]\s*)?Q\s*(\d+)\s*:\s*(.+?)\s*$", re.IGNORECASE | re.MULTILINE)


@dataclass
class Score:
    hits: int = 0
    total: int = 0
    false_positives: int = 0
    parse_ok: bool = False
    detail: str = ""

    @property
    def accuracy(self) -> float:
        """Recall. Precision is reported beside it, never folded in.

        Folding them into an F-score would hide the shotgun: an agent that
        lists every line scores perfect recall, and a single number would
        reward it. Two numbers make that visible.
        """
        return (self.hits / self.total) if self.total else 0.0

    @property
    def precision(self) -> float:
        reported = self.hits + self.false_positives
        return (self.hits / reported) if reported else 0.0


def normalise(text: str) -> str:
    """Case and whitespace are noise. Meaning is not, and is not normalised."""
    return " ".join(text.strip().casefold().split()).strip(".'\"` ")


def parse_findings(text: str) -> list[int]:
    """Line numbers claimed, in the order reported.

    Tolerant of a leading bullet, because a model writing Markdown will add
    one and the format is not what is being measured. Intolerant of anything
    else: `LINE <n>:` was asked for and anything that is not that is not a
    finding.
    """
    return [int(match.group(1)) for match in _FINDING.finditer(text)]


def grade_audit(root: Path, key: dict) -> Score:
    """Recall over planted defects, with false positives counted separately."""
    planted: dict[str, list[int]] = {}
    for defect in key["defects"]:
        planted.setdefault(defect["module"], []).append(defect["line"])

    score = Score(total=len(key["defects"]))
    found_any_file = False
    unmatched_total = 0

    for module, lines in planted.items():
        report = root / "findings" / f"{module}.md"
        if not report.is_file():
            continue
        found_any_file = True
        claimed = parse_findings(report.read_text(encoding="utf-8", errors="replace"))

        # Each planted defect is credited at most once, and each claimed line
        # matches at most one defect -- otherwise one lucky line number could
        # sweep a cluster, or one defect could be "found" five times.
        remaining = list(lines)
        for line in claimed:
            match = next((p for p in remaining if abs(p - line) <= TOLERANCE), None)
            if match is None:
                unmatched_total += 1
            else:
                remaining.remove(match)
                score.hits += 1

    score.false_positives = unmatched_total
    score.parse_ok = found_any_file
    if not found_any_file:
        score.detail = "no findings file produced"
    elif score.hits == 0:
        score.detail = "findings produced, none matched a planted defect"
    return score


def grade_extract(root: Path, key: dict) -> Score:
    """Normalised exact match against the answer key."""
    questions = key["questions"]
    score = Score(total=len(questions))

    answers_file = root / "answers" / "answers.md"
    if not answers_file.is_file():
        score.detail = "no answers file produced"
        return score

    text = answers_file.read_text(encoding="utf-8", errors="replace")
    given = {int(m.group(1)): m.group(2) for m in _ANSWER.finditer(text)}
    if not given:
        score.detail = "answers file produced but no `Q<n>: <answer>` lines in it"
        return score

    score.parse_ok = True
    missed: list[int] = []
    for question in questions:
        supplied = given.get(question["id"])
        if supplied is None:
            missed.append(question["id"])
            continue
        # Containment, not equality: "20 seconds" is a correct answer to "what
        # is KILL_GRACE_S". A wrong number is still wrong, which is the part
        # that matters.
        if normalise(question["answer"]) in normalise(supplied):
            score.hits += 1
        else:
            missed.append(question["id"])
    # An unanswered or wrong question is a miss, never a false positive --
    # there is nothing to be falsely positive about in a fixed question set.
    if missed:
        score.detail = "missed " + ",".join(f"Q{q}" for q in missed)
    return score


def grade(task: str, root: Path, key: dict) -> Score:
    if task == "audit":
        return grade_audit(root, key)
    if task == "extract":
        return grade_extract(root, key)
    raise ValueError(f"unknown task {task!r}")


def load_key(fixture: Path) -> dict:
    return json.loads((fixture / "key.json").read_text(encoding="utf-8"))
