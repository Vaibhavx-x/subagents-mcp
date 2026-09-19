"""What happens to running workers when the server itself is killed.

The Job Object is created with JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE, so when the
last handle to it closes -- which is what happens when the owning process dies,
however abruptly -- Windows terminates everything in the job. That is the
theory. Until this test there was no evidence for it, and the failure mode it
guards against is the worst one available: a hard-killed server leaving live
`agy` processes burning tokens with nobody watching.
"""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

import pytest
from conftest import REPO_ROOT
from test_worker import kill_marker, marker_alive

pytestmark = pytest.mark.skipif(
    sys.platform != "win32",
    reason="POSIX uses a process group, which does NOT die with its creator -- "
           "the equivalent guarantee does not exist there",
)

DRIVER = str(REPO_ROOT / "tests" / "kill_driver.py")


def wait_until(predicate, timeout_s: float, poll_s: float = 0.2) -> bool:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(poll_s)
    return False


def test_hard_killing_the_server_takes_its_workers_with_it():
    marker = "FAKE_WORKER_HARDKILL_1"
    driver = subprocess.Popen(
        [sys.executable, DRIVER, marker],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, cwd=str(REPO_ROOT),
    )
    try:
        # driver + worker + grandchild all carry the marker on their command line.
        assert wait_until(lambda: marker_alive(marker) >= 3, timeout_s=45), (
            f"the tree never came up (saw {marker_alive(marker)} processes)"
        )

        # SIGKILL-equivalent: no cleanup handler can possibly run.
        subprocess.run(["taskkill", "/F", "/PID", str(driver.pid)],
                       capture_output=True, check=False)
        driver.wait(timeout=30)

        assert wait_until(lambda: marker_alive(marker) == 0, timeout_s=30), (
            f"{marker_alive(marker)} process(es) outlived the hard-killed server"
        )
    finally:
        kill_marker(marker)
        if driver.poll() is None:
            driver.kill()
