"""Workspace boundary and task_ref validation.

These are the checks standing between a model-generated string and the
filesystem, so each failure mode gets an explicit test.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from subagents.errors import PathOutsideWorkspace, PlanRefused
from subagents.validate import (
    is_inside,
    resolve_declared_path,
    resolve_workspace_root,
    validate_task_ref,
    validate_unique_task_refs,
)


def R(declared: str, root: Path) -> Path:
    return resolve_declared_path(declared, root, field="reads", task_ref="t")


# ------------------------------------------------------------------ paths
def test_relative_path_resolves_under_root(workspace: Path):
    assert R("pkg/config.py", workspace) == (workspace / "pkg" / "config.py").resolve()


def test_relative_path_ignores_process_cwd(workspace: Path, tmp_path: Path, monkeypatch):
    """Path('x').resolve() silently uses os.getcwd(); we must not.

    Run from an unrelated directory that also contains a matching filename, so
    a CWD-based implementation would resolve to the wrong file rather than
    merely failing.
    """
    decoy = tmp_path / "decoy"
    (decoy / "pkg").mkdir(parents=True)
    (decoy / "pkg" / "config.py").write_text("decoy\n", encoding="utf-8")
    monkeypatch.chdir(decoy)

    resolved = R("pkg/config.py", workspace)
    assert resolved == (workspace / "pkg" / "config.py").resolve()
    assert "decoy" not in str(resolved)


def test_traversal_escape_rejected(workspace: Path):
    with pytest.raises(PathOutsideWorkspace):
        R("../escape.txt", workspace)


def test_deep_traversal_escape_rejected(workspace: Path):
    with pytest.raises(PathOutsideWorkspace):
        R("pkg/../../escape.txt", workspace)


def test_absolute_path_outside_root_rejected(workspace: Path, tmp_path: Path):
    outside = tmp_path / "outside.txt"
    outside.write_text("x", encoding="utf-8")
    with pytest.raises(PathOutsideWorkspace):
        R(str(outside), workspace)


def test_nonexistent_file_under_root_accepted(workspace: Path):
    """A worker may be about to CREATE the file; it need not exist yet."""
    resolved = R("pkg/brand_new.py", workspace)
    assert resolved == (workspace / "pkg" / "brand_new.py").resolve()
    assert not resolved.exists()


def test_case_variant_accepted_as_inside(workspace: Path):
    weird = str(workspace).upper() + os.sep + "README.md"
    assert R(weird, workspace).name.lower() == "readme.md"


def test_root_itself_is_inside(workspace: Path):
    assert is_inside(workspace, workspace)


def test_sibling_prefix_directory_is_not_inside(workspace: Path, tmp_path: Path):
    """'ws-evil' must not count as inside 'ws' just because it shares a prefix."""
    sibling = tmp_path / "ws-evil"
    sibling.mkdir()
    assert not is_inside(sibling.resolve(), workspace)


def test_empty_path_rejected(workspace: Path):
    with pytest.raises(PlanRefused):
        R("   ", workspace)


def test_null_byte_rejected(workspace: Path):
    with pytest.raises(PlanRefused):
        R("pkg/con\x00fig.py", workspace)


def test_non_string_path_rejected(workspace: Path):
    with pytest.raises(PlanRefused):
        resolve_declared_path(42, workspace, field="reads", task_ref="t")


def _can_symlink(tmp_path: Path) -> bool:
    try:
        (tmp_path / "_probe_target").mkdir(exist_ok=True)
        link = tmp_path / "_probe_link"
        if link.exists():
            return True
        os.symlink(tmp_path / "_probe_target", link, target_is_directory=True)
        return True
    except (OSError, NotImplementedError, AttributeError):
        return False


def test_symlink_pointing_outside_rejected_after_resolution(workspace: Path, tmp_path: Path):
    """Containment must be checked AFTER resolve(), which follows symlinks.

    Checking the literal string first would accept this: the declared path
    looks like it is inside the workspace, and only resolution reveals it is
    not.
    """
    if not _can_symlink(tmp_path):
        pytest.skip(
            "cannot create symlinks on this machine (Windows needs Developer Mode "
            "or admin). The symlink escape path is therefore UNVERIFIED here."
        )

    secret_dir = tmp_path / "secret"
    secret_dir.mkdir(exist_ok=True)
    (secret_dir / "creds.txt").write_text("token", encoding="utf-8")

    link = workspace / "looks_local"
    os.symlink(secret_dir, link, target_is_directory=True)

    with pytest.raises(PathOutsideWorkspace):
        R("looks_local/creds.txt", workspace)


# ------------------------------------------------------------- task_ref
@pytest.mark.parametrize("ref", ["a", "fix-auth", "rename-config", "t1", "a" * 40, "0-9-z"])
def test_valid_task_refs_accepted(ref):
    assert validate_task_ref(ref) == ref


@pytest.mark.parametrize(
    "ref",
    [
        "Fix-Auth",                 # uppercase
        "a" * 41,                   # too long
        "",                         # empty
        "fix auth",                 # space
        "fix_auth",                 # underscore
        "../escape",                # path traversal shape
        "fix/auth",                 # path component
        "'; DROP TABLE plans;--",   # SQL injection shape
        "café",                     # non-ascii
        "fix.auth",                 # dot
    ],
)
def test_invalid_task_refs_rejected(ref):
    with pytest.raises(PlanRefused):
        validate_task_ref(ref)


def test_non_string_task_ref_rejected():
    with pytest.raises(PlanRefused):
        validate_task_ref(None)


def test_duplicate_task_refs_rejected():
    with pytest.raises(PlanRefused) as exc:
        validate_unique_task_refs(["a", "b", "a"])
    assert "a" in str(exc.value)


def test_unique_task_refs_accepted():
    validate_unique_task_refs(["a", "b", "c"])


# --------------------------------------------------------- workspace_root
def test_workspace_root_must_be_allowlisted(workspace: Path, tmp_path: Path):
    other = tmp_path / "not-allowed"
    other.mkdir()
    with pytest.raises(PlanRefused) as exc:
        resolve_workspace_root(str(other), (workspace,))
    assert "allowlist" in str(exc.value)


def test_workspace_root_accepted_when_allowlisted(workspace: Path):
    assert resolve_workspace_root(str(workspace), (workspace,)) == workspace


def test_subdirectory_of_allowlisted_root_accepted(workspace: Path):
    assert resolve_workspace_root(str(workspace / "pkg"), (workspace,)) == (workspace / "pkg").resolve()


def test_workspace_root_must_exist(tmp_path: Path):
    with pytest.raises(PlanRefused):
        resolve_workspace_root(str(tmp_path / "nope"), (tmp_path,))


def test_workspace_root_required():
    with pytest.raises(PlanRefused):
        resolve_workspace_root("", (Path("D:/"),))
