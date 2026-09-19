"""Run one ephemeral agy worker and turn its output into a record.

Two measured behaviours shape this module, and both are easy to get wrong:

1. `asyncio.wait_for(proc.communicate(), t)` DISCARDS partial output when it
   times out. A child that flushed real work before hanging yields b''. So the
   streams are drained by tasks that own their buffers, and a timeout cannot
   take that work away.

2. `proc.kill()` leaves grandchildren running (see jobobject.py). A killed
   worker must be killed as a tree, or orphans keep writing to the workspace
   after we have recorded the worker as done.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .config import Config
from .jobobject import ProcessGroup, ProcessGroupError
from .models import Task

log = logging.getLogger("subagents.worker")

SUMMARY_CHARS = 400

# Worker sessions are marked so our own server can refuse to fan out again.
# Verified to reach the worker process.
DEPTH_ENV_VAR = "SUBAGENTS_DEPTH"

# Matched against the JSON `status` field only -- never against the transcript.
# Bench defect D2 was a whole-log grep for "429" matching a conversation_id and
# voiding a run that had actually succeeded.
_RATE_LIMIT_MARKERS = ("rate_limit", "rate limit", "quota", "resource_exhausted")
_TIMEOUT_MARKERS = ("timeout", "deadline")


@dataclass(frozen=True)
class WorkerResult:
    task_ref: str
    status: str            # ok | failed | timeout | unparseable | spawn_error
    exit_reason: str
    transcript: str        # everything the worker emitted, stdout + stderr
    summary: str           # short, for the parent
    agy_status: str
    model_used: str
    returncode: int | None
    started_at: str
    finished_at: str
    duration_s: float | None = None
    num_turns: int | None = None
    tokens_in: int | None = None
    tokens_out: int | None = None
    thinking_tokens: int | None = None
    cache_read_tokens: int | None = None

    @property
    def ok(self) -> bool:
        return self.status == "ok"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def build_command(task: Task, workspace_root: Path, config: Config, model: str | None = None) -> list[str]:
    """The invocation shape proven by bench/run.sh across 33 runs.

    `--dangerously-skip-permissions` is deliberately absent. Print mode
    auto-approves tool calls anyway (measured), so passing it would remove
    nothing that exists while implying the worker had a guardrail we took off.
    """
    return [
        config.agy_path,
        "--model", model or task.model or config.model,
        "--print-timeout", f"{config.worker_timeout_s}s",
        "--add-dir", str(workspace_root),
        "--output-format", "json",
        "--print", task.instruction,
    ]


def worker_env(depth: int = 1) -> dict[str, str]:
    """Full inheritance plus the recursion marker.

    We spawn directly, so unlike the SDK's stdio client there is no allow-list
    filtering here -- the worker needs its own PATH, credentials and config to
    reach other MCP servers, tools and skills, which it is meant to keep.
    """
    env = dict(os.environ)
    env[DEPTH_ENV_VAR] = str(depth)
    return env


async def _drain(stream: asyncio.StreamReader | None, sink: list[bytes]) -> None:
    if stream is None:
        return
    while True:
        chunk = await stream.read(65536)
        if not chunk:
            break
        sink.append(chunk)


def _decode(chunks: list[bytes]) -> str:
    return b"".join(chunks).decode("utf-8", errors="replace")


def _process_alive(pid: int) -> bool:
    """Liveness of one pid, independent of any pipe."""
    if sys.platform == "win32":
        import ctypes

        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        handle = k32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            if not k32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return False
            return code.value == 259  # STILL_ACTIVE
        finally:
            k32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, PermissionError):
        return False


async def _await_exit(pid: int, timeout_s: float, poll_s: float = 0.05) -> bool:
    """Wait for the WORKER PROCESS to exit. Returns False on timeout.

    Deliberately not `proc.wait()`. Measured: asyncio's subprocess transport
    completes on pipe EOF, and a grandchild that inherited the worker's stdout
    holds those pipes open after the worker itself has exited -- so
    `proc.wait()` blocks for as long as the grandchild lives. A worker that
    starts any background process would then be reported as a timeout despite
    having succeeded, and its process tree would be killed for no reason.

    Pipe EOF is not process exit. This polls the process.
    """
    deadline = asyncio.get_running_loop().time() + timeout_s
    while True:
        if not _process_alive(pid):
            return True
        if asyncio.get_running_loop().time() >= deadline:
            return False
        await asyncio.sleep(poll_s)


async def _finish_readers(readers: list[asyncio.Task], grace_s: float = 2.0) -> None:
    """Let the drain tasks finish, but never wait on EOF indefinitely.

    Same reason as above: an inherited pipe may never reach EOF, so the
    readers are cancelled after a grace period once the worker is gone.
    Whatever they buffered is already in our lists.
    """
    done, pending = await asyncio.wait(readers, timeout=grace_s)
    for task in pending:
        task.cancel()
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)


def classify(agy_status: str) -> tuple[str, str]:
    """(status, exit_reason) from the JSON status field alone."""
    normalised = (agy_status or "").strip()
    if normalised.upper() == "SUCCESS":
        return "ok", "success"
    low = normalised.lower()
    if any(marker in low for marker in _RATE_LIMIT_MARKERS):
        return "failed", f"rate_limited ({normalised})"
    if any(marker in low for marker in _TIMEOUT_MARKERS):
        return "timeout", f"agy reported {normalised}"
    return "failed", f"agy status {normalised or 'missing'}"


def _summarise(response: str, status: str, exit_reason: str) -> str:
    if status == "timeout":
        head = response.strip()[:SUMMARY_CHARS]
        return (
            f"INCOMPLETE: worker was killed at its deadline ({exit_reason}). "
            f"Partial output retained." + (f" Last text: {head}" if head else "")
        )
    if status != "ok":
        return f"FAILED: {exit_reason}"
    text = " ".join(response.split())
    return text[:SUMMARY_CHARS] + ("..." if len(text) > SUMMARY_CHARS else "")


def parse_output(task_ref: str, stdout: str, stderr: str, returncode: int | None,
                 model: str, started: str, finished: str, timed_out: bool) -> WorkerResult:
    """Build a record from whatever the worker produced.

    A killed worker emits truncated or absent JSON, so this must never raise.
    """
    transcript = stdout if not stderr else f"{stdout}\n--- stderr ---\n{stderr}"

    doc = None
    try:
        doc = json.loads(stdout)
        if not isinstance(doc, dict):
            doc = None
    except (json.JSONDecodeError, ValueError):
        doc = None

    if doc is None:
        if timed_out:
            status, exit_reason, agy_status = "timeout", "killed at deadline", ""
        else:
            status, exit_reason, agy_status = (
                "unparseable",
                "worker produced no valid JSON document",
                "",
            )
        return WorkerResult(
            task_ref=task_ref,
            status=status,
            exit_reason=exit_reason,
            transcript=transcript,
            summary=_summarise("", status, exit_reason),
            agy_status=agy_status,
            model_used=model,
            returncode=returncode,
            started_at=started,
            finished_at=finished,
        )

    agy_status = str(doc.get("status", ""))
    status, exit_reason = classify(agy_status)
    if timed_out:
        status, exit_reason = "timeout", "killed at deadline"

    usage = doc.get("usage") or {}
    response = str(doc.get("response", ""))

    def _int(key: str) -> int | None:
        value = usage.get(key)
        return int(value) if isinstance(value, (int, float)) else None

    return WorkerResult(
        task_ref=task_ref,
        status=status,
        exit_reason=exit_reason,
        transcript=transcript,
        summary=_summarise(response, status, exit_reason),
        agy_status=agy_status,
        model_used=model,
        returncode=returncode,
        started_at=started,
        finished_at=finished,
        duration_s=float(doc["duration_seconds"]) if isinstance(doc.get("duration_seconds"), (int, float)) else None,
        num_turns=int(doc["num_turns"]) if isinstance(doc.get("num_turns"), (int, float)) else None,
        tokens_in=_int("input_tokens"),
        tokens_out=_int("output_tokens"),
        thinking_tokens=_int("thinking_tokens"),
        cache_read_tokens=_int("cache_read_tokens"),
    )


async def run_worker(
    task_ref: str,
    command: list[str],
    *,
    timeout_s: int,
    cwd: str | Path,
    model: str,
    env: dict[str, str] | None = None,
) -> WorkerResult:
    """Spawn, capture, and kill-on-timeout as a tree."""
    started = _now()
    group = ProcessGroup()
    proc = None
    out_chunks: list[bytes] = []
    err_chunks: list[bytes] = []
    timed_out = False

    try:
        try:
            proc = await asyncio.create_subprocess_exec(
                *command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(cwd),
                env=env if env is not None else worker_env(),
                **group.spawn_kwargs(),
            )
        except (OSError, ValueError) as exc:
            return WorkerResult(
                task_ref=task_ref,
                status="spawn_error",
                exit_reason=f"could not start worker: {exc}",
                transcript="",
                summary=f"FAILED: could not start worker: {exc}",
                agy_status="",
                model_used=model,
                returncode=None,
                started_at=started,
                finished_at=_now(),
            )

        try:
            group.attach(proc.pid)
        except ProcessGroupError:
            # Loud, never silent: without the job we cannot guarantee the tree
            # dies, so refuse to run rather than leave orphans behind later.
            proc.kill()
            await proc.wait()
            raise

        readers = [
            asyncio.create_task(_drain(proc.stdout, out_chunks)),
            asyncio.create_task(_drain(proc.stderr, err_chunks)),
        ]

        exited = await _await_exit(proc.pid, timeout_s)
        if not exited:
            timed_out = True
            log.warning("worker %s exceeded %ss; killing process tree", task_ref, timeout_s)
            group.terminate()
            await _await_exit(proc.pid, 5.0)

        # The buffers belong to the reader tasks, so whatever already arrived
        # survives even when the worker was killed.
        await _finish_readers(readers)

        returncode = proc.returncode
        if returncode is None:
            # asyncio never observed the exit (pipes still held open), so ask
            # the process group to tidy up and record what we can.
            try:
                proc.kill()
            except (ProcessLookupError, OSError):
                pass
            group.terminate()

        return parse_output(
            task_ref=task_ref,
            stdout=_decode(out_chunks),
            stderr=_decode(err_chunks),
            returncode=returncode,
            model=model,
            started=started,
            finished=_now(),
            timed_out=timed_out,
        )
    finally:
        # A pipe held open by a grandchild leaves asyncio's transport
        # unfinalised, which surfaces later as a ResourceWarning from __del__
        # on a closed pipe. There is no public API for this, so close it
        # defensively rather than leaking a handle per worker -- at fan-out
        # scale that would accumulate.
        if proc is not None:
            transport = getattr(proc, "_transport", None)
            if transport is not None:
                try:
                    transport.close()
                except (OSError, ValueError, AttributeError):
                    pass
        group.close()
