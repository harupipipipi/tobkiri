"""Hard resource-controller boundaries for request-scoped Wasm workers.

RSS sampling is useful for diagnostics, but it is not an operating-system hard
limit.  Production Wasm registration therefore requires a controller from this
module whose limit is installed before the worker executable starts.
"""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import secrets
import sys
from typing import Protocol


@dataclass(frozen=True)
class ResourceControllerStatus:
    """Truthful diagnostic state for one worker resource boundary."""

    controller_id: str
    production_eligible: bool
    hard_physical_memory_limit: bool
    detail: str


class ResourceControllerLease(Protocol):
    """One controller allocation owned by one worker process."""

    def child_setup(self) -> None:
        """Attach the forked child before it executes the worker runtime."""

    def close(self) -> None:
        """Remove the allocation after the worker and descendants are gone."""


class WorkerResourceController(Protocol):
    """Create hard, per-worker operating-system resource boundaries."""

    status: ResourceControllerStatus

    def prepare(self, memory_limit_bytes: int) -> ResourceControllerLease:
        """Install a hard limit and return its child attachment lease."""


class _LinuxCgroupLease:
    def __init__(self, path: Path) -> None:
        self._path = path
        self._closed = False

    def child_setup(self) -> None:
        """Move this child into its cgroup before exec creates private memory."""

        descriptor = os.open(self._path / "cgroup.procs", os.O_WRONLY)
        try:
            os.write(descriptor, str(os.getpid()).encode("ascii"))
        finally:
            os.close(descriptor)

    def close(self) -> None:
        """Remove an empty cgroup; a populated group fails closed."""

        if self._closed:
            return
        self._path.rmdir()
        self._closed = True


class LinuxCgroupV2Controller:
    """Use a delegated cgroup v2 subtree for hard memory and process limits."""

    def __init__(self, delegated_root: Path) -> None:
        root = delegated_root.resolve(strict=True)
        if not root.is_dir():
            raise OSError("delegated cgroup v2 root is not a directory")
        self._root = root
        self.status = ResourceControllerStatus(
            controller_id="linux-cgroup-v2",
            production_eligible=True,
            hard_physical_memory_limit=True,
            detail=f"hard memory.max and pids.max limits under {root}",
        )

    def prepare(self, memory_limit_bytes: int) -> ResourceControllerLease:
        """Create one empty cgroup and install limits before child launch."""

        if type(memory_limit_bytes) is not int or memory_limit_bytes <= 0:
            raise ValueError("worker cgroup memory limit is invalid")
        path = self._root / f"tobkiri-wasm-{os.getpid()}-{secrets.token_hex(8)}"
        path.mkdir(mode=0o700)
        try:
            (path / "memory.max").write_text(str(memory_limit_bytes), encoding="ascii")
            (path / "pids.max").write_text("64", encoding="ascii")
            swap_limit = path / "memory.swap.max"
            if swap_limit.exists():
                swap_limit.write_text("0", encoding="ascii")
            if (path / "memory.max").read_text(encoding="ascii").strip() != str(
                memory_limit_bytes
            ):
                raise OSError("cgroup memory.max did not retain the hard limit")
            if (path / "pids.max").read_text(encoding="ascii").strip() != "64":
                raise OSError("cgroup pids.max did not retain the hard limit")
        except Exception:
            path.rmdir()
            raise
        return _LinuxCgroupLease(path)


def detect_production_resource_controller() -> tuple[
    WorkerResourceController | None,
    ResourceControllerStatus,
]:
    """Return a proven hard controller, or a stable fail-closed diagnostic.

    Linux is eligible only when the current unified cgroup is a writable,
    delegated subtree with memory and pids controllers enabled. macOS and other
    process-only platforms deliberately remain conformance-only: sampled RSS,
    RLIMIT_RSS, and RLIMIT_AS do not prove a physical-memory ceiling there.
    """

    if not sys.platform.startswith("linux"):
        status = ResourceControllerStatus(
            controller_id="unavailable",
            production_eligible=False,
            hard_physical_memory_limit=False,
            detail=(
                "production Wasm requires a VZ/PackVM hard resource boundary; "
                "direct-process RSS sampling is diagnostic only"
            ),
        )
        return None, status
    try:
        relative = _current_unified_cgroup()
        root = (Path("/sys/fs/cgroup") / relative).resolve(strict=True)
        mount = Path("/sys/fs/cgroup").resolve(strict=True)
        if not root.is_relative_to(mount):
            raise OSError("current cgroup escapes the unified hierarchy")
        controllers = set(
            (root / "cgroup.controllers").read_text(encoding="ascii").split()
        )
        if not {"memory", "pids"} <= controllers:
            raise OSError("memory and pids controllers are not delegated")
        enabled = set(
            (root / "cgroup.subtree_control").read_text(encoding="ascii").split()
        )
        if not {"memory", "pids"} <= enabled:
            raise OSError("memory and pids controllers are not enabled for children")
        controller = LinuxCgroupV2Controller(root)
        # Creating a disposable limited child proves this is a writable
        # delegation, not merely a readable host cgroup mount.
        probe = controller.prepare(16 * 1024 * 1024)
        probe.close()
        return controller, controller.status
    except (OSError, ValueError) as exc:
        status = ResourceControllerStatus(
            controller_id="linux-cgroup-v2-unavailable",
            production_eligible=False,
            hard_physical_memory_limit=False,
            detail=f"production Wasm cgroup v2 boundary unavailable: {exc}",
        )
        return None, status


def _current_unified_cgroup() -> Path:
    """Read this process's cgroup v2 path without accepting hybrid entries."""

    for line in Path("/proc/self/cgroup").read_text(encoding="ascii").splitlines():
        hierarchy, controllers, path = line.split(":", 2)
        if hierarchy == "0" and not controllers and path.startswith("/"):
            return Path(path.removeprefix("/"))
    raise OSError("the process is not in a unified cgroup v2 hierarchy")


__all__ = [
    "LinuxCgroupV2Controller",
    "ResourceControllerLease",
    "ResourceControllerStatus",
    "WorkerResourceController",
    "detect_production_resource_controller",
]
