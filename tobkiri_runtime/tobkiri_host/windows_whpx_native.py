"""Windows child ownership: suspended creation, private pipes and a kill Job.

QEMU never runs before it is assigned to a kill-on-close Job. The only child
handles are its dedicated stdin/stdout and NUL stderr. There is no listening
network endpoint or ambient inheritable-handle authority.
"""

from __future__ import annotations

import ctypes
import os
import subprocess
import threading
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, BinaryIO

_SPAWN_LOCK = threading.Lock()
_DWORD = ctypes.c_uint32
_SIZE = ctypes.c_size_t
_HANDLE = ctypes.c_void_p


class _BasicLimits(ctypes.Structure):
    _fields_ = [
        ("process_time", ctypes.c_int64),
        ("job_time", ctypes.c_int64),
        ("flags", _DWORD),
        ("minimum_working_set", _SIZE),
        ("maximum_working_set", _SIZE),
        ("active_process_limit", _DWORD),
        ("affinity", _SIZE),
        ("priority", _DWORD),
        ("scheduling", _DWORD),
    ]


class _IoCounters(ctypes.Structure):
    _fields_ = [
        (name, ctypes.c_uint64)
        for name in (
            "read_ops",
            "write_ops",
            "other_ops",
            "read_bytes",
            "write_bytes",
            "other_bytes",
        )
    ]


class _ExtendedLimits(ctypes.Structure):
    _fields_ = [
        ("basic", _BasicLimits),
        ("io", _IoCounters),
        ("process_memory", _SIZE),
        ("job_memory", _SIZE),
        ("peak_process_memory", _SIZE),
        ("peak_job_memory", _SIZE),
    ]


def _kernel() -> Any:
    dll = ctypes.WinDLL("kernel32", use_last_error=True, winmode=0x800)
    signatures = {
        "CreateJobObjectW": ([_HANDLE, ctypes.c_wchar_p], _HANDLE),
        "SetInformationJobObject": ([_HANDLE, ctypes.c_int, _HANDLE, _DWORD], ctypes.c_int),
        "AssignProcessToJobObject": ([_HANDLE, _HANDLE], ctypes.c_int),
        "ResumeThread": ([_HANDLE], _DWORD),
        "CloseHandle": ([_HANDLE], ctypes.c_int),
    }
    for name, (args, result) in signatures.items():
        fn = getattr(dll, name)
        fn.argtypes, fn.restype = args, result
    return dll


def _error(label: str) -> OSError:
    return OSError(getattr(ctypes, "get_last_error", lambda: 0)(), label)


class WindowsJobProcess:
    """One suspended-then-enrolled native child with Popen-like lifecycle.

    A bounded shutdown must succeed before allocation files can be removed.
    Job handles are non-inheritable and owned until the process is reaped.
    """

    def __init__(
        self,
        executable: Path,
        arguments: Sequence[str],
        *,
        cwd: Path,
        env: Mapping[str, str],
        memory_limit_bytes: int,
    ) -> None:
        if os.name != "nt":
            raise OSError("Windows Job processes require Windows")
        import _winapi
        import msvcrt

        self._api = _winapi
        self._kernel = _kernel()
        self._process: Any = None
        self._job: Any = None
        self.returncode: int | None = None
        self.pid: int | None = None
        self.stdin: BinaryIO | None = None
        self.stdout: BinaryIO | None = None
        descriptors: list[int] = []
        thread: Any = None
        try:
            self._job = self._kernel.CreateJobObjectW(None, None)
            if not self._job:
                raise _error("CreateJobObjectW failed")
            limits = _ExtendedLimits()
            # KILL_ON_JOB_CLOSE | ACTIVE_PROCESS | PROCESS_MEMORY. QEMU has no
            # reason to spawn children, and the limit is independent of guest RAM.
            limits.basic.flags = 0x2000 | 0x8 | 0x100
            limits.basic.active_process_limit = 1
            limits.process_memory = memory_limit_bytes
            if not self._kernel.SetInformationJobObject(
                self._job,
                9,
                ctypes.byref(limits),
                ctypes.sizeof(limits),
            ):
                raise _error("SetInformationJobObject failed")
            child_read, parent_write = os.pipe()
            descriptors.extend((child_read, parent_write))
            parent_read, child_write = os.pipe()
            descriptors.extend((parent_read, child_write))
            null = os.open(os.devnull, os.O_WRONLY | os.O_BINARY)
            descriptors.append(null)
            child_handles = [msvcrt.get_osfhandle(fd) for fd in (child_read, child_write, null)]
            startup = subprocess.STARTUPINFO()
            startup.dwFlags = subprocess.STARTF_USESTDHANDLES
            startup.hStdInput, startup.hStdOutput, startup.hStdError = child_handles
            startup.lpAttributeList = {"handle_list": child_handles}
            # CPython's _winapi adds EXTENDED_STARTUPINFO_PRESENT for the handle
            # list. Supplying the explicit flag also documents this requirement.
            flags = 0x4 | 0x08000000 | 0x00080000 | 0x400
            with _SPAWN_LOCK:
                try:
                    for handle in child_handles:
                        os.set_handle_inheritable(handle, True)
                    self._process, thread, self.pid, _ = _winapi.CreateProcess(
                        str(executable),
                        subprocess.list2cmdline(list(arguments)),
                        None,
                        None,
                        True,
                        flags,
                        dict(env),
                        str(cwd),
                        startup,
                    )
                finally:
                    for handle in child_handles:
                        os.set_handle_inheritable(handle, False)
            if not self._kernel.AssignProcessToJobObject(self._job, self._process):
                raise _error("QEMU could not be assigned to its ownership Job")
            if self._kernel.ResumeThread(thread) == 0xFFFFFFFF:
                raise _error("QEMU suspended thread could not resume")
            _winapi.CloseHandle(thread)
            thread = None
            for fd in (child_read, child_write, null):
                os.close(fd)
                descriptors.remove(fd)
            self.stdin = os.fdopen(parent_write, "wb", buffering=0)
            descriptors.remove(parent_write)
            self.stdout = os.fdopen(parent_read, "rb", buffering=0)
            descriptors.remove(parent_read)
        except BaseException:
            # Always close the Job even if a termination call itself fails.
            # An assigned child then cannot outlive the failed constructor.
            try:
                if self._process is not None:
                    try:
                        _winapi.TerminateProcess(self._process, 1)
                    finally:
                        _winapi.WaitForSingleObject(self._process, 5000)
            finally:
                if self._job:
                    self._kernel.CloseHandle(self._job)
                    self._job = None
                if self._process is not None:
                    _winapi.CloseHandle(self._process)
                    self._process = None
                for stream in (self.stdin, self.stdout):
                    if stream is not None:
                        stream.close()
            raise
        finally:
            if thread is not None:
                _winapi.CloseHandle(thread)
            for fd in descriptors:
                os.close(fd)

    def poll(self) -> int | None:
        """Return None only while the exact owned process is still running."""
        if self.returncode is not None:
            return self.returncode
        if self._process is None:
            return self.returncode
        if self._api.WaitForSingleObject(self._process, 0) == 0:
            self.returncode = self._api.GetExitCodeProcess(self._process)
        return self.returncode

    def wait(self, timeout: float | None = None) -> int:
        """Wait for process termination without replacing the process identity."""
        if self.returncode is not None:
            return self.returncode
        milliseconds = (
            0xFFFFFFFF if timeout is None else max(0, min(0xFFFFFFFE, int(timeout * 1000)))
        )
        status = self._api.WaitForSingleObject(self._process, milliseconds)
        if status == 0x102:
            raise subprocess.TimeoutExpired("Tobkiri QEMU", timeout)
        if status != 0:
            raise _error("QEMU process wait failed")
        self.returncode = self._api.GetExitCodeProcess(self._process)
        return self.returncode

    def terminate(self) -> None:
        """Terminate only the exact retained process handle."""
        if self.poll() is None:
            self._api.TerminateProcess(self._process, 1)

    kill = terminate

    def close(self) -> None:
        """Reap first, then release all pipes and native ownership handles."""
        self.terminate()
        self.wait(5)
        for stream in (self.stdin, self.stdout):
            if stream is not None:
                stream.close()
        if self._process is not None:
            self._api.CloseHandle(self._process)
            self._process = None
        if self._job:
            self._kernel.CloseHandle(self._job)
            self._job = None
