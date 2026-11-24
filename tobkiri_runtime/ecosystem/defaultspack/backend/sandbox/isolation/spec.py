from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class WorkspaceMount:
    source: Path
    target: str = "/workspace"
    read_only: bool = False


@dataclass(frozen=True)
class BubblewrapSandboxSpec:
    sandbox_id: str
    profile_id: str
    immutable_root: Path
    workspace: WorkspaceMount
    argv: tuple[str, ...]
    data: WorkspaceMount | None = None
    env: dict[str, str] = field(default_factory=dict)
    network_enabled: bool = False
    uid: int | None = None
    gid: int | None = None
    seccomp_profile: Path | None = None
    seccomp_fd: int | None = None

    def __post_init__(self) -> None:
        if not self.sandbox_id or "/" in self.sandbox_id or "\x00" in self.sandbox_id:
            raise ValueError("sandbox_id must be an opaque id")
        if not self.profile_id:
            raise ValueError("profile_id is required")
        if not self.argv:
            raise ValueError("argv is required")
        if self.workspace.target != "/workspace":
            raise ValueError("workspace target is fixed to /workspace")
        if self.data is not None and self.data.target != "/data":
            raise ValueError("data target is fixed to /data")


@dataclass(frozen=True)
class CgroupLimits:
    memory_max: str = "512M"
    memory_swap_max: str = "0"
    cpu_quota: str = "100%"
    tasks_max: int = 128
    runtime_max_sec: int = 60
