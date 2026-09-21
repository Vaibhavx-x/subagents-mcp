"""Does a clean clone of this repo actually work for someone else?

    python tools/install_check.py              # clone, fresh venv, install, test
    python tools/install_check.py --no-venv    # clone and test with this interpreter

Clones from the repository rather than copying the working tree, so it tests
what is COMMITTED. That is the point: the classic clean-clone failure is a file
everything depends on which `.gitignore` quietly excludes, and a copy of the
working directory would carry that file along and hide the problem.

Exits non-zero on the first failure, and says which step failed.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import platform
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

# Files a clone must contain to be usable. Each is something a new user needs
# and that a careless .gitignore rule could plausibly swallow.
REQUIRED = [
    "server.py",
    "CHANGELOG.md",
    "README.md",
    "CLAUDE.md",
    "NOTES.md",
    ".env.example",
    "pytest.ini",
    "subagents/__init__.py",
    "tests/conftest.py",
    "tests/fake_worker.py",
]

# And files that must NOT be in a clone: local strategy notes and secrets.
FORBIDDEN = ["CONTEXT.md", "ZUDDL.md", ".env"]


def platform_line() -> str:
    """What this check actually validated, and on what.

    Printed because a green run here is evidence about ONE platform. The kill
    path is a Windows Job Object; elsewhere it falls back to a process group,
    which does not die with its creator, and `test_hard_kill.py` skips rather
    than pretending otherwise. A reader seeing "passed" without seeing where
    would reasonably read it as a claim about their machine.
    """
    return (f"platform: {platform.system()} {platform.release()} "
            f"({platform.machine()}) | python {sys.version.split()[0]}"
            + ("" if platform.system() == "Windows"
               else " | NOT the supported platform -- worker kill degrades to a "
                    "process group here, untested"))


def step(label: str) -> None:
    print(f"\n--- {label}")


def inspect_clone(clone: Path) -> list[str]:
    """Problems with a fresh clone. Empty list means it is usable.

    Separated from main so it can be tested against a synthetic directory --
    the failure it guards against (a required file quietly gitignored) is not
    one you want to discover from a colleague.
    """
    problems = []
    for name in REQUIRED:
        if not (clone / name).is_file():
            problems.append(f"missing required file: {name}")
    for name in FORBIDDEN:
        if (clone / name).exists():
            problems.append(f"local-only file present in the clone: {name}")
    return problems


def run(cmd: list[str], cwd: Path, label: str, *, must_pass: bool = True) -> bool:
    print(f"    $ {' '.join(str(c) for c in cmd)}")
    proc = subprocess.run(cmd, cwd=str(cwd), capture_output=True, text=True)
    tail = (proc.stdout or proc.stderr or "").strip().splitlines()[-6:]
    for line in tail:
        print(f"    | {line}")
    if proc.returncode != 0:
        verb = "FAILED" if must_pass else "exit"
        print(f"    {verb}: {label} ({proc.returncode})")
    return proc.returncode == 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--no-venv", action="store_true",
                        help="use the current interpreter instead of a fresh venv")
    parser.add_argument("--keep", action="store_true", help="keep the temp clone")
    args = parser.parse_args()

    workdir = Path(tempfile.mkdtemp(prefix="subagents-install-"))
    clone = workdir / "subagents-mcp"
    print(platform_line())
    print(f"clone target: {clone}")

    try:
        step("cloning what is committed, not what is on disk")
        if not run(["git", "clone", "--quiet", str(REPO), str(clone)], workdir, "git clone"):
            return 1

        step("checking the clone is complete")
        problems = inspect_clone(clone)
        if problems:
            for problem in problems:
                print(f"    FAILED: {problem}")
            print("    Something required is gitignored, or something local was committed.")
            return 1
        print(f"    all {len(REQUIRED)} required files present")
        print(f"    none of {FORBIDDEN} present, as intended")

        python = sys.executable
        if not args.no_venv:
            step("creating a fresh virtual environment")
            if not run([sys.executable, "-m", "venv", str(clone / ".venv")], clone, "venv"):
                return 1
            python = str(clone / ".venv" / ("Scripts" if sys.platform == "win32" else "bin")
                         / ("python.exe" if sys.platform == "win32" else "python"))

            step("installing dependencies")
            if not run([python, "-m", "pip", "install", "--quiet", "mcp", "pytest", "anyio"],
                       clone, "pip install"):
                print("    (no network? re-run with --no-venv to skip this step)")
                return 1

        step("running the suite in the clone")
        if not run([python, "-m", "pytest", "-q"], clone, "pytest"):
            return 1

        step("checking the client configuration report runs")
        # Exit code deliberately not asserted: --check-config returns non-zero
        # when the clone is not the registered server path, which is exactly
        # what a fresh checkout on someone else's laptop looks like. What
        # matters is that it runs and prints the block to copy.
        run([python, "server.py", "--check-config"], clone, "check-config", must_pass=False)

        print("\nclean clone installs and passes")
        return 0
    finally:
        if args.keep:
            print(f"kept: {workdir}")
        else:
            shutil.rmtree(workdir, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
