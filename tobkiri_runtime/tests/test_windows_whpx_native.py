"""Native Windows ownership mocks; real CreateProcess/Job execution is separate."""

import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from tobkiri_host import windows_whpx_native as native


class Kernel:
    def __init__(self, calls, fail=None):
        self.calls, self.fail = calls, fail

    def CreateJobObjectW(self, *args):
        self.calls.append(("job",))
        return 100

    def SetInformationJobObject(self, job, kind, pointer, size):
        limits = pointer._obj
        self.calls.append(
            (
                "limits",
                job,
                kind,
                limits.basic.flags,
                limits.basic.active_process_limit,
                limits.process_memory,
            )
        )
        return self.fail != "limits"

    def AssignProcessToJobObject(self, job, process):
        self.calls.append(("assign", job, process))
        return self.fail != "assign"

    def ResumeThread(self, thread):
        self.calls.append(("resume", thread))
        return 0xFFFFFFFF if self.fail == "resume" else 1

    def CloseHandle(self, handle):
        self.calls.append(("kernel_close", handle))
        return 1


@pytest.fixture
def fake_native(monkeypatch):
    calls = []
    kernel = Kernel(calls)
    fake_os = SimpleNamespace(
        **{
            name: getattr(os, name)
            for name in (
                "pipe",
                "open",
                "close",
                "fdopen",
                "devnull",
                "O_WRONLY",
            )
        },
        name="nt",
        O_BINARY=0,
        set_handle_inheritable=lambda handle, value: calls.append(("inherit", handle, value)),
    )
    api = SimpleNamespace(
        CreateProcess=lambda *args: calls.append(("create", args)) or (200, 201, 1000, 1001),
        CloseHandle=lambda handle: calls.append(("api_close", handle)),
        WaitForSingleObject=lambda handle, timeout: calls.append(("wait", handle, timeout)) or 0,
        GetExitCodeProcess=lambda handle: 0,
        TerminateProcess=lambda handle, code: calls.append(("terminate", handle, code)),
    )
    fake_subprocess = SimpleNamespace(
        **{
            name: getattr(native.subprocess, name)
            for name in (
                "list2cmdline",
                "TimeoutExpired",
            )
        },
        STARTUPINFO=lambda: SimpleNamespace(),
        STARTF_USESTDHANDLES=0x100,
    )
    monkeypatch.setattr(native, "os", fake_os)
    monkeypatch.setattr(native, "subprocess", fake_subprocess)
    monkeypatch.setattr(native, "_kernel", lambda: kernel)
    monkeypatch.setitem(sys.modules, "_winapi", api)
    monkeypatch.setitem(sys.modules, "msvcrt", SimpleNamespace(get_osfhandle=lambda fd: fd))
    return calls, kernel, api


def launch():
    return native.WindowsJobProcess(
        Path("C:/Tobkiri/qemu.exe"),
        ["C:/Tobkiri/qemu.exe", "-accel", "whpx"],
        cwd=Path("C:/Tobkiri/private"),
        env={"PATH": "C:/Windows/System32"},
        memory_limit_bytes=2**31,
    )


def test_suspended_child_assigned_before_any_guest_instruction(fake_native):
    calls, _, _ = fake_native
    child = launch()
    order = [entry[0] for entry in calls]
    assert (
        order.index("job") < order.index("create") < order.index("assign") < order.index("resume")
    )
    create = next(entry[1] for entry in calls if entry[0] == "create")
    assert create[5] & 0x4  # CREATE_SUSPENDED
    assert create[5] & 0x80000  # EXTENDED_STARTUPINFO_PRESENT
    startup = create[8]
    assert startup.lpAttributeList["handle_list"] == [
        startup.hStdInput,
        startup.hStdOutput,
        startup.hStdError,
    ]
    limits = next(entry for entry in calls if entry[0] == "limits")
    assert limits[3] & 0x2000 and limits[4] == 1
    child.close()
    assert ("kernel_close", 100) in calls
    assert ("api_close", 200) in calls


@pytest.mark.parametrize("failure", ["limits", "assign", "resume"])
def test_failure_reaps_or_never_starts_and_never_leaks_job(fake_native, failure):
    calls, kernel, _ = fake_native
    kernel.fail = failure
    with pytest.raises(OSError):
        launch()
    order = [entry[0] for entry in calls]
    assert ("kernel_close", 100) in calls
    if failure == "limits":
        assert "create" not in order
    else:
        assert order.index("terminate") < order.index("kernel_close")
        assert ("api_close", 200) in calls
        assert ("api_close", 201) in calls
    if failure == "assign":
        assert "resume" not in order


def test_process_creation_failure_resets_inherited_handles(fake_native):
    calls, _, api = fake_native

    def failed(*args):
        raise OSError("creation failed")

    api.CreateProcess = failed
    with pytest.raises(OSError, match="creation failed"):
        launch()
    transitions = [(entry[1], entry[2]) for entry in calls if entry[0] == "inherit"]
    assert len(transitions) == 6
    assert [handle for handle, state in transitions if state] == [
        handle for handle, state in transitions if not state
    ]
    assert ("kernel_close", 100) in calls


@pytest.mark.skipif(os.name != "nt", reason="Requires native Windows Job/pipe APIs")
def test_native_windows_job_echoes_only_its_private_pipe(tmp_path):
    """A real Windows process smoke check, without starting a guest VM."""
    child = native.WindowsJobProcess(
        Path(sys.executable),
        [
            sys.executable,
            "-I",
            "-S",
            "-c",
            "import sys; sys.stdout.buffer.write(sys.stdin.buffer.readline())",
        ],
        cwd=tmp_path,
        env={"SystemRoot": os.environ["SystemRoot"]},
        memory_limit_bytes=512 * 1024 * 1024,
    )
    try:
        child.stdin.write(b"private-pipe-test\n")
        assert child.stdout.readline() == b"private-pipe-test\n"
        assert child.wait(10) == 0
    finally:
        child.close()


@pytest.mark.skipif(os.name != "nt", reason="Requires native Windows ACL APIs")
def test_native_windows_private_root_acl_round_trip(tmp_path):
    import hashlib

    from tobkiri_host.windows_whpx_security import file_digest, private_directory

    root = tmp_path / "private-domain"
    storage = private_directory(root)
    storage.write_bytes_atomic("state", b"private-test")
    private_directory(root, create=False)
    assert file_digest(root / "state") == "sha256:" + hashlib.sha256(b"private-test").hexdigest()
