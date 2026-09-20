"""The clean-clone check, tested against synthetic clones.

The failure it guards against -- a required file quietly gitignored, so the
repository works for its author and for nobody else -- is not one you want to
discover from a colleague who has already given up.
"""

from __future__ import annotations

import sys
from pathlib import Path

from conftest import REPO_ROOT

sys.path.insert(0, str(REPO_ROOT / "tools"))

from install_check import FORBIDDEN, REQUIRED, inspect_clone  # noqa: E402


def make_clone(tmp_path: Path, *, omit=(), include_local=()) -> Path:
    clone = tmp_path / "clone"
    for name in REQUIRED:
        if name in omit:
            continue
        target = clone / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("x", encoding="utf-8")
    for name in include_local:
        (clone / name).write_text("secret", encoding="utf-8")
    return clone


def test_a_complete_clone_has_no_problems(tmp_path):
    assert inspect_clone(make_clone(tmp_path)) == []


def test_a_gitignored_requirement_is_caught(tmp_path):
    """The classic: everything works for the author, nothing works for anyone
    who clones it."""
    problems = inspect_clone(make_clone(tmp_path, omit=("tests/conftest.py",)))
    assert any("tests/conftest.py" in p for p in problems)


def test_a_leaked_local_file_is_caught(tmp_path):
    """CONTEXT.md and ZUDDL.md are strategy notes and must stay out of the
    repository; .env holds real values."""
    problems = inspect_clone(make_clone(tmp_path, include_local=("CONTEXT.md",)))
    assert any("CONTEXT.md" in p for p in problems)


def test_the_forbidden_list_matches_what_gitignore_excludes():
    """If someone removes an ignore rule, this check should stop claiming the
    file is deliberately absent."""
    ignored = (REPO_ROOT / ".gitignore").read_text(encoding="utf-8")
    for name in FORBIDDEN:
        assert name in ignored, f"{name} is checked for but not gitignored"


def test_every_required_file_actually_exists_here():
    """A requirement naming a file this repo does not have would pass a clone
    check for the wrong reason."""
    for name in REQUIRED:
        assert (REPO_ROOT / name).is_file(), name
