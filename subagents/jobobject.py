"""Kill a worker and everything it spawned.

Measured on Windows 11: `proc.kill()` on a child leaves its grandchildren
running. A real worker spawns processes (test runs, tooling), so a plain kill
at a timeout leaves them alive -- still holding CPU, and still able to write to
the workspace *after* we have recorded the worker as finished. That last part
is what makes it a correctness problem and not just untidiness: Phase 3 hashes
files after a worker exits, and an orphan writing during that window would
corrupt the comparison at its root.

Windows: a Job Object with JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE, verified to take
the whole tree down.
POSIX: a new session plus killpg, the equivalent idea.

A failed attach is LOUD. A Job Object that silently fails to bind restores
exactly the bug it exists to prevent, so this never degrades quietly.
"""

from __future__ import annotations

import logging
import os
import signal
import sys

log = logging.getLogger("subagents.jobobject")

IS_WINDOWS = sys.platform == "win32"

if IS_WINDOWS:
    import ctypes
    import ctypes.wintypes as wintypes

    _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
    _JobObjectExtendedLimitInformation = 9
    _PROCESS_ALL_ACCESS = 0x1F0FFF

    class _BasicLimitInformation(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_int64),
            ("PerJobUserTimeLimit", ctypes.c_int64),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.POINTER(ctypes.c_ulong)),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class _IoCounters(ctypes.Structure):
        _fields_ = [
            ("ReadOperationCount", ctypes.c_uint64),
            ("WriteOperationCount", ctypes.c_uint64),
            ("OtherOperationCount", ctypes.c_uint64),
            ("ReadTransferCount", ctypes.c_uint64),
            ("WriteTransferCount", ctypes.c_uint64),
            ("OtherTransferCount", ctypes.c_uint64),
        ]

    class _ExtendedLimitInformation(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", _BasicLimitInformation),
            ("IoInfo", _IoCounters),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    _k32 = ctypes.WinDLL("kernel32", use_last_error=True)


class ProcessGroupError(RuntimeError):
    """The kill mechanism could not be established. Never swallowed."""


class ProcessGroup:
    """Owns the lifetime of a worker process tree.

    Usage:
        group = ProcessGroup()
        proc = await asyncio.create_subprocess_exec(*cmd, **group.spawn_kwargs())
        group.attach(proc.pid)
        ...
        group.terminate()      # kills the tree
        group.close()
    """

    def __init__(self) -> None:
        self._handle = None
        self._pid: int | None = None
        self._closed = False

        if IS_WINDOWS:
            handle = _k32.CreateJobObjectW(None, None)
            if not handle:
                raise ProcessGroupError(
                    f"CreateJobObjectW failed (error {ctypes.get_last_error()})"
                )
            info = _ExtendedLimitInformation()
            info.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            ok = _k32.SetInformationJobObject(
                handle,
                _JobObjectExtendedLimitInformation,
                ctypes.byref(info),
                ctypes.sizeof(info),
            )
            if not ok:
                err = ctypes.get_last_error()
                _k32.CloseHandle(handle)
                raise ProcessGroupError(
                    f"SetInformationJobObject failed (error {err}); without "
                    f"KILL_ON_JOB_CLOSE the job would not kill the tree"
                )
            self._handle = handle

    def spawn_kwargs(self) -> dict:
        """Platform kwargs for create_subprocess_exec."""
        if IS_WINDOWS:
            return {}
        # New session so the children share a process group id we can signal.
        return {"start_new_session": True}

    def attach(self, pid: int) -> None:
        """Bind a freshly spawned process. Raises rather than degrading.

        There is a small race on Windows between CreateProcess returning and
        the assignment landing: a grandchild spawned inside that window escapes
        the job. It is microseconds against a worker that takes seconds to
        start, so it is accepted and documented rather than worked around with
        CREATE_SUSPENDED, which asyncio does not expose a way to resume.
        """
        self._pid = pid
        if not IS_WINDOWS:
            return

        proc_handle = _k32.OpenProcess(_PROCESS_ALL_ACCESS, False, pid)
        if not proc_handle:
            raise ProcessGroupError(
                f"OpenProcess({pid}) failed (error {ctypes.get_last_error()})"
            )
        try:
            if not _k32.AssignProcessToJobObject(self._handle, proc_handle):
                raise ProcessGroupError(
                    f"AssignProcessToJobObject failed for pid {pid} "
                    f"(error {ctypes.get_last_error()}); the worker's children "
                    f"would survive a kill"
                )
        finally:
            _k32.CloseHandle(proc_handle)

    def terminate(self) -> None:
        """Kill the worker and every process it spawned. Safe to call twice."""
        if IS_WINDOWS:
            if self._handle:
                _k32.TerminateJobObject(self._handle, 1)
            return

        if self._pid is None:
            return
        try:
            os.killpg(os.getpgid(self._pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass  # already gone

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if IS_WINDOWS and self._handle:
            _k32.CloseHandle(self._handle)
            self._handle = None

    def __enter__(self) -> ProcessGroup:
        return self

    def __exit__(self, *exc) -> None:
        self.close()
