"""Read-only Windows Hypervisor Platform capability checks.

No optional features, drivers, boot settings, or host permissions are changed.
A missing hypervisor is a setup blocker, never grounds to fall back to TCG.
"""

from __future__ import annotations

import ctypes
import platform
from typing import Any, cast

from .windows_abi_types import WindowsCTypes


def _hypervisor_present(library: Any) -> bool:
    """Query WHvCapabilityCodeHypervisorPresent using its documented ABI."""
    query = library.WHvGetCapability
    query.argtypes = (
        ctypes.c_uint32,
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.POINTER(ctypes.c_uint32),
    )
    query.restype = ctypes.c_int32  # HRESULT is signed 32-bit, including on x64.
    # The documented output capacity is at least 64 bits, even though this
    # union member is BOOL. Accept its 4-byte result (or zero-padded union).
    present = ctypes.c_uint64()
    written = ctypes.c_uint32()
    status = query(0, ctypes.byref(present), ctypes.sizeof(present), ctypes.byref(written))
    if status < 0 or written.value not in (4, 8) or present.value > 0xFFFFFFFF:
        raise OSError("Windows Hypervisor Platform capability query failed")
    return present.value != 0


def whpx_capability() -> tuple[bool, str | None]:
    """Report supported WHPX availability without mutating host configuration."""
    if platform.system() != "Windows" or platform.machine().lower() not in (
        "amd64",
        "x86_64",
    ):
        return False, "Windows PackVM currently requires Windows x86_64"
    try:
        loader = cast(WindowsCTypes, ctypes).WinDLL
        # LOAD_LIBRARY_SEARCH_SYSTEM32 prevents a DLL from the current directory
        # or PATH from masquerading as the operating-system hypervisor API.
        api = loader("WinHvPlatform.dll", use_last_error=True, winmode=0x800)
        if not _hypervisor_present(api):
            return False, (
                "Windows Hypervisor Platform is not active. Enable it in Windows "
                "Features with your approval, then restart if Windows asks. "
                "Tobkiri will not use TCG as a production isolation fallback."
            )
    except (AttributeError, OSError):
        return False, (
            "Windows Hypervisor Platform could not be verified. Review the "
            "Windows Hypervisor Platform feature and firmware virtualization "
            "settings; Tobkiri does not change them automatically."
        )
    return True, None
