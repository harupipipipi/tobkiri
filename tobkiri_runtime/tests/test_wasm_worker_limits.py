"""Measured OS-limit stops for the supervised Wasm worker path.

Every test runs a real workload against the actual enforcement mechanism and
asserts a measured stop — a signal exit, a bounded syscall failure, or a
supervisor kill — never merely the presence of a declared constant. The
probe children install the identical limit block that ``worker_main``
applies, so the measured values describe the production ceilings.
"""

from __future__ import annotations

import errno
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Iterator

import pytest

from tobkiri_host.errors import ProviderExecutionError
from tobkiri_host.wasm_worker import ComponentWorker

pytestmark = pytest.mark.skipif(os.name == "nt", reason="worker limits are POSIX")

_RUNTIME_ROOT = Path(__file__).resolve().parents[1]
_FSIZE_LIMIT = 2 * 1024 * 1024


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


def limits_probe(body: str) -> list[str]:
    """Apply the real ``worker_main`` limit block, then run the probe body."""
    script = (
        "import resource, sys\n"
        f"sys.path.insert(0, {str(_RUNTIME_ROOT)!r})\n"
        "from tobkiri_host.wasm_component import _install_worker_resource_limits\n"
        "_install_worker_resource_limits(resource, sys.platform)\n"
        + body
    )
    return [sys.executable, "-I", "-B", "-c", script]


def run_probe(body: str, timeout: float = 30) -> subprocess.CompletedProcess:
    return subprocess.run(
        limits_probe(body), capture_output=True, timeout=timeout, check=False
    )


def test_fd_limit_produces_measured_emfile_stop() -> None:
    """RLIMIT_NOFILE=64: descriptor opens fail with EMFILE at a measured count."""
    probe = (
        "import errno, os, sys\n"
        "fds = []\n"
        "try:\n"
        "    while True:\n"
        "        fds.append(os.open('/dev/null', os.O_RDONLY))\n"
        "except OSError as e:\n"
        "    assert e.errno == errno.EMFILE, e\n"
        "    print('EMFILE after', len(fds), 'opens', flush=True)\n"
        "    sys.exit(9)\n"
        "sys.exit(7)\n"
    )
    result = run_probe(probe)
    assert result.returncode == 9, result.stderr
    opened = int(result.stdout.split()[-2])
    # 64 descriptors total minus the three standard pipes the supervisor owns.
    assert 0 < opened <= 61


def test_file_size_limit_stops_writes_at_exactly_two_mib(tmp_path: Path) -> None:
    """RLIMIT_FSIZE=2MiB: with SIGXFSZ ignored the write fails at the byte cap."""
    target = tmp_path / "bounded.bin"
    probe = (
        "import errno, os, signal, sys\n"
        "signal.signal(signal.SIGXFSZ, signal.SIG_IGN)\n"
        f"fd = os.open({str(target)!r}, os.O_WRONLY | os.O_CREAT, 0o600)\n"
        "written = 0\n"
        "try:\n"
        "    while True:\n"
        "        written += os.write(fd, b'x' * 65536)\n"
        "except OSError as e:\n"
        "    assert e.errno == errno.EFBIG, e\n"
        "    print('EFBIG at', written, flush=True)\n"
        "    sys.exit(9)\n"
        "sys.exit(7)\n"
    )
    result = run_probe(probe)
    assert result.returncode == 9, result.stderr
    assert result.stdout.split()[-1] == str(_FSIZE_LIMIT).encode()
    assert target.stat().st_size == _FSIZE_LIMIT


def test_file_size_limit_kills_writer_with_sigxfsz(tmp_path: Path) -> None:
    """The writer cannot cross RLIMIT_FSIZE: signal death or EFBIG exit.

    Linux delivers SIGXFSZ whose default action kills the process. macOS
    surfaces the same cap as an EFBIG write failure that escapes the child as
    an uncaught error. Either way the file never exceeds the 2 MiB ceiling.
    """
    target = tmp_path / "bounded.bin"
    probe = (
        "import os\n"
        f"fd = os.open({str(target)!r}, os.O_WRONLY | os.O_CREAT, 0o600)\n"
        "while True:\n"
        "    os.write(fd, b'x' * 65536)\n"
    )
    result = run_probe(probe)
    assert (
        result.returncode == -signal.SIGXFSZ
        or (result.returncode != 0 and b"File too large" in result.stderr)
    )
    assert target.stat().st_size <= _FSIZE_LIMIT


def test_cpu_limit_kills_compute_loop_with_sigxcpu() -> None:
    """RLIMIT_CPU=10: a pure compute loop dies by SIGXCPU at ~10 CPU-seconds."""
    started = time.monotonic()
    result = run_probe("while True: pass\n", timeout=60)
    elapsed = time.monotonic() - started
    assert result.returncode == -signal.SIGXCPU
    assert 5 <= elapsed <= 45


@pytest.mark.skipif(
    sys.platform != "darwin",
    reason="the worker NPROC bound is macOS-only; Linux uses cgroup pids.max",
)
def test_process_creation_limit_blocks_descendants() -> None:
    """RLIMIT_NPROC=0: the bounded worker cannot fork a descendant at all."""
    probe = (
        "import errno, os, sys\n"
        "try:\n"
        "    pid = os.fork()\n"
        "    if pid == 0:\n"
        "        os._exit(0)\n"
        "    os.waitpid(pid, 0)\n"
        "    sys.exit(7)\n"
        "except OSError as e:\n"
        "    print('fork blocked errno', e.errno, flush=True)\n"
        "    sys.exit(9 if e.errno == errno.EAGAIN else 8)\n"
    )
    result = run_probe(probe)
    assert result.returncode == 9, result.stdout + result.stderr
    assert result.stdout.split()[-1] == str(errno.EAGAIN).encode()


def test_rss_limit_stops_allocator_while_alive(children: list) -> None:
    """A child growing past the RSS ceiling is killed, not merely rejected."""
    source = (
        "import sys, time\n"
        "sys.stdin.buffer.read()\n"
        "blocks = []\n"
        "for _ in range(6):\n"
        "    blocks.append(bytearray(32 * 1024 * 1024))\n"
        "    time.sleep(0.05)\n"
        "time.sleep(60)\n"
    )
    owned = ComponentWorker(
        (sys.executable, "-I", "-B", "-c", source),
        rss_limit=48 * 1024 * 1024,
    )
    started = time.monotonic()
    with pytest.raises(ProviderExecutionError, match="memory limit"):
        owned.invoke({}, cancelled=threading.Event(), timeout=15)
    elapsed = time.monotonic() - started
    assert len(children) == 1
    assert children[0].returncode is not None and children[0].returncode < 0
    # The child sleeps 60 s untouched; a prompt return proves the live stop.
    assert elapsed < 15
    assert owned._process is None


def test_output_cap_stops_stderr_flood_while_alive(children: list) -> None:
    """The 2 MiB stderr cap stops a flooding child during the exchange."""
    owned = ComponentWorker(
        (
            sys.executable, "-I", "-B", "-c",
            "import os, sys\n"
            "sys.stdin.buffer.read()\n"
            "while True: os.write(2, b'x' * 65536)\n",
        )
    )
    started = time.monotonic()
    with pytest.raises(ProviderExecutionError, match="output exceeds"):
        owned.invoke({}, cancelled=threading.Event(), timeout=15)
    assert time.monotonic() - started < 15
    assert children[0].returncode is not None and children[0].returncode < 0


def test_wall_deadline_kills_real_component_worker(children: list) -> None:
    """A real engine worker exceeding the wall deadline is SIGKILLed.

    The guest's fuel alone would end it ~1 s after it starts running, so the
    0.75 s deadline must be the mechanism that stops it: the child dies by
    signal, never by a clean or failed exit of its own.
    """
    pytest.importorskip("wasmtime")
    from tests.test_wasm_component import component, worker_command, worker_request

    binary = component("(loop $forever br $forever) unreachable")
    owned = ComponentWorker(worker_command())
    started = time.monotonic()
    with pytest.raises(ProviderExecutionError, match="deadline"):
        owned.invoke(
            worker_request(binary), cancelled=threading.Event(), timeout=0.75
        )
    assert time.monotonic() - started < 10
    assert children[0].returncode == -signal.SIGKILL


@pytest.mark.skipif(
    sys.platform != "darwin" and not sys.platform.startswith("linux"),
    reason="resident-memory supervision is implemented for macOS and Linux",
)
def test_transient_memory_peak_is_rejected_live_or_post_hoc(children: list) -> None:
    """A touch-and-return guest peak is rejected even if it outlives a sample.

    The workload's resident peak (~125 MB here) exceeds the 96 MiB ceiling but
    the guest returns immediately after touching it, so either the 1 ms
    sampler kills the child mid-run (signal exit) or the worker's own
    high-water check converts the success into exit 3. Both paths surface as
    the same "memory limit" rejection; a clean exit is never accepted.
    """
    pytest.importorskip("wasmtime")
    from tests.test_wasm_component import component, worker_command, worker_request

    binary = component(
        "i32.const 65536 i32.const 1 i32.const 67108864 memory.fill i32.const 0",
        memory_pages=1025,
    )
    owned = ComponentWorker(worker_command(), rss_limit=96 * 1024 * 1024)
    with pytest.raises(ProviderExecutionError, match="memory limit"):
        owned.invoke(worker_request(binary), cancelled=threading.Event())
    assert children[0].returncode is not None and children[0].returncode != 0


@pytest.mark.skipif(
    sys.platform != "darwin" and not sys.platform.startswith("linux"),
    reason="resident-memory supervision is implemented for macOS and Linux",
)
def test_memory_within_limit_is_accepted(children: list) -> None:
    """Same ~125 MB peak under a 256 MiB ceiling completes and is reaped."""
    pytest.importorskip("wasmtime")
    from tests.test_wasm_component import component, worker_command, worker_request

    binary = component(
        "i32.const 65536 i32.const 1 i32.const 67108864 memory.fill i32.const 0",
        memory_pages=1025,
    )
    owned = ComponentWorker(worker_command(), rss_limit=256 * 1024 * 1024)
    assert owned.invoke(worker_request(binary), cancelled=threading.Event()) == {
        "ok": True
    }
    assert children[0].returncode == 0


@pytest.mark.skipif(os.name != "posix", reason="process-group stop is POSIX")
def test_worker_termination_covers_its_process_group(
    tmp_path: Path, children: list
) -> None:
    """close() stops the worker's whole session group, including descendants.

    ``start_new_session=True`` makes the worker a group leader, so a child it
    forked shares its group id; ``killpg`` reaps both where a leader-only
    ``kill()`` would leak the descendant.
    """
    marker = tmp_path / "grandchild.pid"
    source = (
        "import os, sys, time\n"
        "pid = os.fork()\n"
        "if pid == 0:\n"
        f"    open({str(marker)!r}, 'w').write(str(os.getpid()))\n"
        "    time.sleep(60)\n"
        "    os._exit(0)\n"
        "sys.stdin.buffer.read()\n"
        "time.sleep(60)\n"
    )
    owned = ComponentWorker((sys.executable, "-I", "-B", "-c", source))
    cancelled = threading.Event()
    outcome: dict[str, object] = {}

    def run() -> None:
        try:
            owned.invoke({}, cancelled=cancelled, timeout=30)
        except Exception as exc:
            outcome["error"] = exc

    thread = threading.Thread(target=run)
    thread.start()
    deadline = time.monotonic() + 10
    while not marker.exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert marker.exists(), "grandchild never reported its pid"
    grandchild = int(marker.read_text())
    cancelled.set()
    thread.join(10)
    assert not thread.is_alive()
    assert isinstance(outcome.get("error"), ProviderExecutionError)
    assert "cancelled" in str(outcome["error"])
    assert children[0].returncode is not None
    with pytest.raises(ProcessLookupError):
        os.kill(grandchild, 0)
