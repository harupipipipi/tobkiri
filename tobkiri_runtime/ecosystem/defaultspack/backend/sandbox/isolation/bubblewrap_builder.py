from __future__ import annotations

from pathlib import Path

from .spec import BubblewrapSandboxSpec


def build_bubblewrap_argv(spec: BubblewrapSandboxSpec) -> list[str]:
    """Build Bubblewrap argv from server-side policy only."""
    root = _existing_dir(spec.immutable_root, "immutable_root")
    workspace = _existing_dir(spec.workspace.source, "workspace")
    argv = [
        "bwrap",
        "--unshare-user",
        "--disable-userns",
        "--unshare-pid",
        "--unshare-ipc",
        "--unshare-uts",
        "--new-session",
        "--die-with-parent",
        "--clearenv",
        "--ro-bind",
        str(root),
        "/",
        "--proc",
        "/proc",
        "--tmpfs",
        "/tmp",
        "--tmpfs",
        "/home",
        "--tmpfs",
        "/run",
        "--dev",
        "/dev",
    ]
    if not spec.network_enabled:
        argv.append("--unshare-net")
    bind_flag = "--ro-bind" if spec.workspace.read_only else "--bind"
    argv.extend([bind_flag, str(workspace), "/workspace", "--chdir", "/workspace"])
    if spec.data is not None:
        data = _existing_dir(spec.data.source, "data")
        data_bind_flag = "--ro-bind" if spec.data.read_only else "--bind"
        argv.extend([data_bind_flag, str(data), "/data"])
    if spec.seccomp_fd is not None:
        argv.extend(["--seccomp", str(int(spec.seccomp_fd))])
    env = {
        "HOME": "/home",
        "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
        "RUMI_SANDBOX_ID": spec.sandbox_id,
    }
    env.update(spec.env or {})
    for key, value in sorted(env.items()):
        argv.extend(["--setenv", str(key), str(value)])
    argv.extend(["--", *spec.argv])
    return argv


def _existing_dir(path: Path, label: str) -> Path:
    candidate = Path(path).resolve()
    if not candidate.is_dir():
        raise ValueError(f"{label} must be an existing directory")
    return candidate
