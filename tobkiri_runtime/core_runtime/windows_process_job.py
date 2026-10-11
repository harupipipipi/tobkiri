"""Windows process-tree ownership for Host-spawned suspended children.

Matches the Launcher's WindowsJob lifecycle. This is containment for process
lifetime, not an OS sandbox or permission grant.
"""

from __future__ import annotations

import ctypes as c
import time
from typing import Any

CREATE_SUSPENDED = 0x00000004
_KILL_ON_JOB_CLOSE = 0x00002000
_TH32CS_SNAPTHREAD = 0x00000004
_THREAD_SUSPEND_RESUME = 0x0002


class _BasicLimits(c.Structure):
    _fields_ = [
        ("process_time", c.c_int64), ("job_time", c.c_int64),
        ("flags", c.c_uint32), ("minimum_working_set", c.c_size_t),
        ("maximum_working_set", c.c_size_t), ("active_limit", c.c_uint32),
        ("affinity", c.c_size_t), ("priority", c.c_uint32),
        ("scheduling", c.c_uint32),
    ]


class _ExtendedLimits(c.Structure):
    _fields_ = [
        ("basic", _BasicLimits), ("io_counters", c.c_uint64 * 6),
        ("process_memory", c.c_size_t), ("job_memory", c.c_size_t),
        ("peak_process_memory", c.c_size_t), ("peak_job_memory", c.c_size_t),
    ]


class _Accounting(c.Structure):
    _fields_ = [
        ("times", c.c_int64 * 4), ("page_faults", c.c_uint32),
        ("total_processes", c.c_uint32), ("active_processes", c.c_uint32),
        ("terminated_processes", c.c_uint32),
    ]


class _ThreadEntry(c.Structure):
    _fields_ = [
        ("size", c.c_uint32), ("usage", c.c_uint32),
        ("thread_id", c.c_uint32), ("owner_pid", c.c_uint32),
        ("base_priority", c.c_int32), ("delta_priority", c.c_int32),
        ("flags", c.c_uint32),
    ]


def _kernel_api() -> Any:
    api = getattr(c, "WinDLL")("kernel32", use_last_error=True)
    handle, dword, boolean = c.c_void_p, c.c_uint32, c.c_int32
    signatures = {
        "CreateJobObjectW": ([handle, c.c_wchar_p], handle),
        "SetInformationJobObject": ([handle, c.c_int32, handle, dword], boolean),
        "AssignProcessToJobObject": ([handle, handle], boolean),
        "TerminateJobObject": ([handle, dword], boolean),
        "QueryInformationJobObject": ([handle, c.c_int32, handle, dword, handle], boolean),
        "CreateToolhelp32Snapshot": ([dword, dword], handle),
        "Thread32First": ([handle, c.POINTER(_ThreadEntry)], boolean),
        "Thread32Next": ([handle, c.POINTER(_ThreadEntry)], boolean),
        "OpenThread": ([dword, boolean, dword], handle),
        "ResumeThread": ([handle], dword),
        "CloseHandle": ([handle], boolean),
    }
    for name, (arguments, result) in signatures.items():
        function = getattr(api, name)
        function.argtypes, function.restype = arguments, result
    return api


class WindowsProcessJob:
    """Own one unnamed, non-inheritable Job with breakaway disabled."""

    def __init__(self) -> None:
        self._api = _kernel_api()
        self._handle = self._api.CreateJobObjectW(None, None)
        if not self._handle:
            raise OSError("Windows process Job creation failed")
        limits = _ExtendedLimits()
        limits.basic.flags = _KILL_ON_JOB_CLOSE
        if not self._api.SetInformationJobObject(
            self._handle, 9, c.byref(limits), c.sizeof(limits),
        ):
            self.close()
            raise OSError("Windows process Job configuration failed")

    def assign_and_resume(self, process_handle: int, pid: int) -> None:
        """Attach before the suspended primary thread can create children."""
        if not self._api.AssignProcessToJobObject(self._handle, process_handle):
            raise OSError("Windows process Job assignment failed")
        snapshot = self._api.CreateToolhelp32Snapshot(_TH32CS_SNAPTHREAD, 0)
        if snapshot in (None, c.c_void_p(-1).value):
            raise OSError("Windows suspended thread snapshot failed")
        try:
            entry = _ThreadEntry()
            entry.size = c.sizeof(entry)
            available = self._api.Thread32First(snapshot, c.byref(entry))
            while available:
                if entry.owner_pid == pid:
                    thread = self._api.OpenThread(_THREAD_SUSPEND_RESUME, False, entry.thread_id)
                    if not thread:
                        raise OSError("Windows suspended thread open failed")
                    try:
                        if self._api.ResumeThread(thread) != 1:
                            raise OSError("Windows suspended thread resume failed")
                    finally:
                        self._api.CloseHandle(thread)
                    return
                entry.size = c.sizeof(entry)
                available = self._api.Thread32Next(snapshot, c.byref(entry))
            raise OSError("Windows suspended primary thread is unavailable")
        finally:
            self._api.CloseHandle(snapshot)

    def terminate(self, timeout_seconds: float) -> bool:
        """Request tree termination and confirm no active Job processes remain."""
        if not self._handle or not self._api.TerminateJobObject(self._handle, 1):
            return False
        deadline = time.monotonic() + timeout_seconds
        while True:
            accounting = _Accounting()
            if not self._api.QueryInformationJobObject(
                self._handle, 1, c.byref(accounting), c.sizeof(accounting), None,
            ):
                return False
            if accounting.active_processes == 0:
                return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.01)

    def close(self) -> None:
        """Close the sole Job handle, terminating any remaining children."""
        if self._handle:
            handle, self._handle = self._handle, None
            if not self._api.CloseHandle(handle):
                raise OSError("Windows process Job close failed")
