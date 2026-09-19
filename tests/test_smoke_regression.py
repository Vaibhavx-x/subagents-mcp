"""Proof that the stdio smoke test can actually fail.

A smoke test that cannot fail is not a test. stdout pollution is the failure
this project is most exposed to -- it is invisible to every in-memory test,
and the symptom at the host is "a server with zero tools" rather than an
error naming the cause.

So: take the real server, add one `print()` at import time, and require the
smoke test to reject it. The clean server is the control.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SMOKE = REPO_ROOT / "tests" / "smoke_stdio.py"


def run_smoke(server_path: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(SMOKE), str(server_path)],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
        timeout=120,
    )


def write_variant(tmp_path: Path, pollution: str) -> Path:
    """A copy of the real server with `pollution` injected before it starts."""
    original = (REPO_ROOT / "server.py").read_text(encoding="utf-8")
    variant = tmp_path / "polluted_server.py"
    variant.write_text(
        f"import sys\nsys.path.insert(0, r{str(REPO_ROOT)!r})\n{pollution}\n{original}",
        encoding="utf-8",
    )
    return variant


@pytest.mark.slow
def test_clean_server_passes_smoke():
    """Control. If this fails, the failures below prove nothing."""
    result = run_smoke(REPO_ROOT / "server.py")
    assert result.returncode == 0, f"clean server failed smoke:\n{result.stderr}"
    assert "SMOKE | clean" in result.stderr


@pytest.mark.slow
def test_import_time_print_is_caught():
    """The canonical mistake: a debug print left at module scope."""
    with_pollution = write_variant(
        Path(__import__("tempfile").mkdtemp()),
        'print("this line lands on the protocol channel")',
    )
    result = run_smoke(with_pollution)
    assert result.returncode != 0, (
        "smoke test PASSED a server that writes to stdout at import time -- "
        "it therefore proves nothing about protocol cleanliness"
    )


@pytest.mark.slow
def test_logging_handler_on_stdout_is_caught():
    """The subtler mistake: a StreamHandler defaulting to stdout.

    logging.StreamHandler() with no argument writes to stderr, but an explicit
    sys.stdout -- or basicConfig(stream=sys.stdout) -- is a plausible slip and
    pollutes every log line rather than just one.
    """
    with_pollution = write_variant(
        Path(__import__("tempfile").mkdtemp()),
        "import logging, sys as _s\n"
        "logging.getLogger().addHandler(logging.StreamHandler(_s.stdout))\n"
        "logging.getLogger().warning('polluting the protocol channel')",
    )
    result = run_smoke(with_pollution)
    assert result.returncode != 0, (
        "smoke test PASSED a server logging to stdout"
    )
