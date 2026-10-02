"""Truthfulness tests for production Wasm resource-controller admission."""

from __future__ import annotations

import os
from pathlib import Path
import sys
import threading

import pytest

from tobkiri_host import resource_controller
from tobkiri_host.errors import ProviderExecutionError
from tobkiri_host.resource_controller import (
    LinuxCgroupV2Controller,
    ResourceControllerStatus,
    detect_production_resource_controller,
)
from tobkiri_host.wasm_backend import WasmComponentBackend
from tobkiri_host.wasm_worker import ComponentWorker
from tobkiri_protocol.canonical import canonical_digest


def _patch_unified_cgroup_mount(
    monkeypatch: pytest.MonkeyPatch, mount: Path, relative: str
) -> None:
    """Point cgroup v2 detection at a fake delegated hierarchy."""

    monkeypatch.setattr(resource_controller.sys, "platform", "linux")
    monkeypatch.setattr(
        resource_controller, "_current_unified_cgroup", lambda: Path(relative)
    )
    original_resolve = Path.resolve

    def resolve(path: Path, strict: bool = False) -> Path:
        text = str(path)
        if text == "/sys/fs/cgroup":
            return mount.resolve()
        if text.startswith("/sys/fs/cgroup/"):
            return (mount / text[len("/sys/fs/cgroup/"):]).resolve(strict=strict)
        return original_resolve(path, strict=strict)

    monkeypatch.setattr(Path, "resolve", resolve)


def _patch_fake_cgroupfs_rmdir(
    monkeypatch: pytest.MonkeyPatch, captured: dict | None = None
) -> None:
    """Simulate kernel cgroup files, which never block a cgroupfs rmdir.

    On real cgroupfs the knob files are kernel virtual files, not directory
    entries, so ``rmdir`` removes an empty worker cgroup outright. The
    regular-file stand-ins on a test filesystem must be unlinked first; their
    contents are captured so tests can still assert what was written.
    """

    original_rmdir = Path.rmdir

    def rmdir(path: Path) -> None:
        if path.name.startswith(resource_controller._WORKER_CGROUP_PREFIX):
            for entry in path.iterdir():
                if entry.is_file():
                    if captured is not None:
                        captured[entry.name] = entry.read_text(encoding="ascii")
                    entry.unlink()
        original_rmdir(path)

    monkeypatch.setattr(Path, "rmdir", rmdir)


def _touch_kernel_procs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Give each prepared fake cgroup the kernel-provided cgroup.procs file."""

    original_prepare = LinuxCgroupV2Controller.prepare

    def prepare(self, memory_limit_bytes: int) -> object:
        lease = original_prepare(self, memory_limit_bytes)
        (lease._path / "cgroup.procs").touch()
        return lease

    monkeypatch.setattr(LinuxCgroupV2Controller, "prepare", prepare)


def _empty_worker_group(lease: object) -> None:
    """Unlink regular-file knob stand-ins so a fake cgroup rmdir succeeds."""

    for entry in lease._path.iterdir():
        entry.unlink()


class _HardController:
    status = ResourceControllerStatus(
        controller_id="test-hard-controller",
        production_eligible=True,
        hard_physical_memory_limit=True,
        detail="test-only hard controller",
    )

    def prepare(self, memory_limit_bytes: int) -> object:
        raise AssertionError("status construction must not launch a worker")


def test_macos_process_monitoring_is_never_a_production_hard_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(resource_controller.sys, "platform", "darwin")
    controller, status = detect_production_resource_controller()
    assert controller is None
    assert not status.production_eligible
    assert not status.hard_physical_memory_limit
    assert "VZ/PackVM" in status.detail
    assert "diagnostic only" in status.detail


def test_linux_without_delegated_cgroup_v2_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(resource_controller.sys, "platform", "linux")
    monkeypatch.setattr(
        resource_controller,
        "_current_unified_cgroup",
        lambda: Path("missing-delegation"),
    )
    original_resolve = Path.resolve

    def resolve(path: Path, strict: bool = False) -> Path:
        if path == Path("/sys/fs/cgroup"):
            return tmp_path.resolve()
        return original_resolve(path, strict=strict)

    monkeypatch.setattr(Path, "resolve", resolve)
    controller, status = detect_production_resource_controller()
    assert controller is None
    assert status.controller_id == "linux-cgroup-v2-unavailable"
    assert not status.production_eligible
    assert not status.hard_physical_memory_limit
    assert "unavailable" in status.detail


def test_backend_only_claims_resource_gate_for_hard_controller() -> None:
    command = (sys.executable, "-I", "-B", "-c", "pass")
    parameters = {
        "worker_command_digest": canonical_digest(list(command)),
        "worker_runtime_digest": canonical_digest({"runtime": "test"}),
    }
    development = WasmComponentBackend(command, **parameters)
    assert not development.status.ready_for_production
    assert development.status.conformance_only
    assert "resource_controller" not in development.status.satisfied_gates
    assert development.resource_controller_status.controller_id == (
        "development-rss-sampler"
    )

    production = WasmComponentBackend(
        command,
        resource_controller=_HardController(),
        **parameters,
    )
    assert production.status.ready_for_production
    assert "resource_controller" in production.status.satisfied_gates
    assert production.status.backend_digest != development.status.backend_digest


def test_backend_rejects_hard_status_without_matching_controller() -> None:
    command = (sys.executable, "-I", "-B", "-c", "pass")
    parameters = {
        "worker_command_digest": canonical_digest(list(command)),
        "worker_runtime_digest": canonical_digest({"runtime": "test"}),
    }
    hard = _HardController.status
    with pytest.raises(ValueError, match="controller is missing"):
        WasmComponentBackend(
            command,
            resource_controller_status=hard,
            **parameters,
        )
    with pytest.raises(ValueError, match="status mismatch"):
        WasmComponentBackend(
            command,
            resource_controller=_HardController(),
            resource_controller_status=ResourceControllerStatus(
                controller_id="forged",
                production_eligible=True,
                hard_physical_memory_limit=True,
                detail="forged",
            ),
            **parameters,
        )


def test_linux_cgroup_controller_requires_a_real_directory(tmp_path: Path) -> None:
    with pytest.raises(OSError, match="not a directory"):
        LinuxCgroupV2Controller(tmp_path / "missing")
    marker = tmp_path / "file-root"
    marker.write_text("not a dir")
    with pytest.raises(OSError):
        LinuxCgroupV2Controller(marker)


def test_linux_cgroup_prepare_installs_hard_limits(tmp_path: Path) -> None:
    controller = LinuxCgroupV2Controller(tmp_path)
    assert controller.status.controller_id == "linux-cgroup-v2"
    assert controller.status.production_eligible
    assert controller.status.hard_physical_memory_limit
    lease = controller.prepare(64 * 1024 * 1024)
    group = lease._path
    assert group.parent == tmp_path
    assert group.name.startswith("tobkiri-wasm-")
    assert (group / "memory.max").read_text(encoding="ascii") == str(
        64 * 1024 * 1024
    )
    assert (group / "pids.max").read_text(encoding="ascii") == "64"
    _empty_worker_group(lease)
    lease.close()
    assert not group.exists()
    lease.close()  # idempotent


@pytest.mark.parametrize("limit", [0, -1, 1.5, True, "67108864"])
def test_linux_cgroup_prepare_rejects_invalid_limits(
    tmp_path: Path, limit: object
) -> None:
    controller = LinuxCgroupV2Controller(tmp_path)
    with pytest.raises(ValueError, match="memory limit is invalid"):
        controller.prepare(limit)
    assert not list(tmp_path.glob("tobkiri-wasm-*"))


def test_linux_cgroup_prepare_sets_swap_and_oom_kill_scope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Optional kernel knobs are set when the delegation provides them.

    ``memory.swap.max=0`` keeps the hard memory cap honest on swap-enabled
    hosts; ``memory.oom.group=1`` makes a memory.max OOM kill the whole
    request-scoped worker group instead of leaving siblings behind.
    """

    writes: dict[str, str] = {}
    original_write_text = Path.write_text
    original_exists = Path.exists

    def write_text(path: Path, data: str, *args: object, **kwargs: object) -> int:
        writes[path.name] = data
        return original_write_text(path, data, *args, **kwargs)

    def exists(path: Path, *args: object, **kwargs: object) -> bool:
        if path.name in {"memory.swap.max", "memory.oom.group"}:
            return True
        return original_exists(path, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", write_text)
    monkeypatch.setattr(Path, "exists", exists)
    controller = LinuxCgroupV2Controller(tmp_path)
    lease = controller.prepare(64 * 1024 * 1024)
    assert writes["memory.max"] == str(64 * 1024 * 1024)
    assert writes["pids.max"] == "64"
    assert writes["memory.swap.max"] == "0"
    assert writes["memory.oom.group"] == "1"
    _empty_worker_group(lease)
    lease.close()


def test_linux_cgroup_prepare_failure_removes_the_group(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original_write_text = Path.write_text

    def write_text(path: Path, data: str, *args: object, **kwargs: object) -> int:
        if path.name == "memory.max":
            raise OSError("read-only cgroupfs")
        return original_write_text(path, data, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", write_text)
    controller = LinuxCgroupV2Controller(tmp_path)
    with pytest.raises(OSError, match="read-only cgroupfs"):
        controller.prepare(64 * 1024 * 1024)
    assert not list(tmp_path.glob("tobkiri-wasm-*"))


def test_linux_cgroup_prepare_prunes_only_empty_stale_groups(
    tmp_path: Path,
) -> None:
    stale_empty = tmp_path / "tobkiri-wasm-424242-deadbeef"
    stale_empty.mkdir()
    stale_populated = tmp_path / "tobkiri-wasm-31337-cafef00d"
    stale_populated.mkdir()
    (stale_populated / "cgroup.procs").write_text("4242")
    own_in_flight = tmp_path / f"tobkiri-wasm-{os.getpid()}-00"
    own_in_flight.mkdir()
    unrelated = tmp_path / "unrelated-cgroup"
    unrelated.mkdir()

    controller = LinuxCgroupV2Controller(tmp_path)
    lease = controller.prepare(64 * 1024 * 1024)
    try:
        assert not stale_empty.exists()
        assert stale_populated.is_dir()  # populated: rmdir refuses, never harmed
        assert own_in_flight.is_dir()  # same-pid sweep is never raced
        assert unrelated.is_dir()
    finally:
        _empty_worker_group(lease)
        lease.close()


def test_linux_cgroup_lease_child_setup_writes_cgroup_procs(
    tmp_path: Path,
) -> None:
    controller = LinuxCgroupV2Controller(tmp_path)
    lease = controller.prepare(64 * 1024 * 1024)
    try:
        procs = lease._path / "cgroup.procs"
        procs.touch()  # kernel-provided file on real cgroupfs
        lease.child_setup()
        assert procs.read_text(encoding="ascii") == str(os.getpid())
    finally:
        _empty_worker_group(lease)
        lease.close()


def test_linux_cgroup_lease_child_setup_rejects_short_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    controller = LinuxCgroupV2Controller(tmp_path)
    lease = controller.prepare(64 * 1024 * 1024)
    try:
        (lease._path / "cgroup.procs").touch()
        monkeypatch.setattr(resource_controller.os, "write", lambda fd, data: 0)
        with pytest.raises(OSError, match="short write"):
            lease.child_setup()
    finally:
        _empty_worker_group(lease)
        lease.close()


def test_linux_cgroup_lease_close_kills_trapped_members(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """close() SIGKILLs group-escaped members via cgroup.kill before rmdir.

    A descendant that called ``setsid`` escapes the supervisor's ``killpg``
    but stays inside the cgroup; only the cgroup-wide kill makes teardown
    independent of the worker's process-group cooperation.
    """

    captured: dict[str, str] = {}
    _patch_fake_cgroupfs_rmdir(monkeypatch, captured)
    controller = LinuxCgroupV2Controller(tmp_path)
    lease = controller.prepare(64 * 1024 * 1024)
    (lease._path / "cgroup.kill").touch()  # kernel >= 5.14
    lease.close()
    assert captured["cgroup.kill"] == "1"
    assert not lease._path.exists()
    lease.close()  # idempotent after success


def test_linux_cgroup_lease_close_fails_closed_on_populated_group(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(resource_controller, "_REMOVE_DEADLINE_SECONDS", 0.05)
    controller = LinuxCgroupV2Controller(tmp_path)
    lease = controller.prepare(64 * 1024 * 1024)
    (lease._path / "cgroup.kill").touch()
    # Leftover regular files stand in for live member processes: rmdir on a
    # populated cgroup raises ENOTEMPTY/EBUSY and the lease must not close.
    with pytest.raises(OSError):
        lease.close()
    assert (lease._path / "cgroup.kill").read_text(encoding="ascii") == "1"
    with pytest.raises(OSError):
        lease.close()
    # The contract stays retryable: once members are gone the retry succeeds.
    _empty_worker_group(lease)
    lease.close()
    assert not lease._path.exists()


def test_linux_cgroup_lease_close_retries_past_async_kill_window(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A transiently-populated group is removed within the bounded wait."""

    original_rmdir = Path.rmdir
    attempts = 0

    def rmdir(path: Path) -> None:
        nonlocal attempts
        if path.name.startswith(resource_controller._WORKER_CGROUP_PREFIX):
            attempts += 1
            if attempts == 1:
                raise OSError("cgroup is still populated")
            for entry in path.iterdir():
                if entry.is_file():
                    entry.unlink()
        original_rmdir(path)

    monkeypatch.setattr(Path, "rmdir", rmdir)
    monkeypatch.setattr(resource_controller, "_REMOVE_INTERVAL_SECONDS", 0.001)
    controller = LinuxCgroupV2Controller(tmp_path)
    lease = controller.prepare(64 * 1024 * 1024)
    lease.close()
    assert attempts >= 2
    assert not lease._path.exists()


def test_component_worker_runs_inside_prepared_cgroup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The real fork path attaches the child to its cgroup before exec.

    A regular file stands in for kernel-provided ``cgroup.procs``: the child
    writes its own pid through the same ``os.open``/``os.write`` path the
    production lease uses, so the pid that later answers the request proves
    the fork-time attachment actually ran inside the prepared group.
    """

    captured: dict[str, str] = {}
    _patch_fake_cgroupfs_rmdir(monkeypatch, captured)
    _touch_kernel_procs(monkeypatch)
    controller = LinuxCgroupV2Controller(tmp_path)
    source = (
        "import os, sys\n"
        "sys.stdin.buffer.read()\n"
        "sys.stdout.write("
        "'{\"status\": \"ok\", \"data\": {\"pid\": %d}}' % os.getpid())\n"
    )
    worker = ComponentWorker(
        (sys.executable, "-I", "-B", "-c", source),
        rss_limit=256 * 1024 * 1024,
        resource_controller=controller,
    )
    result = worker.invoke({}, cancelled=threading.Event(), timeout=15)
    assert captured["cgroup.procs"] == str(result["pid"])
    assert captured["memory.max"] == str(256 * 1024 * 1024)
    assert captured["pids.max"] == "64"
    assert not list(tmp_path.glob("tobkiri-wasm-*"))


def test_component_worker_retains_lease_when_release_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A populated cgroup surfaces as an unconfirmed, retryable release."""

    monkeypatch.setattr(resource_controller, "_REMOVE_DEADLINE_SECONDS", 0.02)
    _touch_kernel_procs(monkeypatch)
    controller = LinuxCgroupV2Controller(tmp_path)
    worker = ComponentWorker(
        (sys.executable, "-I", "-B", "-c", "import sys\nsys.stdin.buffer.read()\n"),
        rss_limit=256 * 1024 * 1024,
        resource_controller=controller,
    )
    # The child exits cleanly, but the leftover knob files stand in for live
    # cgroup members: rmdir fails and the reservation must stay retained.
    with pytest.raises(ProviderExecutionError, match="release is unconfirmed"):
        worker.invoke({}, cancelled=threading.Event(), timeout=15)
    lease = worker._controller_lease
    assert lease is not None
    _empty_worker_group(lease)
    worker.close()
    assert worker._controller_lease is None
    assert not lease._path.exists()


@pytest.mark.parametrize(
    ("content", "expected"),
    [
        (
            "0::/user.slice/u-1000.slice/app.scope\n",
            "user.slice/u-1000.slice/app.scope",
        ),
        ("3:memory:/legacy\n0::/unified/path\n", "unified/path"),
        ("0::/\n", "."),
    ],
)
def test_current_unified_cgroup_parses_v2_entry(
    monkeypatch: pytest.MonkeyPatch, content: str, expected: str
) -> None:
    original_read_text = Path.read_text

    def read_text(path: Path, *args: object, **kwargs: object) -> str:
        if str(path) == "/proc/self/cgroup":
            return content
        return original_read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", read_text)
    assert resource_controller._current_unified_cgroup() == Path(expected)


@pytest.mark.parametrize(
    "content",
    [
        "1:cpu:/legacy-only\n",
        "0::relative-without-slash\n",
        "\n",
    ],
)
def test_current_unified_cgroup_rejects_non_unified(
    monkeypatch: pytest.MonkeyPatch, content: str
) -> None:
    original_read_text = Path.read_text

    def read_text(path: Path, *args: object, **kwargs: object) -> str:
        if str(path) == "/proc/self/cgroup":
            return content
        return original_read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", read_text)
    with pytest.raises(OSError, match="unified cgroup v2"):
        resource_controller._current_unified_cgroup()


def test_linux_delegated_cgroup_v2_detects_hard_controller(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A fully delegated subtree passes detection and the writable probe."""

    delegated = tmp_path / "delegated"
    delegated.mkdir()
    (delegated / "cgroup.controllers").write_text("cpu memory pids\n")
    (delegated / "cgroup.subtree_control").write_text("memory pids\n")
    _patch_unified_cgroup_mount(monkeypatch, tmp_path, "delegated")
    _patch_fake_cgroupfs_rmdir(monkeypatch)

    controller, status = detect_production_resource_controller()
    assert isinstance(controller, LinuxCgroupV2Controller)
    assert status.controller_id == "linux-cgroup-v2"
    assert status.production_eligible
    assert status.hard_physical_memory_limit
    # The disposable probe cgroup was created and fully removed again.
    assert not list(delegated.glob("tobkiri-wasm-*"))


@pytest.mark.parametrize(
    ("controllers", "subtree_control", "expected"),
    [
        ("cpu memory\n", "memory pids\n", "not delegated"),
        ("cpu memory pids\n", "memory\n", "not enabled"),
    ],
)
def test_linux_incomplete_delegation_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    controllers: str,
    subtree_control: str,
    expected: str,
) -> None:
    delegated = tmp_path / "delegated"
    delegated.mkdir()
    (delegated / "cgroup.controllers").write_text(controllers)
    (delegated / "cgroup.subtree_control").write_text(subtree_control)
    _patch_unified_cgroup_mount(monkeypatch, tmp_path, "delegated")

    controller, status = detect_production_resource_controller()
    assert controller is None
    assert status.controller_id == "linux-cgroup-v2-unavailable"
    assert not status.production_eligible
    assert expected in status.detail
    assert not list(delegated.glob("tobkiri-wasm-*"))


def test_linux_cgroup_outside_unified_mount_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_unified_cgroup_mount(monkeypatch, tmp_path, "..")
    controller, status = detect_production_resource_controller()
    assert controller is None
    assert "escapes the unified hierarchy" in status.detail


def test_linux_cgroup_prepare_verifies_swap_limit_retained(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A delegation exposing memory.swap.max must retain the zero cap.

    Like memory.max and pids.max, the write alone does not prove the kernel
    kept the value; a read-back that disagrees fails closed.
    """

    original_exists = Path.exists
    original_read_text = Path.read_text

    def exists(path: Path, *args: object, **kwargs: object) -> bool:
        if path.name == "memory.swap.max":
            return True
        return original_exists(path, *args, **kwargs)

    def read_text(path: Path, *args: object, **kwargs: object) -> str:
        if path.name == "memory.swap.max":
            return "max"
        return original_read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "exists", exists)
    monkeypatch.setattr(Path, "read_text", read_text)
    _patch_fake_cgroupfs_rmdir(monkeypatch)
    controller = LinuxCgroupV2Controller(tmp_path)
    with pytest.raises(OSError, match="memory.swap.max did not retain"):
        controller.prepare(64 * 1024 * 1024)
    assert not list(tmp_path.glob("tobkiri-wasm-*"))


def test_linux_cgroup_prepare_cleanup_preserves_the_original_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed limit write stays the error even when cleanup also fails.

    A concurrent foreign sweep may remove the empty worker group between
    mkdir and the knob writes; that removal must not mask the real reason
    the limits could not be installed.
    """

    original_write_text = Path.write_text
    original_rmdir = Path.rmdir

    def write_text(path: Path, data: str, *args: object, **kwargs: object) -> int:
        if path.name == "memory.max":
            raise OSError("read-only cgroupfs")
        return original_write_text(path, data, *args, **kwargs)

    def rmdir(path: Path) -> None:
        if path.name.startswith(resource_controller._WORKER_CGROUP_PREFIX):
            raise FileNotFoundError("already swept")
        original_rmdir(path)

    monkeypatch.setattr(Path, "write_text", write_text)
    monkeypatch.setattr(Path, "rmdir", rmdir)
    controller = LinuxCgroupV2Controller(tmp_path)
    with pytest.raises(OSError, match="read-only cgroupfs"):
        controller.prepare(64 * 1024 * 1024)


def test_linux_cgroup_lease_close_still_removes_when_kill_write_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A refused cgroup.kill write falls back to the rmdir check.

    The kill is best-effort: rmdir remains the authoritative fail-closed
    check on live members, so a rejected kill write must not leave the
    group behind when it is already empty.
    """

    original_write_text = Path.write_text

    def write_text(path: Path, data: str, *args: object, **kwargs: object) -> int:
        if path.name == "cgroup.kill":
            raise OSError("kill not permitted")
        return original_write_text(path, data, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", write_text)
    _patch_fake_cgroupfs_rmdir(monkeypatch)
    controller = LinuxCgroupV2Controller(tmp_path)
    lease = controller.prepare(64 * 1024 * 1024)
    (lease._path / "cgroup.kill").touch()  # present, but the kernel refuses
    lease.close()
    assert not lease._path.exists()


def test_linux_delegation_probe_failure_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Delegation files that look writable but refuse the probe are rejected.

    ``cgroup.controllers``/``cgroup.subtree_control`` only claim the
    delegation; the disposable prepare probe is the authoritative check
    that this cgroup root is actually writable.
    """

    delegated = tmp_path / "delegated"
    delegated.mkdir()
    (delegated / "cgroup.controllers").write_text("cpu memory pids\n")
    (delegated / "cgroup.subtree_control").write_text("memory pids\n")
    _patch_unified_cgroup_mount(monkeypatch, tmp_path, "delegated")
    _patch_fake_cgroupfs_rmdir(monkeypatch)
    original_write_text = Path.write_text

    def write_text(path: Path, data: str, *args: object, **kwargs: object) -> int:
        if path.name == "memory.max":
            raise OSError("read-only cgroupfs")
        return original_write_text(path, data, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", write_text)
    controller, status = detect_production_resource_controller()
    assert controller is None
    assert status.controller_id == "linux-cgroup-v2-unavailable"
    assert not status.production_eligible
    assert "read-only cgroupfs" in status.detail
    assert not list(delegated.glob("tobkiri-wasm-*"))


def test_linux_cgroup_prepare_bounds_the_stale_sweep(tmp_path: Path) -> None:
    """The stale sweep is bounded; groups past the limit are left alone."""

    stale_total = resource_controller._STALE_SWEEP_LIMIT + 6
    foreign_pid = os.getpid() + 10_000_000  # never skipped as own-pid
    for index in range(stale_total):
        (tmp_path / f"tobkiri-wasm-{foreign_pid}-{index:016x}").mkdir()

    controller = LinuxCgroupV2Controller(tmp_path)
    lease = controller.prepare(64 * 1024 * 1024)
    try:
        # At most _STALE_SWEEP_LIMIT stale groups were pruned; the fresh
        # lease group also matches the glob.
        remaining = list(tmp_path.glob("tobkiri-wasm-*"))
        assert len(remaining) >= stale_total - (
            resource_controller._STALE_SWEEP_LIMIT
        ) + 1
    finally:
        _empty_worker_group(lease)
        lease.close()
