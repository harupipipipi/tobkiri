"""Structural types for the Windows-only standard-library ABI.

These describe existing modules at guarded call sites. They never load Windows
libraries on POSIX or replace native access checks and handle ownership.
"""
from __future__ import annotations

import ctypes
from collections.abc import Mapping
from typing import Protocol


class WindowsCTypes(Protocol):
    def WinDLL(
        self,
        name: str,
        *,
        use_last_error: bool = False,
        winmode: int | None = None,
    ) -> ctypes.CDLL: ...

    def get_last_error(self) -> int: ...

class WindowsCRuntime(Protocol):
    LK_NBLCK: int
    LK_UNLCK: int

    def get_osfhandle(self, fd: int, /) -> int: ...
    def open_osfhandle(self, handle: int, flags: int, /) -> int: ...
    def locking(self, fd: int, mode: int, nbytes: int, /) -> None: ...

class WindowsOS(Protocol):
    O_BINARY: int

    def set_handle_inheritable(
        self, handle: int, inheritable: bool, /
    ) -> None: ...

class StartupInfo(Protocol):
    dwFlags: int
    hStdInput: int | None
    hStdOutput: int | None
    hStdError: int | None
    lpAttributeList: Mapping[str, list[int]]

class WindowsSubprocess(Protocol):
    STARTF_USESTDHANDLES: int

    def STARTUPINFO(self) -> StartupInfo: ...

class WindowsAPI(Protocol):
    def CreateProcess(
        self,
        application_name: str | None,
        command_line: str | None,
        proc_attrs: None,
        thread_attrs: None,
        inherit_handles: bool,
        creation_flags: int,
        env_mapping: dict[str, str],
        current_directory: str | None,
        startup_info: StartupInfo,
        /,
    ) -> tuple[int, int, int, int]: ...

    def CloseHandle(self, handle: int, /) -> None: ...
    def TerminateProcess(self, handle: int, exit_code: int, /) -> None: ...
    def WaitForSingleObject(self, handle: int, milliseconds: int, /) -> int: ...
    def GetExitCodeProcess(self, process: int, /) -> int: ...
