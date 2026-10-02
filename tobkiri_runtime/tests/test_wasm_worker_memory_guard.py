"""Measured committed-bytes stops for the injected worker memory guard.

``worker_memory_guard.c`` is a userspace committed cap injected into the
worker with ``DYLD_INSERT_LIBRARIES``: every mediated memory commit —
interpreter startup, the Wasmtime compiler and the guest's linear-memory
commits — fails at the call site once the cap is reached. Each test runs a
real workload through the real supervision path and asserts a measured stop
(uncaught failure, nonzero exit, no success response) — never the presence
of a declared constant. The peak-RSS check additionally measures the
physical bound on the child's ``ru_maxrss`` high-water mark.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
from pathlib import Path
from typing import Iterator

import pytest

from tobkiri_host.errors import ProviderExecutionError
from tobkiri_host.wasm_worker import ComponentWorker, MemoryGuardConfig

pytestmark = pytest.mark.skipif(
    sys.platform != "darwin",
    reason="the committed-bytes guard is a macOS dyld-interpose boundary",
)

_RUNTIME_ROOT = Path(__file__).resolve().parents[1]
_GUARD_SOURCE = _RUNTIME_ROOT / "tobkiri_host" / "worker_memory_guard.c"
_GIB = 1024 * 1024 * 1024


@pytest.fixture(scope="module")
def guard_library(tmp_path_factory: pytest.TempPathFactory) -> str:
    """Build the real guard dylib once; tests exercise the shipped source."""
    compiler = shutil.which("cc")
    if compiler is None or not _GUARD_SOURCE.is_file():
        pytest.skip("a C toolchain is required to build the guard dylib")
    out = tmp_path_factory.mktemp("guard") / "worker_memory_guard.dylib"
    subprocess.run(
        [compiler, "-O2", "-dynamiclib", "-o", str(out), str(_GUARD_SOURCE)],
        check=True,
        timeout=60,
    )
    return str(out)


@pytest.fixture
def children(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[subprocess.Popen]]:
    """Record real child handles and reap any survivor after a failed assert."""
    original = subprocess.Popen
    processes: list[subprocess.Popen] = []

    def launch(*args: object, **kwargs: object) -> subprocess.Popen:
        process = original(*args, **kwargs)
        processes.append(process)
        return process

    monkeypatch.setattr("tobkiri_host.wasm_worker.subprocess.Popen", launch)
    yield processes
    for process in processes:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=5)


def _allocating_worker(memory_guard: MemoryGuardConfig) -> ComponentWorker:
    """A real child that commits 64 MiB maps until refused, then waits."""
    source = (
        "import mmap, sys, time\n"
        "sys.stdin.buffer.read()\n"
        "blocks = []\n"
        "for _ in range(16):\n"
        "    m = mmap.mmap(-1, 64 * 1024 * 1024)\n"
        "    m[0:1] = b'x'\n"
        "    blocks.append(m)\n"
        "time.sleep(60)\n"
    )
    return ComponentWorker(
        (sys.executable, "-I", "-B", "-c", source),
        memory_guard=memory_guard,
    )


def test_commit_cap_stops_allocation_at_the_cap(
    guard_library: str, children: list
) -> None:
    """A child whose commits cross the guard cap dies at the commit call."""
    owned = _allocating_worker(
        MemoryGuardConfig(guard_library, 640 * 1024 * 1024)
    )
    with pytest.raises(ProviderExecutionError):
        owned.invoke({}, cancelled=threading.Event(), timeout=15)
    assert len(children) == 1
    assert children[0].returncode is not None and children[0].returncode != 0


def test_measured_peak_rss_never_crosses_commit_cap(guard_library: str) -> None:
    """ru_maxrss of a page-touching child stays under the committed cap.

    The child touches every page it commits, so resident bytes track
    committed bytes; the cap failing at commit time is the only mechanism
    that can hold the high-water mark below the ceiling plus slack.
    """
    cap = 640 * 1024 * 1024
    source = (
        "import mmap, sys\n"
        "blocks = []\n"
        "try:\n"
        "    for _ in range(16):\n"
        "        m = mmap.mmap(-1, 64 * 1024 * 1024)\n"
        "        for i in range(0, len(m), 65536):\n"
        "            m[i:i+1] = b'x'\n"
        "        blocks.append(m)\n"
        "except (OSError, MemoryError):\n"
        "    sys.exit(9)\n"
        "sys.exit(7)\n"
    )
    pid = os.fork()
    if pid == 0:
        os.execve(
            sys.executable,
            [sys.executable, "-I", "-B", "-c", source],
            {
                "DYLD_INSERT_LIBRARIES": guard_library,
                "TOBKIRI_WASM_GUARD_CAP_BYTES": str(cap),
            },
        )
        os._exit(127)
    _, status, usage = os.wait4(pid, 0)
    # 1 GiB of requested commits cannot complete under a 640 MiB cap.
    assert not os.WIFEXITED(status) or os.WEXITSTATUS(status) != 0
    # The kernel high-water mark proves resident bytes never crossed the
    # committed bound plus startup slack.
    assert 0 < usage.ru_maxrss <= cap + 128 * 1024 * 1024


def test_lifetime_cap_stops_worker_before_engine_starts(
    guard_library: str, children: list
) -> None:
    """A cap below the interpreter baseline fails the worker at startup."""
    pytest.importorskip("wasmtime")
    from tests.test_wasm_component import worker_command, worker_request

    owned = ComponentWorker(
        worker_command(),
        memory_guard=MemoryGuardConfig(guard_library, 64 * 1024 * 1024),
    )
    with pytest.raises(ProviderExecutionError):
        owned.invoke(worker_request(), cancelled=threading.Event(), timeout=15)
    assert children[0].returncode is not None and children[0].returncode != 0


def test_headroom_stops_guest_memory_growth_after_compile(
    guard_library: str, children: list
) -> None:
    """After trusted compile, the guest may grow committed only by headroom.

    The 64 MiB fill is the transient-peak workload class: under a 32 MiB
    headroom its linear-memory commit fails at the commit call site, so the
    worker can never produce the success frame it previously returned.

    The lifetime cap is deliberately generous (4 GiB) since the honest
    committed baseline — counted exactly as the hooks charge it — varies
    ~0.5–1.5 GiB across identical launches (interpreter/mimalloc arena
    reservations differ per launch). It must sit above every possible
    startup so that ONLY the armed ``used + headroom`` cap is the
    constraint exercised here: the compile must reliably complete, then
    the guest's 64 MiB growth must be refused by the 32 MiB headroom.
    """
    pytest.importorskip("wasmtime")
    from tests.test_wasm_component import component, worker_command, worker_request

    binary = component(
        "i32.const 65536 i32.const 1 i32.const 67108864 memory.fill i32.const 0",
        memory_pages=1025,
    )
    owned = ComponentWorker(
        worker_command(),
        memory_guard=MemoryGuardConfig(
            guard_library,
            4 * _GIB,
            headroom_bytes=32 * 1024 * 1024,
        ),
    )
    with pytest.raises(ProviderExecutionError):
        owned.invoke(worker_request(binary), cancelled=threading.Event())
    assert children[0].returncode is not None and children[0].returncode != 0


def test_within_cap_and_headroom_completes(
    guard_library: str, children: list
) -> None:
    """The same guest inside a headroom it fits returns normally.

    The cap still binds: after ``arm_headroom`` the effective cap is
    ``used + 128 MiB``, so any guest growth beyond the fill + invoke
    slack — a >128 MiB workload — is refused at the commit call site (the
    companion test proves refusal at 32 MiB headroom). The lifetime cap
    is 4 GiB, safely above the variable honest baseline; the previous
    1536 MiB/512 MiB constants predated exact committed accounting and
    left only ~43 MiB of effective headroom, which failed this test.
    """
    pytest.importorskip("wasmtime")
    from tests.test_wasm_component import component, worker_command, worker_request

    binary = component(
        "i32.const 65536 i32.const 1 i32.const 33554432 memory.fill i32.const 0",
        memory_pages=513,
    )
    owned = ComponentWorker(
        worker_command(),
        memory_guard=MemoryGuardConfig(
            guard_library,
            4 * _GIB,
            headroom_bytes=128 * 1024 * 1024,
        ),
    )
    assert owned.invoke(worker_request(binary), cancelled=threading.Event()) == {
        "ok": True
    }
    assert children[0].returncode == 0


def test_guard_cap_without_library_fails_closed(children: list) -> None:
    """A configured cap with no injected library must not run the worker."""
    pytest.importorskip("wasmtime")
    from tests.test_wasm_component import worker_command, worker_request

    result = subprocess.run(
        worker_command(),
        input=json.dumps(worker_request()).encode(),
        capture_output=True,
        env={"TOBKIRI_WASM_GUARD_CAP_BYTES": str(_GIB)},
        close_fds=True,
        timeout=15,
        check=False,
    )
    assert result.returncode != 0
    assert b'"status": "ok"' not in result.stdout
    assert b'"status":"ok"' not in result.stdout


def test_malformed_guard_cap_fails_closed(
    guard_library: str, children: list
) -> None:
    """A non-decimal cap env is rejected, not silently ignored."""
    pytest.importorskip("wasmtime")
    from tests.test_wasm_component import worker_command, worker_request

    result = subprocess.run(
        worker_command(),
        input=json.dumps(worker_request()).encode(),
        capture_output=True,
        env={
            "DYLD_INSERT_LIBRARIES": guard_library,
            "TOBKIRI_WASM_GUARD_CAP_BYTES": "not-a-number",
        },
        close_fds=True,
        timeout=15,
        check=False,
    )
    assert result.returncode != 0
    assert b'"status": "ok"' not in result.stdout
    assert b'"status":"ok"' not in result.stdout


def test_memory_guard_config_rejects_untrusted_values(
    guard_library: str, tmp_path: Path
) -> None:
    """The config is trusted launch data: malformed values fail at bind."""
    for bad in ("relative/path.dylib", "", "has:colon.dylib"):
        with pytest.raises(ValueError):
            MemoryGuardConfig(bad, 1024)
    with pytest.raises(ValueError):
        MemoryGuardConfig(str(tmp_path / "missing.dylib"), 1024)
    for bad_cap in (0, -1, True, 17 * _GIB, "1024"):
        with pytest.raises(ValueError):
            MemoryGuardConfig(guard_library, bad_cap)  # type: ignore[arg-type]
    for bad_headroom in (0, -1, True, 17 * _GIB, "1024"):
        with pytest.raises(ValueError):
            MemoryGuardConfig(
                guard_library, 1024, headroom_bytes=bad_headroom  # type: ignore[arg-type]
            )
