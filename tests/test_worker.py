"""Worker lifecycle: capture, timeout, tree kill, output classification.

Everything here uses tests/fake_worker.py, so it costs no API calls. The two
tests that matter most are the ones pinning measured behaviour that the obvious
implementation gets wrong: partial output on timeout, and orphaned grandchildren.
"""

from __future__ import annotations

import asyncio
import subprocess
import sys
import time
from pathlib import Path

import pytest
from conftest import REPO_ROOT

from subagents.worker import (
    DEPTH_ENV_VAR,
    build_command,
    classify,
    parse_output,
    run_worker,
    worker_env,
)

FAKE = str(REPO_ROOT / "tests" / "fake_worker.py")


def fake_cmd(*args: str) -> list[str]:
    return [sys.executable, FAKE, *args]


def run(cmd, timeout_s=10, task_ref="t", cwd=None):
    return asyncio.run(
        run_worker(task_ref, cmd, timeout_s=timeout_s, cwd=cwd or REPO_ROOT, model="fake-model")
    )


def marker_alive(marker: str) -> int:
    """Count live processes carrying a marker, via a real process listing."""
    if sys.platform == "win32":
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             f"(Get-CimInstance Win32_Process | Where-Object {{ $_.CommandLine -like '*{marker}*' }}).Count"],
            capture_output=True, text=True,
        ).stdout.strip()
        return int(out or 0)
    out = subprocess.run(["ps", "-eo", "args"], capture_output=True, text=True).stdout
    return sum(1 for line in out.splitlines() if marker in line and "ps -eo" not in line)


def kill_marker(marker: str) -> None:
    if sys.platform == "win32":
        subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             f"Get-CimInstance Win32_Process | Where-Object {{ $_.CommandLine -like '*{marker}*' }} | "
             f"ForEach-Object {{ Stop-Process -Id $_.ProcessId -Force }}"],
            capture_output=True,
        )


# ------------------------------------------------------------------ happy
def test_successful_worker_is_parsed():
    result = run(fake_cmd("--mode", "ok"))
    assert result.ok and result.status == "ok"
    assert result.agy_status == "SUCCESS"
    assert result.tokens_in == 120000 and result.tokens_out == 1500
    assert result.cache_read_tokens == 40000
    assert "Renamed the helper" in result.summary
    assert result.returncode == 0


def test_summary_is_short_not_the_whole_transcript():
    """The point of the project is that the parent gets a summary, not a dump."""
    result = run(fake_cmd("--mode", "ok"))
    assert len(result.summary) <= 420
    assert len(result.transcript) > len(result.summary)


def test_stderr_is_kept_in_the_transcript():
    result = run(fake_cmd("--mode", "stderr_noise"))
    assert result.ok
    assert "a warning on stderr" in result.transcript


# --------------------------------------------------------------- timeouts
def test_timeout_retains_partial_output():
    """The measured trap: wait_for(communicate()) would return b'' here."""
    result = run(fake_cmd("--mode", "hang", "--flush-first"), timeout_s=2)
    assert result.status == "timeout"
    assert "PARTIAL-BEFORE-HANG" in result.transcript
    assert "PARTIAL-STDERR" in result.transcript


def test_timeout_summary_says_it_is_incomplete():
    """A truncated transcript must never read as a finished answer."""
    result = run(fake_cmd("--mode", "hang", "--flush-first"), timeout_s=2)
    assert "INCOMPLETE" in result.summary
    assert "deadline" in result.summary.lower()
    assert result.exit_reason


def test_timeout_is_roughly_on_time():
    start = time.time()
    run(fake_cmd("--mode", "hang"), timeout_s=2)
    assert 1.5 <= time.time() - start < 15


def test_timeout_kills_the_whole_process_tree():
    """p.kill() alone leaves grandchildren running -- measured on Windows."""
    marker = "FAKE_WORKER_TREEKILL_1"
    try:
        result = run(
            fake_cmd("--mode", "hang", "--spawn-child", "--marker", marker), timeout_s=2
        )
        assert result.status == "timeout"
        time.sleep(1.0)
        assert marker_alive(marker) == 0, "a grandchild outlived the worker"
    finally:
        kill_marker(marker)


def test_worker_that_spawns_a_background_process_is_not_reported_as_a_timeout():
    """Regression: pipe EOF is not process exit.

    asyncio's `proc.wait()` completes when the subprocess transport sees EOF on
    the pipes -- and a grandchild inherits those handles. So a worker that
    starts any background process (a dev server, a watcher, a daemon left by a
    tool) keeps the pipes open after exiting, and `proc.wait()` blocks until
    the full timeout. The worker would be recorded as a timeout despite having
    succeeded, and its tree killed for no reason.

    The fix polls process liveness instead. This test fails without it.
    """
    marker = "FAKE_WORKER_TREEKILL_2"
    try:
        result = run(fake_cmd("--mode", "ok", "--spawn-child", "--marker", marker), timeout_s=10)
        assert result.ok, f"reported {result.status} ({result.exit_reason})"
        assert result.tokens_in == 120000, "output should still parse normally"
    finally:
        kill_marker(marker)


def test_background_process_does_not_delay_a_fast_worker():
    """The same bug, measured as latency rather than status."""
    marker = "FAKE_WORKER_TREEKILL_3"
    try:
        start = time.time()
        result = run(fake_cmd("--mode", "ok", "--spawn-child", "--marker", marker), timeout_s=30)
        elapsed = time.time() - start
        assert result.ok
        assert elapsed < 10, f"took {elapsed:.1f}s; it was waiting on pipe EOF, not the process"
    finally:
        kill_marker(marker)


# ------------------------------------------------------- malformed output
@pytest.mark.parametrize("mode,expected", [
    ("garbage", "unparseable"),
    ("empty", "unparseable"),
    ("truncated", "unparseable"),
])
def test_malformed_output_is_recorded_not_raised(mode, expected):
    result = run(fake_cmd("--mode", mode))
    assert result.status == expected
    assert "FAILED" in result.summary


def test_garbage_output_is_still_retained_for_debugging():
    result = run(fake_cmd("--mode", "garbage"))
    assert "this is not json at all" in result.transcript


def test_missing_usage_block_does_not_crash():
    result = run(fake_cmd("--mode", "no_usage"))
    assert result.ok
    assert result.tokens_in is None


def test_spawn_error_is_reported_not_raised():
    result = run([str(REPO_ROOT / "definitely-not-an-executable-xyz")])
    assert result.status == "spawn_error"
    assert "could not start worker" in result.summary


# ----------------------------------------------------------- classification
def test_error_status_is_a_failure():
    result = run(fake_cmd("--mode", "error_status"))
    assert result.status == "failed"
    assert "ERROR" in result.exit_reason


def test_rate_limit_status_is_detected():
    result = run(fake_cmd("--mode", "rate_limited"))
    assert result.status == "failed"
    assert "rate_limited" in result.exit_reason


def test_429_in_conversation_id_is_not_a_rate_limit():
    """Bench defect D2, as a regression test.

    A whole-log grep for 429 matched a conversation_id and voided a run that
    had succeeded -- and that run's token counts were quoted in the published
    table while its row said rate_limited.
    """
    result = run(fake_cmd("--mode", "conv_id_429"))
    assert result.ok, result.exit_reason
    assert "429" in result.transcript


@pytest.mark.parametrize("status,expected", [
    ("SUCCESS", "ok"), ("success", "ok"),
    ("ERROR", "failed"), ("", "failed"),
    ("RESOURCE_EXHAUSTED", "failed"),
    ("DEADLINE_EXCEEDED", "timeout"),
])
def test_classify_reads_only_the_status_field(status, expected):
    assert classify(status)[0] == expected


def test_parse_output_never_raises_on_junk():
    for junk in ("", "   ", "{", "[1,2,3]", "null", '{"status":'):
        result = parse_output("t", junk, "", 1, "m", "s", "f", timed_out=False)
        assert result.status in ("unparseable", "failed")


# ----------------------------------------------------------- command / env
def test_build_command_has_the_proven_shape(cfg, workspace):
    from conftest import make_task

    cmd = build_command(make_task("a", instruction="do it"), workspace, cfg)
    assert "--output-format" in cmd and "json" in cmd
    assert "--add-dir" in cmd and str(workspace) in cmd
    assert cmd[cmd.index("--print") + 1] == "do it"
    assert f"{cfg.worker_timeout_s}s" in cmd


def test_build_command_passes_dangerously_skip_permissions(cfg, workspace):
    """Required, and not the blanket removal of safety it sounds like.

    Measured against real agy: headless mode auto-APPROVES file tools whether
    or not this flag is present, but auto-DENIES the `command` tool without
    it -- and then still reports status SUCCESS with an empty response and a
    `denied_actions` entry. A worker asked to run tests would be silently
    blocked while appearing to succeed.

    A worker has no human to prompt, so a denial here is a failure mode rather
    than a safeguard. The human gate is the parent's execute_plan prompt.
    """
    from conftest import make_task

    assert "--dangerously-skip-permissions" in build_command(make_task("a"), workspace, cfg)


def test_worker_env_carries_the_depth_marker():
    env = worker_env()
    assert env[DEPTH_ENV_VAR] == "1"
    assert "PATH" in env, "the worker needs PATH to reach other tools and MCP servers"


# ------------------------------------------------- SUCCESS that isn't
# Three measured cases where agy exits 0 with status SUCCESS having done
# nothing. Taking the status field at face value would record incomplete work
# as a success, which is the precise failure this project exists to avoid.

def _doc(**over) -> str:
    import json as _json

    base = {
        "conversation_id": "abc", "status": "SUCCESS", "response": "did the thing",
        "duration_seconds": 2.9, "num_turns": 1,
        "usage": {"input_tokens": 12715, "output_tokens": 355,
                  "thinking_tokens": 239, "cache_read_tokens": 0},
    }
    base.update(over)
    return _json.dumps(base)


def test_agy_own_print_timeout_is_not_a_success():
    """agy emits status SUCCESS with an empty response when its own
    --print-timeout fires; the banner is on stderr."""
    result = parse_output(
        "t", _doc(response=""),
        "[agy] print timeout after 6s with turn in progress; returning partial output",
        0, "m", "s", "f", timed_out=False,
    )
    assert result.status == "timeout"
    assert "own --print-timeout" in result.exit_reason
    assert "INCOMPLETE" in result.summary


def test_denied_tool_permission_is_not_a_success():
    """Without --dangerously-skip-permissions the `command` tool is auto-denied
    in headless mode, and agy still reports SUCCESS."""
    result = parse_output(
        "t", _doc(response="", denied_actions=[{"action": "command", "display_name": "RunCommand"}]),
        "jetski: no output produced -- a tool required the \"command\" permission",
        0, "m", "s", "f", timed_out=False,
    )
    assert result.status == "failed"
    assert "RunCommand" in result.exit_reason
    assert "blocked" in result.exit_reason


def test_success_with_no_answer_is_not_a_success():
    """A worker that produced nothing did not succeed, whatever status says."""
    result = parse_output("t", _doc(response="   "), "", 0, "m", "s", "f", timed_out=False)
    assert result.status == "failed"
    assert "no answer" in result.exit_reason


def test_a_real_success_is_still_ok():
    """The hardening must not turn genuine successes into failures."""
    result = parse_output("t", _doc(), "", 0, "m", "s", "f", timed_out=False)
    assert result.ok and result.exit_reason == "success"
    assert result.tokens_in == 12715


def test_timeout_banner_is_matched_on_stderr_only():
    """Scoped like the D2 fix: a worker discussing timeouts in its own answer
    must not be reclassified."""
    result = parse_output(
        "t", _doc(response="I fixed the print timeout after the retry loop."),
        "", 0, "m", "s", "f", timed_out=False,
    )
    assert result.ok, result.exit_reason
