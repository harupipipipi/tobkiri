"""Portable fake-native contracts for atomic owner-only Windows creation."""

import ctypes
from types import SimpleNamespace

import pytest


import tobkiri_protocol.windows_private_file as private

class Native:
    def __init__(self, fail=None):
        self.fail = fail
        self.freed = []
        self.closed = []
        self.sddl = []
        self.token = 0x123456789
        self.sid = ctypes.create_unicode_buffer("S-1-5-21-1234-5678-1001")
        self.descriptor = ctypes.create_string_buffer(64)
        self.kernel = SimpleNamespace(
            GetCurrentProcess=lambda: ctypes.c_void_p(-1).value,
            CloseHandle=self.close,
            LocalFree=self.free,
        )
        self.advapi = SimpleNamespace(
            OpenProcessToken=self.open_token,
            GetTokenInformation=self.information,
            ConvertSidToStringSidW=self.convert_sid,
            ConvertStringSecurityDescriptorToSecurityDescriptorW=self.convert_descriptor,
        )

    def open_token(self, process, access, output):
        assert process == ctypes.c_void_p(-1).value
        assert access == 8
        if self.fail == "open":
            return 0
        output._obj.value = self.token
        return 1

    def information(self, token, category, buffer, size, required):
        assert token.value == self.token
        assert category == 1
        required._obj.value = ctypes.sizeof(private._TokenUser)
        if buffer is None:
            assert size == 0
            return 0
        if self.fail == "information":
            return 0
        user = ctypes.cast(buffer, ctypes.POINTER(private._TokenUser)).contents
        user.user.sid = 0x23456789A
        return 1

    def convert_sid(self, sid, output):
        assert sid == 0x23456789A
        if self.fail == "sid":
            return 0
        output._obj.value = ctypes.addressof(self.sid)
        return 1

    def convert_descriptor(self, sddl, revision, output, size):
        self.sddl.append(sddl)
        assert revision == 1 and size is None
        if self.fail == "descriptor":
            return 0
        output._obj.value = ctypes.addressof(self.descriptor)
        return 1

    def free(self, allocation):
        self.freed.append(allocation.value)
        return allocation.value if self.fail == "free" else None

    def close(self, handle):
        self.closed.append(handle.value)
        return int(self.fail != "close")


def install(monkeypatch, native):
    monkeypatch.setattr(private, "_security_apis", lambda: (native.kernel, native.advapi))
    monkeypatch.setattr(private, "_last_error", lambda: 122)


def test_atomic_descriptor_is_protected_exact_user_and_lifetime_bound(monkeypatch):
    native = Native()
    install(monkeypatch, native)
    with private.owner_only_security_attributes() as attributes:
        assert attributes.nLength == ctypes.sizeof(private.SecurityAttributes)
        assert attributes.bInheritHandle == 0
        assert attributes.lpSecurityDescriptor == ctypes.addressof(native.descriptor)
        assert native.freed == [] and native.closed == []
        assert native.sddl == [
            "O:S-1-5-21-1234-5678-1001D:P(A;;FA;;;S-1-5-21-1234-5678-1001)"
        ]
    assert native.freed == [
        ctypes.addressof(native.descriptor), ctypes.addressof(native.sid)
    ]
    assert native.closed == [native.token]


@pytest.mark.parametrize(
    "failure, freed, closed",
    [("open", 0, 0), ("information", 0, 1), ("sid", 0, 1), ("descriptor", 1, 1)],
)
def test_api_failure_fails_closed_and_cleans_allocations(monkeypatch, failure, freed, closed):
    native = Native(failure)
    install(monkeypatch, native)
    with pytest.raises(OSError):
        with private.owner_only_security_attributes():
            pytest.fail("attributes yielded after native API failure")
    assert len(native.freed) == freed
    assert len(native.closed) == closed


@pytest.mark.parametrize("failure", ["free", "close"])
def test_cleanup_failure_is_reported_after_all_cleanup_attempts(monkeypatch, failure):
    native = Native(failure)
    install(monkeypatch, native)
    with pytest.raises(OSError):
        with private.owner_only_security_attributes():
            pass
    assert len(native.freed) == 2
    assert native.closed == [native.token]


def test_caller_exception_still_releases_native_resources(monkeypatch):
    native = Native()
    install(monkeypatch, native)
    with pytest.raises(RuntimeError, match="caller failed"):
        with private.owner_only_security_attributes():
            raise RuntimeError("caller failed")
    assert len(native.freed) == 2
    assert native.closed == [native.token]
