from __future__ import annotations

import ctypes
import errno
import hashlib
import hmac
import importlib
import json
import os
import platform
import re
import secrets
import shutil
import stat
import subprocess
import tempfile
import time
import urllib.parse
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Iterator, Mapping, Sequence

import yaml  # type: ignore[import-untyped]

from core_runtime.bounded_process_runner import (
    HostBoundedProcessRunner,
    ProcessExecutionPolicy,
)
from core_runtime.env_compat import read_migrated_env
from core_runtime.hmac_key_manager import generate_or_load_signing_key
from ecosystem.defaultspack.backend.sandbox.isolation.packvm_image_cache import (
    PackVMImageAuthority,
    PackVMImageCache,
    PackVMImageCancelled,
    PackVMImageProgress,
    PackVMPinnedImage,
    PackVMVerifiedImage,
)
from ecosystem.defaultspack.backend.sandbox.isolation.packvm_image_handoff import (
    PackVMLoopbackImageHandoff,
)


LimaRunner = Callable[..., Any]
DiskUsage = Callable[[Path], Any]


DEFAULT_LIMA_INSTANCE = "rumi-managed-runtime"
PACKVM_LIMA_INSTANCE = "tobkiri-packvm-v4"
LIMA_STATE_VERSION = 1
LIMA_CONFIG_POLICY_VERSION = 4
LIMA_STATE_ENV = "RUMI_SANDBOX_LIMA_STATE"
MAX_LIMA_STATE_BYTES = 64 * 1024
PACKVM_LIMA_SCRUB_CHUNK_BYTES = 64 * 1024
PACKVM_LIMA_SCRUB_MAX_FILE_BYTES = 128 * 1024 * 1024
PACKVM_LIMA_SCRUB_MAX_TOTAL_BYTES = 256 * 1024 * 1024
PACKVM_LIMA_SCRUB_DEADLINE_SECONDS = 30.0
_PACKVM_LIMA_BULK_PAYLOAD_NAMES = frozenset({"basedisk", "diffdisk", "disk"})
LIMA_GUEST_WORKSPACE_ROOT = "/var/lib/rumi/workspaces"
LIMA_GUEST_PACK_DATA_ROOT = "/var/lib/rumi/pack-data"
PACKVM_BACKEND_ID = "tobkiri.python-pack-v4"
PACKVM_GUEST_RUNNER = "/usr/local/libexec/tobkiri-packvm-supervisor"
PACKVM_PROTOCOL = "io.tobkiri.packvm-supervisor.v1"
PACKVM_ATTESTATION_VERSION = 2
PACKVM_CONFIRMATION_PREFIX = "PROVISION"
PACKVM_STOP_PREFIX = "STOP"
PACKVM_CLEANUP_PREFIX = "DELETE"
MAX_PACKVM_ARTIFACT_REQUEST_BYTES = 700 * 1024 * 1024
PACKVM_PINNED_IMAGE_VIRTUAL_SIZE_BYTES = 2_361_393_152
PACKVM_ARTIFACT_STORAGE_BUDGET_BYTES = 768 * 1024 * 1024
PACKVM_GUEST_FREE_RESERVE_BYTES = 512 * 1024 * 1024
PACKVM_SYSTEM_GROWTH_BUDGET_BYTES = 512 * 1024 * 1024
PACKVM_MIN_DISK_SIZE_BYTES = (
    PACKVM_PINNED_IMAGE_VIRTUAL_SIZE_BYTES
    + PACKVM_ARTIFACT_STORAGE_BUDGET_BYTES
    + PACKVM_GUEST_FREE_RESERVE_BYTES
    + PACKVM_SYSTEM_GROWTH_BUDGET_BYTES
)
PACKVM_DISK_SIZE_BYTES = 4 * 1024 * 1024 * 1024
PACKVM_HOST_STORAGE_RESERVE_BYTES = 512 * 1024 * 1024
PACKVM_LOCK_RETRY_SECONDS = 0.05
PACKVM_LIMA_UNIX_PATH_LIMIT_BYTES = 104
PACKVM_IMAGE_FD_TOKEN = "__TOBKIRI_PACKVM_IMAGE_FD_0__"
_PACKVM_IMAGE_FD_LOCATION = f"file:///dev/fd/{PACKVM_IMAGE_FD_TOKEN}"
# Lima currently derives both host-agent and socket paths below LIMA_HOME.  Keep
# this list explicit so a Lima upgrade cannot silently invalidate preflight.
PACKVM_LIMA_DERIVED_PATH_SUFFIXES = (
    "_config/hostagent.sock",
    f"{PACKVM_LIMA_INSTANCE}/ha.sock",
    f"{PACKVM_LIMA_INSTANCE}/ssh.sock",
    f"{PACKVM_LIMA_INSTANCE}/serial.sock",
)
LIMA_PROCESS_PATH = "/usr/bin:/bin:/usr/sbin:/sbin:/opt/homebrew/bin:/usr/local/bin"
LIMA_PROCESS_ENVIRONMENT_KEYS = frozenset(
    {"PATH", "HOME", "LIMA_HOME", "TMPDIR", "XDG_CACHE_HOME"}
)
_PACKVM_RESOURCE_ROOT = Path(__file__).with_name("resources")
_PACKVM_CONFIG = _PACKVM_RESOURCE_ROOT / "packvm-lima.v1.yaml"
_PACKVM_RUNNER = _PACKVM_RESOURCE_ROOT / "packvm_guest_runner.py"
_PACKVM_IMAGES = {
    "arm64": {
        "lima_arch": "aarch64",
        "url": "https://cloud-images.ubuntu.com/jammy/20260807/jammy-server-cloudimg-arm64.img",
        "digest": "sha256:b17d9ac9b6249ab30f8c95630acdab3b7a51d76050229ab0ce6c013e303f5ccd",
        "size_bytes": 703_594_496,
        "virtual_size_bytes": PACKVM_PINNED_IMAGE_VIRTUAL_SIZE_BYTES,
    },
    "amd64": {
        "lima_arch": "x86_64",
        "url": "https://cloud-images.ubuntu.com/jammy/20260807/jammy-server-cloudimg-amd64.img",
        "digest": "sha256:ff271290a23279ce764561dbe2e9c3ec29da899535b571a987c37b47970c2ad9",
        "size_bytes": 734_327_808,
        "virtual_size_bytes": PACKVM_PINNED_IMAGE_VIRTUAL_SIZE_BYTES,
    },
}


@dataclass(frozen=True)
class PackVMProvisioningPlan:
    """User-visible, immutable facts for one explicit provisioning ceremony."""

    backend_id: str
    instance: str
    limactl: str | None
    launcher_reason: str | None
    architecture: str
    image_source: str
    image_digest: str
    image_size_bytes: int
    image_download_required: bool
    image_download_bytes: int
    image_cache_status: str
    image_cache_reason: str | None
    disk_size_bytes: int
    host_free_space_required_bytes: int
    host_free_space_available_bytes: int
    host_free_space_reason: str | None
    config_digest: str
    guest_runner_digest: str
    host_build_digest: str
    runtime_root_digest: str
    runtime_path_status: str
    runtime_path_reason: str | None
    ceremony_nonce: str
    plan_digest: str
    confirmation: str


@dataclass(frozen=True)
class PackVMProvisioningRequest:
    """Typed user/setup authorization for one exact provisioning plan."""

    plan_digest: str
    ceremony_nonce: str
    confirmation: str
    approve_image_download: bool = False
    session_digest: str | None = None
    operation_id: str | None = None


@dataclass(frozen=True)
class PackVMDoctor:
    """Fail-closed health status for the managed PackVM supervisor."""

    ready: bool
    backend_id: str
    platform: str
    instance: str
    reason: str | None = None
    attestation_digest: str | None = None


@dataclass(frozen=True)
class _PinnedStagedImage:
    """One complete unlinked staging inode pinned by descriptor."""

    verified: PackVMVerifiedImage
    image_descriptor: int


@dataclass
class _PreparedLimaScrub:
    """Pinned old and replacement inodes prepared without mutating Lima state."""

    directory_descriptor: int
    source_descriptor: int
    temporary_descriptor: int
    name: str
    temporary_name: str
    expected: os.stat_result


class PackVMProcessError(RuntimeError):
    """Typed, bounded diagnostic for one failed Lima subprocess stage."""

    def __init__(
        self,
        *,
        stage: str,
        kind: str,
        exit_code: int | None = None,
        stderr: str | None = None,
    ) -> None:
        self.stage = stage
        self.kind = kind
        self.exit_code = exit_code
        self.stderr = _safe_process_diagnostic(stderr)
        detail = "timed out" if kind == "timeout" else "failed"
        if exit_code is not None:
            detail += f" with exit code {exit_code}"
        if self.stderr:
            detail += f": {self.stderr}"
        super().__init__(f"PackVM Lima {stage} {detail}")

    def diagnostic(self) -> dict[str, Any]:
        """Return the stable public operation-ledger diagnostic."""

        return {
            "code": "packvm_lima_process_failed",
            "stage": self.stage,
            "kind": self.kind,
            "exit_code": self.exit_code,
            "stderr": self.stderr,
        }


class PackVMForeignInstanceError(RuntimeError):
    """A destructive command target no longer matches Host-owned evidence."""


class PackVMOrphanReconciliationRequired(PackVMForeignInstanceError):
    """An orphan cannot be mutated without exact authenticated identity proof."""


class PackVMMutationConflict(RuntimeError):
    """Another process owns the fixed PackVM instance mutation boundary."""


class PackVMResponseReconciliationRequired(PackVMForeignInstanceError):
    """A guest response could not be bound to the exact current instance."""


class _FileLockUnavailable(RuntimeError):
    """The exclusive byte range is already owned by another descriptor."""


def _load_file_lock_module(platform_name: str | None = None) -> Any:
    """Load only the lock backend available on the current operating system."""

    selected = os.name if platform_name is None else platform_name
    if selected == "nt":
        return importlib.import_module("msvcrt")
    if selected == "posix":
        return importlib.import_module("fcntl")
    raise RuntimeError(f"PackVM file locking is unsupported on {selected}")


def _prepare_file_lock_byte(descriptor: int) -> None:
    """Ensure Windows has one stable byte at offset zero to lock."""

    metadata = os.fstat(descriptor)
    if metadata.st_size > 1:
        raise ValueError("PackVM mutation lock has an invalid size")
    os.lseek(descriptor, 0, os.SEEK_SET)
    if metadata.st_size == 0:
        written = os.write(descriptor, b"\0")
        if written != 1:
            raise OSError("PackVM mutation lock initialization failed")
        os.fsync(descriptor)
    os.lseek(descriptor, 0, os.SEEK_SET)


def _try_exclusive_file_lock(descriptor: int) -> None:
    """Attempt one non-blocking OS lock acquisition."""

    backend = _load_file_lock_module()
    try:
        if os.name == "nt":
            os.lseek(descriptor, 0, os.SEEK_SET)
            backend.locking(descriptor, backend.LK_NBLCK, 1)
        else:
            backend.flock(descriptor, backend.LOCK_EX | backend.LOCK_NB)
    except (BlockingIOError, OSError) as exc:
        if isinstance(exc, BlockingIOError) or exc.errno in {
            errno.EACCES,
            errno.EAGAIN,
            errno.EDEADLK,
        }:
            raise _FileLockUnavailable from exc
        raise


def _acquire_exclusive_file_lock(descriptor: int, *, timeout_seconds: float) -> None:
    """Acquire exclusively, polling only up to the explicit bounded timeout.

    A timeout of zero performs exactly one non-blocking attempt. The descriptor
    must remain open until ``_release_exclusive_file_lock`` completes; the OS
    releases the lock automatically if the process exits or crashes.
    """

    if timeout_seconds < 0:
        raise ValueError("PackVM mutation lock timeout must be non-negative")
    _prepare_file_lock_byte(descriptor)
    deadline = time.monotonic() + timeout_seconds
    while True:
        try:
            _try_exclusive_file_lock(descriptor)
            return
        except _FileLockUnavailable:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise
            time.sleep(min(PACKVM_LOCK_RETRY_SECONDS, remaining))


def _release_exclusive_file_lock(descriptor: int) -> None:
    """Release the exact POSIX file or Windows byte-range lock."""

    backend = _load_file_lock_module()
    if os.name == "nt":
        os.lseek(descriptor, 0, os.SEEK_SET)
        backend.locking(descriptor, backend.LK_UNLCK, 1)
    else:
        backend.flock(descriptor, backend.LOCK_UN)


@dataclass(frozen=True)
class _LimaCallResult:
    returncode: int
    stdout: bytes | str
    stderr: bytes | str
    timed_out: bool = False


def lima_state_path() -> Path:
    configured = str(os.environ.get(LIMA_STATE_ENV) or "").strip()
    if configured:
        return Path(configured).expanduser()
    user_data = str(
        read_migrated_env("TOBKIRI_USER_DATA", "RUMI_USER_DATA") or ""
    ).strip()
    if user_data:
        root = Path(user_data).expanduser()
    else:
        root = Path(__file__).resolve().parents[5] / "user_data"
    return root / "sandbox" / "lima-runtime.json"


def save_lima_runtime_state(
    limactl: str,
    instance: str = DEFAULT_LIMA_INSTANCE,
    *,
    runner: LimaRunner | None = None,
) -> dict[str, Any]:
    payload = lima_instance_payload(limactl, instance, runner=runner)
    violation = validate_lima_instance_config(payload)
    if violation:
        raise ValueError(violation)
    state = {
        "version": LIMA_STATE_VERSION,
        "policy_version": LIMA_CONFIG_POLICY_VERSION,
        "instance": instance,
        "config_hash": stable_lima_config_hash(instance, payload),
    }
    path = lima_state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".lima-runtime-", suffix=".json", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(state, handle, ensure_ascii=False, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except OSError:
            pass
    return state


def load_lima_runtime_state() -> dict[str, Any]:
    path = lima_state_path()
    try:
        if path.stat().st_size > MAX_LIMA_STATE_BYTES:
            raise ValueError("Lima sandbox state file is too large")
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ValueError("Lima sandbox has not been provisioned by Rumi") from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("Lima sandbox state is unreadable") from exc
    if not isinstance(payload, dict):
        raise ValueError("Lima sandbox state is invalid")
    if payload.get("version") != LIMA_STATE_VERSION:
        raise ValueError("Lima sandbox state version is unsupported")
    if payload.get("policy_version") != LIMA_CONFIG_POLICY_VERSION:
        raise ValueError("Lima sandbox policy changed; provision the runtime again")
    return payload


def resolve_attested_lima_runtime() -> tuple[str, str]:
    limactl = resolve_limactl_path()
    if limactl is None:
        raise ValueError("Lima sandbox runtime is not installed; run `brew install lima`")
    state = load_lima_runtime_state()
    instance = str(state.get("instance") or "").strip()
    expected_hash = str(state.get("config_hash") or "").strip().lower()
    if not instance or not expected_hash:
        raise ValueError("Lima sandbox state is incomplete")
    payload = lima_instance_payload(limactl, instance)
    violation = validate_lima_instance_config(payload)
    if violation:
        raise ValueError(violation)
    current_hash = stable_lima_config_hash(instance, payload)
    if current_hash != expected_hash:
        raise ValueError("Lima sandbox config changed; provision the runtime again")
    if str(payload.get("status") or "").casefold() != "running":
        raise ValueError("Lima sandbox instance is not running")
    return limactl, instance


def resolve_limactl_path() -> str | None:
    """Find limactl for shell and Finder-launched macOS applications."""
    discovered = shutil.which("limactl")
    if discovered:
        return discovered
    for candidate in (
        Path("/opt/homebrew/bin/limactl"),
        Path("/usr/local/bin/limactl"),
    ):
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    return None


def _lima_process_environment() -> dict[str, str]:
    """Build the minimal validated environment permitted for a Lima process."""

    environment = {"PATH": LIMA_PROCESS_PATH}
    for key in ("HOME", "LIMA_HOME"):
        if key in os.environ:
            environment[key] = _validate_lima_directory(os.environ[key], key)
    return environment


def _validate_lima_directory(value: str, variable: str) -> str:
    """Validate one host directory supplied to Lima through its environment.

    Lima may create a missing leaf directory, so the nearest existing parent is
    accepted when it is a user-owned, non-writable-safe directory.  Every
    existing component is inspected with ``lstat`` and paths that resolve
    differently are rejected to keep symlink and traversal escapes fail-closed.
    """

    if not isinstance(value, str) or not value or value != value.strip() or "\x00" in value:
        raise ValueError(f"{variable} must be a non-empty absolute directory path")
    path = Path(value)
    if not path.is_absolute():
        raise ValueError(f"{variable} must be an absolute directory path")
    if any(part in {".", ".."} for part in value.split("/")):
        raise ValueError(f"{variable} path traversal is not allowed")
    try:
        resolved = path.resolve(strict=False)
    except (OSError, RuntimeError) as exc:
        raise ValueError(f"{variable} path is unsafe") from exc
    if resolved != path:
        raise ValueError(f"{variable} path must not contain symlinks or traversal")

    current = Path(path.anchor)
    components = path.parts[1:]
    for component in components:
        current /= component
        try:
            metadata = current.lstat()
        except FileNotFoundError:
            break
        except OSError as exc:
            raise ValueError(f"{variable} path cannot be inspected") from exc
        is_target = current == path
        if current.is_symlink() or not stat.S_ISDIR(metadata.st_mode):
            raise ValueError(f"{variable} path must contain only regular directories")
        _validate_lima_directory_metadata(metadata, variable, is_target=is_target)
    else:
        try:
            metadata = current.lstat()
        except OSError as exc:
            raise ValueError(f"{variable} path cannot be inspected") from exc
        _validate_lima_directory_metadata(metadata, variable, is_target=True)
    return value


def _validate_lima_directory_metadata(
    metadata: os.stat_result,
    variable: str,
    *,
    is_target: bool,
) -> None:
    """Reject ownership and permission states unsafe for a Lima directory."""

    if metadata.st_mode & 0o022:
        is_sticky_parent = not is_target and bool(metadata.st_mode & stat.S_ISVTX)
        if not is_sticky_parent:
            raise ValueError(f"{variable} path has unsafe permissions")
    if hasattr(os, "getuid"):
        user_id = os.getuid()
        if is_target:
            if metadata.st_uid != user_id:
                raise ValueError(f"{variable} path must be owned by the current user")
        elif metadata.st_uid not in {0, user_id}:
            raise ValueError(f"{variable} path has an unsafe owner")


def lima_instance_payload(
    limactl: str,
    instance: str,
    *,
    runner: LimaRunner | None = None,
    environment: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    if runner is None:
        executable = (
            str(Path(limactl).resolve()) if Path(limactl).is_absolute() else shutil.which(limactl)
        )
        if executable is None:
            raise ValueError("limactl is unavailable")
        argv = (executable, "list", instance, "--format", "json")
        cwd = Path.cwd().resolve()
        process_environment = dict(environment or _lima_process_environment())
        result = HostBoundedProcessRunner().run_local(
            argv=argv,
            cwd=cwd,
            stdin=None,
            timeout_seconds=10,
            environment=process_environment,
            policy=ProcessExecutionPolicy(
                allowed_executables=frozenset({argv[0]}),
                allowed_argv=(argv,),
                allowed_cwds=(cwd,),
                allowed_environment=LIMA_PROCESS_ENVIRONMENT_KEYS,
                max_stdin_bytes=1,
                max_stdout_bytes=MAX_LIMA_STATE_BYTES,
                max_stderr_bytes=MAX_LIMA_STATE_BYTES,
                max_timeout_seconds=10,
            ),
        )
        if result.timed_out:
            raise ValueError("limactl list timed out")
        proc = subprocess.CompletedProcess(
            args=list(argv),
            returncode=result.exit_code if result.exit_code is not None else 1,
            stdout=result.stdout,
            stderr=result.stderr or result.transport_error or "",
        )
    else:
        proc = runner((limactl, "list", instance, "--format", "json"), None, 10)
    if proc.returncode != 0:
        raise ValueError(_decode(proc.stderr) or "limactl list failed")
    try:
        payload = json.loads(_decode(proc.stdout))
    except json.JSONDecodeError as exc:
        raise ValueError("limactl returned invalid JSON") from exc
    if isinstance(payload, list):
        item = next(
            (
                candidate
                for candidate in payload
                if isinstance(candidate, dict)
                and str(candidate.get("name") or "").strip() == instance
            ),
            None,
        )
    else:
        item = payload
    if not isinstance(item, dict) or str(item.get("name") or "").strip() != instance:
        raise ValueError("Lima sandbox instance was not found")
    return _with_resolved_mounts(item, instance)


def validate_lima_instance_config(payload: Mapping[str, Any]) -> str | None:
    config = payload.get("config")
    if not isinstance(config, Mapping):
        return "Lima sandbox config is unavailable"
    if str(config.get("vmType") or payload.get("vmType") or "").casefold() != "vz":
        return "Lima sandbox must use the macOS Virtualization.framework driver"
    mounts = config.get("mounts")
    if mounts != []:
        return "Lima sandbox host mounts must be disabled"
    if config.get("networks") != []:
        return "Lima sandbox network attachments must be disabled"
    ssh = config.get("ssh")
    if not isinstance(ssh, Mapping) or ssh.get("forwardAgent") is not False:
        return "Lima sandbox SSH agent forwarding must be disabled"
    if ssh.get("forwardX11") is not False or ssh.get("forwardX11Trusted") is not False:
        return "Lima sandbox X11 forwarding must be disabled"
    containerd = config.get("containerd")
    if (
        not isinstance(containerd, Mapping)
        or containerd.get("system") is not False
        or containerd.get("user") is not False
    ):
        return "Lima sandbox containerd services must be disabled"
    if config.get("propagateProxyEnv") is not False:
        return "Lima sandbox host proxy propagation must be disabled"
    host_resolver = config.get("hostResolver")
    if not isinstance(host_resolver, Mapping) or host_resolver.get("enabled") is not False:
        return "Lima sandbox host resolver bridging must be disabled"
    port_forwards = config.get("portForwards")
    if not _all_guest_ports_ignored(port_forwards):
        return "Lima sandbox guest port forwarding must be disabled"
    return None


def stable_lima_config_hash(instance: str, payload: Mapping[str, Any]) -> str:
    config = payload.get("config")
    relevant = {
        "instance": instance,
        "arch": payload.get("arch"),
        "vmType": payload.get("vmType"),
        "config": config if isinstance(config, Mapping) else {},
    }
    encoded = json.dumps(
        relevant, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str
    )
    return hashlib.sha256(encoded.encode("utf-8", errors="replace")).hexdigest()


def _packvm_config_semantic_digest(config: bytes) -> str:
    """Hash config semantics with only the one-shot endpoint normalized."""

    try:
        loaded = yaml.safe_load(config)
    except yaml.YAMLError as exc:
        raise ValueError("PackVM Lima config is invalid") from exc
    if not isinstance(loaded, dict):
        raise ValueError("PackVM Lima config is invalid")
    images = loaded.get("images")
    if not isinstance(images, list) or len(images) != 1 or not isinstance(images[0], dict):
        raise ValueError("PackVM Lima image config is invalid")
    location = images[0].get("location")
    if not isinstance(location, str):
        raise ValueError("PackVM Lima image locator is not pinned")
    try:
        parsed = urllib.parse.urlsplit(location)
        port = parsed.port
    except ValueError as exc:
        raise ValueError("PackVM Lima image locator is not pinned") from exc
    if (
        parsed.scheme != "http"
        or parsed.hostname != "127.0.0.1"
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or port is None
        or not 0 <= port <= 65535
        or re.fullmatch(r"/packvm-image/[0-9a-f]{64}", parsed.path) is None
    ):
        raise ValueError("PackVM Lima image locator is not pinned")
    try:
        normalized = json.loads(json.dumps(loaded, sort_keys=True))
    except (TypeError, ValueError) as exc:
        raise ValueError("PackVM Lima config is not canonicalizable") from exc
    normalized["images"][0]["location"] = "packvm-pinned-image://one-shot-loopback"
    return _canonical_digest(normalized)


def build_guest_bwrap_argv(
    *,
    workspace: str,
    cwd: str,
    argv: Sequence[str],
    env: Mapping[str, str],
    network_enabled: bool,
    data_dir: str | None = None,
) -> tuple[str, ...]:
    workspace_path = PurePosixPath(workspace)
    workspace_root = PurePosixPath(LIMA_GUEST_WORKSPACE_ROOT)
    cwd_path = PurePosixPath(cwd)
    visible_workspace = PurePosixPath("/workspace")
    if (
        not workspace_path.is_absolute()
        or workspace_path.parent != workspace_root
        or workspace_path.name in {"", ".", ".."}
        or not cwd_path.is_absolute()
        or (cwd_path != visible_workspace and visible_workspace not in cwd_path.parents)
        or ".." in cwd_path.parts
    ):
        raise ValueError("guest sandbox paths must be absolute")
    if data_dir is not None:
        data_path = PurePosixPath(data_dir)
        if (
            not data_path.is_absolute()
            or data_path.parent != PurePosixPath(LIMA_GUEST_PACK_DATA_ROOT)
            or data_path.name in {"", ".", ".."}
        ):
            raise ValueError("guest Pack data path is outside the managed root")
    command = [
        "bwrap",
        "--die-with-parent",
        "--new-session",
        "--unshare-user",
        "--uid",
        "0",
        "--gid",
        "0",
        "--unshare-pid",
        "--unshare-ipc",
        "--unshare-uts",
    ]
    if not network_enabled:
        command.append("--unshare-net")
    command.extend(
        [
            "--ro-bind",
            "/",
            "/",
            "--dev",
            "/dev",
            "--proc",
            "/proc",
            "--tmpfs",
            "/tmp",
            "--tmpfs",
            "/home",
            "--tmpfs",
            "/run",
            "--bind",
            workspace,
            "/workspace",
        ]
    )
    if data_dir is not None:
        command.extend(("--bind", data_dir, "/data"))
    command.extend(("--tmpfs", LIMA_GUEST_WORKSPACE_ROOT))
    command.extend(("--tmpfs", LIMA_GUEST_PACK_DATA_ROOT))
    command.append("--clearenv")
    for key, value in sorted(env.items()):
        command.extend(("--setenv", str(key), str(value)))
    command.extend(("--chdir", cwd, "--"))
    command.extend(str(item) for item in argv)
    return tuple(command)


def _with_resolved_mounts(
    payload: Mapping[str, Any],
    instance: str,
) -> dict[str, Any]:
    """Fill omitted isolation fields from Lima's Host-owned instance YAML."""
    config = payload.get("config")
    missing_fields = {
        field
        for field in ("mounts", "networks")
        if not isinstance(config, Mapping) or field not in config
    }
    if not missing_fields:
        return dict(payload)
    instance_dir = Path(str(payload.get("dir") or ""))
    if not instance_dir.is_absolute() or instance_dir.name != instance or instance_dir.is_symlink():
        raise ValueError("Lima sandbox config attestation source is unavailable")
    config_path = instance_dir / "lima.yaml"
    try:
        metadata = config_path.lstat()
    except OSError as exc:
        raise ValueError("Lima sandbox config attestation source is unavailable") from exc
    if (
        not stat.S_ISREG(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or metadata.st_size > MAX_LIMA_STATE_BYTES
        or (hasattr(os, "getuid") and metadata.st_uid != os.getuid())
    ):
        raise ValueError("Lima sandbox config attestation source is unsafe")
    try:
        raw_config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise ValueError("Lima sandbox config attestation source is invalid") from exc
    if not isinstance(raw_config, Mapping) or any(
        field not in raw_config for field in missing_fields
    ):
        raise ValueError("Lima sandbox config attestation is incomplete")
    resolved = dict(payload)
    resolved_config = dict(config) if isinstance(config, Mapping) else {}
    for field in missing_fields:
        resolved_config[field] = raw_config[field]
    resolved["config"] = resolved_config
    return resolved


def _all_guest_ports_ignored(value: Any) -> bool:
    if not isinstance(value, list) or not value:
        return False
    covers_all_ports = False
    for rule in value:
        if not isinstance(rule, Mapping):
            return False
        if rule.get("ignore") is not True:
            return False
        port_range = rule.get("guestPortRange")
        if not isinstance(port_range, list) or len(port_range) != 2:
            continue
        try:
            first_port = int(port_range[0])
            last_port = int(port_range[1])
        except (TypeError, ValueError):
            continue
        if first_port <= 1 and last_port >= 65535:
            covers_all_ports = True
    return covers_all_ports


def _decode(value: bytes | str | None) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value or "")


def _default_packvm_lima_home(state_dir: Path, instance: str) -> Path:
    """Select a deterministic persistent user application-data Lima root."""

    uid = os.getuid() if hasattr(os, "getuid") else 0
    identity = hashlib.sha256(
        f"{state_dir.resolve()}\0{instance}\0{uid}".encode("utf-8")
    ).hexdigest()[:16]
    if os.name == "posix":
        pwd = importlib.import_module("pwd")
        user_home = Path(str(pwd.getpwuid(uid).pw_dir)).resolve()
    else:
        user_home = Path.home().resolve()
    return user_home / ".tobkiri" / "packvm" / f"runtime-{uid}-{identity}"


def _packvm_runtime_path_diagnostic(lima_home: Path) -> str | None:
    """Return an actionable reason when any derived Unix path exceeds Lima's cap."""

    for suffix in PACKVM_LIMA_DERIVED_PATH_SUFFIXES:
        derived = lima_home / suffix
        length = len(os.fsencode(derived))
        if length >= PACKVM_LIMA_UNIX_PATH_LIMIT_BYTES:
            return (
                "PackVM runtime path requires "
                f"{length} bytes but Lima supports at most "
                f"{PACKVM_LIMA_UNIX_PATH_LIMIT_BYTES}; Tobkiri runtime-root "
                "preflight rejected provisioning"
            )
    return None


def _ensure_owned_directory_chain(directory: Path) -> None:
    """Create missing descendants and reject symlink/foreign-owned components."""

    missing: list[Path] = []
    cursor = directory
    while not cursor.exists() and not cursor.is_symlink():
        missing.append(cursor)
        if cursor == cursor.parent:
            break
        cursor = cursor.parent
    for path in reversed(missing):
        try:
            path.mkdir(mode=0o700)
        except OSError as exc:
            raise ValueError("PackVM managed directory cannot be created safely") from exc
    checked = list(reversed(missing)) if missing else [directory]
    if cursor.is_symlink():
        checked.insert(0, cursor)
    for path in checked:
        metadata = path.lstat()
        if (
            path.is_symlink()
            or not stat.S_ISDIR(metadata.st_mode)
            or (os.name == "posix" and metadata.st_mode & 0o022)
            or (hasattr(os, "getuid") and metadata.st_uid != os.getuid())
        ):
            raise ValueError("PackVM managed directory is unsafe")
        if path == directory and os.name == "posix" and metadata.st_mode & 0o077:
            os.chmod(path, 0o700)


def _fsync_directory_path(directory: Path) -> None:
    """Durably persist one directory entry update."""

    descriptor = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _owned_directory_chain(
    directory: Path,
) -> tuple[tuple[Path, tuple[int, int]], ...]:
    """Capture every non-symlink component naming one owned directory."""

    current = Path(directory.anchor)
    chain: list[tuple[Path, tuple[int, int]]] = []
    paths = [current]
    for part in directory.parts[1:]:
        current = current / part
        paths.append(current)
    for path in paths:
        metadata = path.lstat()
        if path.is_symlink() or not stat.S_ISDIR(metadata.st_mode):
            raise ValueError("PackVM managed directory chain is unsafe")
        chain.append((path, (int(metadata.st_dev), int(metadata.st_ino))))
    return tuple(chain)


def _open_pinned_owned_directory(
    directory: Path,
) -> tuple[int, int, int, tuple[tuple[Path, tuple[int, int]], ...]]:
    """Pin one private directory and its complete pathname chain."""

    _ensure_owned_directory_chain(directory)
    chain = _owned_directory_chain(directory)
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(directory, flags)
    metadata = os.fstat(descriptor)
    current = directory.lstat()
    if (
        directory.is_symlink()
        or not stat.S_ISDIR(metadata.st_mode)
        or current.st_dev != metadata.st_dev
        or current.st_ino != metadata.st_ino
        or (metadata.st_dev, metadata.st_ino) != chain[-1][1]
        or (hasattr(os, "getuid") and metadata.st_uid != os.getuid())
        or (os.name == "posix" and metadata.st_mode & 0o077)
    ):
        os.close(descriptor)
        raise ValueError("PackVM managed directory identity is unsafe")
    return descriptor, int(metadata.st_dev), int(metadata.st_ino), chain


def _require_pinned_directory_identity(
    directory: Path,
    descriptor: int,
    device: int,
    inode: int,
    chain: tuple[tuple[Path, tuple[int, int]], ...],
) -> None:
    """Reject replacement of a pathname while its original directory is pinned."""

    pinned = os.fstat(descriptor)
    try:
        for path, identity in chain:
            component = path.lstat()
            if (
                path.is_symlink()
                or not stat.S_ISDIR(component.st_mode)
                or (component.st_dev, component.st_ino) != identity
            ):
                raise ValueError("PackVM managed directory identity changed")
        current = directory.lstat()
    except FileNotFoundError as exc:
        raise ValueError("PackVM managed directory identity changed") from exc
    if (
        directory.is_symlink()
        or not stat.S_ISDIR(current.st_mode)
        or pinned.st_dev != device
        or pinned.st_ino != inode
        or current.st_dev != device
        or current.st_ino != inode
    ):
        raise ValueError("PackVM managed directory identity changed")


class PackVMLimaProvisioner:
    """Explicit, authenticated lifecycle for Tobkiri's dedicated Lima PackVM."""

    def __init__(
        self,
        *,
        command_path: str | None = None,
        runner: LimaRunner | None = None,
        state_dir: Path | None = None,
        machine: str | None = None,
        instance: str = PACKVM_LIMA_INSTANCE,
        disk_usage: DiskUsage | None = None,
        lima_home: Path | None = None,
        image_cache: PackVMImageCache | None = None,
    ) -> None:
        self._command_path = command_path
        self._runner = runner
        state_root = state_dir or lima_state_path().parent
        self._state_dir = state_root.resolve()
        self._machine = _normalize_packvm_machine(machine or platform.machine())
        self._instance = instance
        self._disk_usage = disk_usage or shutil.disk_usage
        self._pending: dict[str, str] = {}
        requested_lima_home = lima_home or _default_packvm_lima_home(
            self._state_dir, self._instance
        )
        self._lima_home = requested_lima_home.resolve()
        self._requested_lima_home = requested_lima_home
        self._image_cache = image_cache or PackVMImageCache(
            self._state_dir / "packvm-image-cache",
            disk_usage=self._disk_usage,
        )
        self._active_lima_operation_root: Path | None = None

    @property
    def state_path(self) -> Path:
        return self._state_dir / "packvm-lima-attestation.json"

    @property
    def audit_path(self) -> Path:
        return self._state_dir / "packvm-lima-audit.jsonl"

    @property
    def recovery_path(self) -> Path:
        """Host-authenticated evidence for a partially created instance."""

        return self._state_dir / "packvm-lima-recovery.json"

    @property
    def mutation_claim_path(self) -> Path:
        """Return the durable owner claim for the fixed Lima instance."""

        return self._state_dir / "packvm-lima-mutation-claim.json"

    @property
    def mutation_lock_path(self) -> Path:
        """Return the interprocess serialization lock for the fixed instance."""

        return self._state_dir / "packvm-lima-mutation.lock"

    @property
    def lima_home(self) -> Path:
        """Return the canonical dedicated Lima home used by this PackVM."""

        return self._lima_home

    @property
    def image_cache(self) -> PackVMImageCache:
        """Return the dedicated PackVM-owned image cache."""

        return self._image_cache

    @contextmanager
    def operation_gate(
        self,
        operation: str,
        binding: Mapping[str, str | int],
        *,
        recover_claim: bool = False,
        preserve_claim_on_error: bool = False,
        retain_claim_on_success: bool = False,
    ) -> Iterator[None]:
        """Serialize one fixed-instance operation and persist its exact owner."""

        self._ensure_private_managed_directories()
        flags = os.O_CREAT | os.O_RDWR
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(self.mutation_lock_path, flags, 0o600)
        succeeded = False
        locked = False
        claim = {
            "version": 1,
            "operation": operation,
            "instance": self._instance,
            "owner_pid": os.getpid(),
            "binding": dict(binding),
        }
        try:
            metadata = os.fstat(descriptor)
            if (
                not stat.S_ISREG(metadata.st_mode)
                or (os.name == "posix" and metadata.st_mode & 0o077)
                or (hasattr(os, "getuid") and metadata.st_uid != os.getuid())
            ):
                raise ValueError("PackVM mutation lock is unsafe")
            try:
                _acquire_exclusive_file_lock(descriptor, timeout_seconds=0.0)
                locked = True
            except _FileLockUnavailable as exc:
                raise PackVMMutationConflict(
                    "PackVM fixed instance has another operation in progress"
                ) from exc
            existing = self._load_mutation_claim()
            if existing is not None:
                same_owner = _constant_mapping_equal(existing, claim)
                stale_recovery = (
                    recover_claim
                    and _claim_binding_equal(existing, claim)
                    and not _process_is_alive(existing.get("owner_pid"))
                )
                if not same_owner and not stale_recovery:
                    raise PackVMMutationConflict(
                        "PackVM fixed instance has an unresolved operation; "
                        "reconciliation is required"
                    )
                if stale_recovery:
                    _atomic_private_json(self.mutation_claim_path, claim)
            else:
                _atomic_private_json(self.mutation_claim_path, claim)
            yield
            succeeded = True
        finally:
            remove_after = (succeeded and not retain_claim_on_success) or (
                not succeeded and not preserve_claim_on_error
            )
            if locked and remove_after:
                self._remove_owned_mutation_claim(claim)
            try:
                if locked:
                    _release_exclusive_file_lock(descriptor)
            finally:
                os.close(descriptor)

    def _load_mutation_claim(self) -> dict[str, Any] | None:
        try:
            raw = _read_private_file(self.mutation_claim_path, MAX_LIMA_STATE_BYTES)
        except FileNotFoundError:
            return None
        try:
            claim = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise PackVMMutationConflict(
                "PackVM mutation claim is invalid; reconciliation is required"
            ) from exc
        if (
            not isinstance(claim, dict)
            or claim.get("version") != 1
            or claim.get("instance") != self._instance
            or not isinstance(claim.get("owner_pid"), int)
            or not isinstance(claim.get("operation"), str)
            or not isinstance(claim.get("binding"), dict)
        ):
            raise PackVMMutationConflict(
                "PackVM mutation claim is invalid; reconciliation is required"
            )
        return claim

    def _remove_owned_mutation_claim(self, expected: Mapping[str, Any]) -> None:
        current = self._load_mutation_claim()
        if current is not None and _constant_mapping_equal(current, expected):
            self.mutation_claim_path.unlink(missing_ok=True)

    def recovery_identity(self) -> dict[str, int | str]:
        """Return non-path Host identity fields for the dedicated Lima root."""

        self._ensure_private_managed_directories()
        metadata = self._lima_home.lstat()
        return {
            "lima_home_digest": _sha256(str(self._lima_home).encode()),
            "lima_home_device": int(metadata.st_dev),
            "lima_home_inode": int(metadata.st_ino),
            "limactl_digest": _file_digest(Path(self._require_command())),
        }

    def prepare(self) -> PackVMProvisioningPlan:
        """Return download and identity facts without creating or starting a VM."""
        image = _PACKVM_IMAGES[self._machine]
        limactl = self._resolve_command()
        config = self._rendered_config()
        image_cache_status, image_cache_reason = self._packvm_image_cache_status()
        image_download_required = not self._instance_exists(limactl) and (
            image_cache_status != "verified_source"
        )
        image_download_bytes = (
            self._image_cache.remaining_bytes(
                self._image_authority(
                    plan_digest="sha256:" + "0" * 64,
                    session_digest="sha256:" + "0" * 64,
                    operation_id="prepare",
                )
            )
            if image_download_required and image_cache_status != "unsafe"
            else (int(str(image["size_bytes"])) if image_download_required else 0)
        )
        runtime_path_reason = _packvm_runtime_path_diagnostic(self._lima_home)
        required_space = self._required_host_space(image_download_bytes)
        available_space, storage_reason = self._host_free_space(required_space)
        nonce = secrets.token_hex(16)
        facts = {
            "backend_id": PACKVM_BACKEND_ID,
            "instance": self._instance,
            "limactl_digest": _file_digest(Path(limactl)) if limactl else None,
            "architecture": self._machine,
            "image_source": image["url"],
            "image_digest": image["digest"],
            "image_size_bytes": image["size_bytes"],
            "image_download_required": image_download_required,
            "image_download_bytes": image_download_bytes,
            "image_cache_status": image_cache_status,
            "disk_size_bytes": PACKVM_DISK_SIZE_BYTES,
            "host_free_space_required_bytes": required_space,
            "config_digest": self._config_digest(config),
            "guest_runner_digest": _file_digest(_PACKVM_RUNNER),
            "host_build_digest": _file_digest(Path(__file__)),
            "runtime_root_digest": _sha256(str(self._lima_home).encode()),
            "runtime_path_status": "unsafe" if runtime_path_reason else "ready",
            "ceremony_nonce": nonce,
        }
        plan_digest = _canonical_digest(facts)
        confirmation = f"{PACKVM_CONFIRMATION_PREFIX} {self._instance} {plan_digest[7:19]}"
        self._pending.clear()
        self._pending[nonce] = plan_digest
        return PackVMProvisioningPlan(
            backend_id=PACKVM_BACKEND_ID,
            instance=self._instance,
            limactl=limactl,
            launcher_reason=self._launcher_reason(limactl),
            architecture=self._machine,
            image_source=str(image["url"]),
            image_digest=str(image["digest"]),
            image_size_bytes=int(str(image["size_bytes"])),
            image_download_required=bool(facts["image_download_required"]),
            image_download_bytes=image_download_bytes,
            image_cache_status=image_cache_status,
            image_cache_reason=image_cache_reason,
            disk_size_bytes=PACKVM_DISK_SIZE_BYTES,
            host_free_space_required_bytes=required_space,
            host_free_space_available_bytes=available_space,
            host_free_space_reason=storage_reason,
            config_digest=str(facts["config_digest"]),
            guest_runner_digest=str(facts["guest_runner_digest"]),
            host_build_digest=str(facts["host_build_digest"]),
            runtime_root_digest=str(facts["runtime_root_digest"]),
            runtime_path_status=str(facts["runtime_path_status"]),
            runtime_path_reason=runtime_path_reason,
            ceremony_nonce=nonce,
            plan_digest=plan_digest,
            confirmation=confirmation,
        )

    def provision(
        self,
        request: PackVMProvisioningRequest,
        *,
        progress: Callable[[PackVMImageProgress], None] | None = None,
        cancelled: Callable[[], bool] | None = None,
    ) -> PackVMDoctor:
        """Create and attest the guest after consuming an exact ceremony once."""
        expected = self._pending.pop(request.ceremony_nonce, None)
        if expected is None or not hmac.compare_digest(expected, request.plan_digest):
            raise ValueError("PackVM provisioning ceremony is invalid or already consumed")
        plan = self._plan_for_consumed_nonce(request.ceremony_nonce)
        if plan.plan_digest != request.plan_digest:
            raise ValueError("PackVM provisioning plan changed; review it again")
        if not hmac.compare_digest(plan.confirmation, request.confirmation):
            raise ValueError("PackVM provisioning confirmation does not match")
        if plan.limactl is None:
            raise ValueError("limactl is unavailable; install approved Lima first")
        if plan.image_download_required and not request.approve_image_download:
            raise ValueError(
                "PackVM image download requires explicit approval for the displayed source, size, and digest"
            )
        if plan.runtime_path_status != "ready":
            raise ValueError(plan.runtime_path_reason or "PackVM runtime path is unsafe")
        self._require_host_capacity(plan.image_download_bytes)
        authority = self._image_authority(
            plan_digest=plan.plan_digest,
            session_digest=request.session_digest or _sha256(b"direct-local-lifecycle"),
            operation_id=request.operation_id or _sha256(request.ceremony_nonce.encode()),
        )
        binding = {
            "session_digest": request.session_digest or _sha256(b"direct-local-lifecycle"),
            "plan_digest": request.plan_digest,
            "ceremony_nonce_digest": _sha256(request.ceremony_nonce.encode()),
        }
        with self._image_cache.provisioning_image(
            authority, progress=progress, cancelled=cancelled
        ) as pinned_image:
            self._image_cache.garbage_collect(authority)
            with self.operation_gate(
                "provision",
                binding,
                recover_claim=True,
                preserve_claim_on_error=True,
            ):
                return self._provision_locked(
                    request,
                    plan,
                    pinned_image,
                    progress=progress,
                    cancelled=cancelled,
                )

    def _provision_locked(
        self,
        request: PackVMProvisioningRequest,
        plan: PackVMProvisioningPlan,
        pinned_image: PackVMPinnedImage,
        *,
        progress: Callable[[PackVMImageProgress], None] | None,
        cancelled: Callable[[], bool] | None,
    ) -> PackVMDoctor:
        """Provision while holding the fixed-instance interprocess claim."""

        limactl = plan.limactl
        if limactl is None:
            raise ValueError("limactl is unavailable; install approved Lima first")
        if self.state_path.exists():
            raise ValueError("PackVM is already provisioned; use doctor or explicit cleanup")
        if self._instance_exists(limactl):
            raise ValueError(
                "unattested managed Lima instance already exists; explicit cleanup is required"
            )
        current_cache_status, current_cache_reason = self._packvm_image_cache_status()
        expected_cache_status = (
            "verified_source" if plan.image_download_required else plan.image_cache_status
        )
        if current_cache_status != expected_cache_status:
            raise ValueError("PackVM provisioning plan changed; review it again")
        if current_cache_status == "unsafe":
            raise ValueError(current_cache_reason or "PackVM image cache is unsafe")
        self._require_host_capacity(0)
        verified_image = pinned_image.verified

        self._ensure_private_managed_directories()
        recovery = self._recovery_facts(plan, request, verified_image)
        try:
            with self._staged_image(
                pinned_image,
                progress=progress,
                cancelled=cancelled,
            ) as staged_image:
                self._verify_sealed_staged_identity(staged_image)
                self._seal_staged_image(staged_image)
                if cancelled is not None and cancelled():
                    raise PackVMImageCancelled(
                        "packvm_image_cancelled",
                        "PackVM image provisioning was cancelled",
                    )
                with PackVMLoopbackImageHandoff(
                    staged_image.image_descriptor,
                    size_bytes=staged_image.verified.size_bytes,
                    digest=staged_image.verified.digest,
                    cancelled=cancelled,
                ) as handoff:
                    config = self._rendered_config(image_location=handoff.url)
                    self._require_config_image_location(config, handoff.url)
                    executed_config_digest = self._config_digest(config)
                    if not hmac.compare_digest(executed_config_digest, plan.config_digest):
                        raise ValueError("PackVM executed Lima config differs from reviewed plan")
                    recovery["phase"] = "start_pending"
                    recovery["image_handoff"] = "one-shot-loopback-v1"
                    recovery["executed_config_digest"] = executed_config_digest
                    recovery["authentication"] = self._sign_recovery(recovery)
                    _atomic_private_json(self.recovery_path, recovery)
                    if cancelled is not None and cancelled():
                        raise PackVMImageCancelled(
                            "packvm_image_cancelled",
                            "PackVM image provisioning was cancelled",
                        )
                    try:
                        with self._lima_handoff_operation_environment():
                            try:
                                self._checked_call(
                                    (limactl, "start", "--name", self._instance, "-"),
                                    timeout=900,
                                    input_text=config.decode("utf-8"),
                                    max_stdin_bytes=len(config),
                                    stage="start",
                                    sensitive_values=handoff.sensitive_values,
                                )
                            finally:
                                self._scrub_lima_handoff_artifacts(
                                    handoff.url, handoff.sensitive_values
                                )
                    except PackVMProcessError:
                        if cancelled is not None and cancelled():
                            raise PackVMImageCancelled(
                                "packvm_image_cancelled",
                                "PackVM image provisioning was cancelled",
                            )
                        raise
                    if cancelled is not None and cancelled():
                        raise PackVMImageCancelled(
                            "packvm_image_cancelled",
                            "PackVM image provisioning was cancelled",
                        )
                    handoff.require_consumed()
                self._seal_staged_image(staged_image)
                self._verify_sealed_staged_identity(staged_image)
            self._install_guest_runner(limactl)
            machine_id = self._guest_machine_id(limactl)
            runner_digest = self._guest_runner_digest(limactl)
            if runner_digest != plan.guest_runner_digest:
                raise ValueError("guest supervisor binary verification failed")
            self._verify_guest_doctor(limactl)
            payload = lima_instance_payload(
                limactl,
                self._instance,
                runner=self._runner,
                environment=self._lima_process_environment(),
            )
            violation = validate_lima_instance_config(payload)
            if violation:
                raise ValueError(violation)
            state = {
                "version": PACKVM_ATTESTATION_VERSION,
                "backend_id": PACKVM_BACKEND_ID,
                "instance": self._instance,
                "instance_machine_id": machine_id,
                "instance_config_hash": stable_lima_config_hash(self._instance, payload),
                **self._instance_directory_identity(),
                "config_digest": plan.config_digest,
                "image_digest": plan.image_digest,
                "image_source": plan.image_source,
                "image_local_device": verified_image.device,
                "image_local_inode": verified_image.inode,
                "limactl_digest": _file_digest(Path(limactl)),
                "guest_runner_digest": runner_digest,
                "host_build_digest": plan.host_build_digest,
                "ceremony_nonce_digest": _sha256(request.ceremony_nonce.encode()),
                "session_digest": (
                    request.session_digest
                    if request.session_digest is not None
                    else _sha256(b"direct-local-lifecycle")
                ),
                "plan_digest": request.plan_digest,
                "created_unix": int(time.time()),
                **self.recovery_identity(),
            }
            state["attestation_digest"] = _canonical_digest(state)
            state["authentication"] = self._sign_state(state)
            _atomic_private_json(self.state_path, state)
            self.recovery_path.unlink(missing_ok=True)
            self._audit("provisioned", str(state["attestation_digest"]))
            return self.doctor()
        except Exception as error:
            recovery_status = self._reconcile_failed_provision(limactl, recovery)
            if recovery_status != "orphaned":
                self.mutation_claim_path.unlink(missing_ok=True)
            self._audit(
                "provision_failed",
                None,
                details={
                    "recovery_status": recovery_status,
                    "failure_stage": getattr(error, "stage", None),
                },
            )
            raise

    def doctor(self) -> PackVMDoctor:
        """Authenticate Host state, VM identity, config, and guest runner health."""
        platform_id = f"macos-{self._machine}"
        try:
            limactl = self._require_command()
            state = self._load_authenticated_state()
            if state.get("limactl_digest") != _file_digest(Path(limactl)):
                raise ValueError("limactl binary changed after provisioning")
            if state.get("config_digest") != self._config_digest(self._rendered_config()):
                raise ValueError("managed PackVM pinned config changed")
            if state.get("image_digest") != _PACKVM_IMAGES[self._machine]["digest"]:
                raise ValueError("managed PackVM pinned image changed")
            if state.get("image_source") != _PACKVM_IMAGES[self._machine]["url"]:
                raise ValueError("managed PackVM pinned image source changed")
            if state.get("guest_runner_digest") != _file_digest(_PACKVM_RUNNER):
                raise ValueError("packaged PackVM guest supervisor changed")
            if state.get("host_build_digest") != _file_digest(Path(__file__)):
                raise ValueError("PackVM Host build changed after provisioning")
            for field, value in self.recovery_identity().items():
                if state.get(field) != value:
                    raise ValueError(f"managed PackVM {field} changed")
            for field, value in self._instance_directory_identity().items():
                if state.get(field) != value:
                    raise ValueError(f"managed PackVM {field} changed")
            payload = lima_instance_payload(
                limactl,
                self._instance,
                runner=self._runner,
                environment=self._lima_process_environment(),
            )
            violation = validate_lima_instance_config(payload)
            if violation:
                raise ValueError(violation)
            if str(payload.get("status") or "").casefold() != "running":
                raise ValueError("managed PackVM instance is not running")
            if state.get("instance_config_hash") != stable_lima_config_hash(
                self._instance, payload
            ):
                raise ValueError("managed PackVM config changed")
            if state.get("instance_machine_id") != self._guest_machine_id(limactl):
                raise ValueError("managed PackVM instance identity changed")
            if state.get("guest_runner_digest") != self._guest_runner_digest(limactl):
                raise ValueError("managed PackVM guest supervisor changed")
            self._verify_guest_doctor(limactl)
            return PackVMDoctor(
                True,
                PACKVM_BACKEND_ID,
                platform_id,
                self._instance,
                attestation_digest=str(state["attestation_digest"]),
            )
        except (OSError, ValueError, PackVMForeignInstanceError) as exc:
            return PackVMDoctor(
                False, PACKVM_BACKEND_ID, platform_id, self._instance, reason=str(exc)
            )

    def readiness_snapshot(self) -> dict[str, Any]:
        """Return a fresh Host-authenticated PackVM attestation projection."""

        doctor = self.doctor()
        result = {
            "ready": doctor.ready,
            "backend_id": doctor.backend_id,
            "platform": doctor.platform,
            "instance": doctor.instance,
            "reason": doctor.reason,
            "attestation_digest": doctor.attestation_digest,
            "observed_unix": int(time.time()),
        }
        if not doctor.ready:
            return result
        state = self._load_authenticated_state()
        return {
            **result,
            **{key: value for key, value in state.items() if key != "authentication"},
        }

    def recover_provision_operation(
        self,
        expected_proof: Mapping[str, Any],
    ) -> PackVMDoctor:
        """Recover success only for the exact session and plan now attested live."""

        binding = _provision_claim_binding(expected_proof)
        with self.operation_gate("provision", binding, recover_claim=True):
            return self._recover_provision_operation_locked(expected_proof)

    def _recover_provision_operation_locked(
        self,
        expected_proof: Mapping[str, Any],
    ) -> PackVMDoctor:
        """Recover a provision while holding its exact durable owner claim."""

        state = self._load_authenticated_state()
        proof_fields = {
            "backend_id",
            "instance",
            "session_digest",
            "plan_digest",
            "ceremony_nonce_digest",
            "config_digest",
            "image_digest",
            "guest_runner_digest",
            "host_build_digest",
            "limactl_digest",
            "lima_home_digest",
            "lima_home_device",
            "lima_home_inode",
        }
        for field in proof_fields:
            expected = expected_proof.get(field)
            actual = state.get(field)
            if not isinstance(expected, (str, int)) or type(expected) is not type(actual):
                raise ValueError("PackVM provision recovery proof is incomplete")
            if isinstance(expected, str):
                if not hmac.compare_digest(expected, str(actual)):
                    raise ValueError(f"PackVM provision recovery {field} changed")
            elif expected != actual:
                raise ValueError(f"PackVM provision recovery {field} changed")
        self._verify_exact_current_instance(state, require_guest=True)
        doctor = self.doctor()
        if not doctor.ready:
            raise ValueError(doctor.reason or "managed PackVM is unavailable")
        return doctor

    def stop(self, confirmation: str) -> None:
        """Stop only the authenticated instance after exact confirmation."""
        state = self._load_authenticated_state()
        expected = f"{PACKVM_STOP_PREFIX} {self._instance}"
        if not hmac.compare_digest(confirmation, expected):
            raise ValueError(f"PackVM stop requires exact confirmation: {expected}")
        binding = {"attestation_digest": str(state.get("attestation_digest") or "")}
        with self.operation_gate("stop", binding):
            self._verify_attested_host_binding(state)
            limactl = self._verify_exact_current_instance(state, require_guest=True)
            self._checked_call((limactl, "stop", "--force", self._instance), timeout=60)
            self._audit("stopped", None)

    def cleanup(self, confirmation: str) -> None:
        """Delete only the authenticated instance after an exact typed ceremony."""
        state = self._load_authenticated_state()
        self._verify_attested_host_binding(state)
        expected = f"{PACKVM_CLEANUP_PREFIX} {self._instance}"
        if not hmac.compare_digest(confirmation, expected):
            raise ValueError(f"PackVM cleanup requires exact confirmation: {expected}")
        binding = {"attestation_digest": str(state.get("attestation_digest") or "")}
        with self.operation_gate("cleanup", binding):
            self._verify_attested_host_binding(state)
            limactl = self._require_command()
            if self._instance_exists(limactl):
                limactl = self._verify_exact_current_instance(state, require_guest=False)
                self._checked_call(
                    (limactl, "delete", "--force", self._instance),
                    timeout=120,
                    stage="cleanup_delete",
                )
            self._audit("deleted", str(state["attestation_digest"]))
            self.state_path.unlink(missing_ok=True)
            (self._state_dir / "packvm-lima-attestation.key").unlink(missing_ok=True)

    def cleanup_failed_provision(
        self,
        confirmation: str,
        expected_proof: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Delete only the server-attested orphan from one failed provision."""

        expected = f"{PACKVM_CLEANUP_PREFIX} {self._instance}"
        if not hmac.compare_digest(confirmation, expected):
            raise ValueError(f"PackVM cleanup requires exact confirmation: {expected}")
        binding = _provision_claim_binding(expected_proof)
        with self.operation_gate("provision", binding, recover_claim=True):
            return self._cleanup_failed_provision_locked(expected_proof)

    def _cleanup_failed_provision_locked(
        self,
        expected_proof: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Clean one failed provision while holding its durable owner claim."""

        try:
            recovery = self._load_authenticated_recovery()
        except ValueError as error:
            if "evidence is unavailable" not in str(error):
                raise
            self._verify_recovery_environment(expected_proof)
            limactl = self._require_command()
            if self._instance_exists(limactl):
                raise ValueError(
                    "PackVM orphan exists without authenticated recovery evidence"
                ) from error
            self._audit(
                "failed_provision_delete_idempotent",
                None,
                details={"missing": True, "plan_digest": expected_proof["plan_digest"]},
            )
            return {"missing": True}
        proof_fields = {
            "backend_id",
            "instance",
            "session_digest",
            "plan_digest",
            "ceremony_nonce_digest",
            "config_digest",
            "image_digest",
            "guest_runner_digest",
            "host_build_digest",
            "limactl_digest",
            "lima_home_digest",
            "lima_home_device",
            "lima_home_inode",
        }
        for field in proof_fields:
            actual = recovery.get(field)
            supplied = expected_proof.get(field)
            if isinstance(actual, str) and isinstance(supplied, str):
                if not hmac.compare_digest(actual, supplied):
                    raise ValueError("PackVM orphan recovery proof does not match")
            elif isinstance(actual, int) and isinstance(supplied, int):
                if actual != supplied:
                    raise ValueError("PackVM orphan recovery proof does not match")
            else:
                raise ValueError("PackVM orphan recovery proof is incomplete")
        self._verify_recovery_environment(recovery)
        limactl = self._require_command()
        missing = not self._instance_exists(limactl)
        if not missing:
            limactl = self._verify_exact_recovery_instance(recovery)
            self._checked_call(
                (limactl, "delete", "--force", self._instance),
                timeout=120,
                stage="cleanup_delete",
            )
        self._audit(
            "failed_provision_deleted",
            None,
            details={"missing": missing, "plan_digest": recovery["plan_digest"]},
        )
        self.recovery_path.unlink(missing_ok=True)
        return {"missing": missing}

    def invoke_guest(self, request: Mapping[str, Any]) -> Mapping[str, Any]:
        """Invoke only through the authenticated guest supervisor channel."""
        return self._sensitive_guest_call(request, artifact=False)

    def materialize_artifact(self, request: Mapping[str, Any]) -> Mapping[str, Any]:
        """Stage one Host-captured artifact through the root-owned guest supervisor."""

        if request.get("operation") != "materialize":
            raise ValueError("PackVM artifact request operation is invalid")
        return self._sensitive_guest_call(request, artifact=True)

    def _sensitive_guest_call(
        self,
        request: Mapping[str, Any],
        *,
        artifact: bool,
    ) -> Mapping[str, Any]:
        """Bind a sensitive shell transcript to the exact attested instance."""

        encoded = json.dumps(request, sort_keys=True, separators=(",", ":"))
        maximum = MAX_PACKVM_ARTIFACT_REQUEST_BYTES if artifact else 1024 * 1024
        if len(encoded.encode()) > maximum:
            label = "artifact" if artifact else "supervisor"
            raise ValueError(f"PackVM {label} request is too large")
        state = self._load_authenticated_state()
        attestation = str(state.get("attestation_digest") or "")
        binding = {
            "attestation_digest": attestation,
            "request_digest": _sha256(encoded.encode()),
            "operation_nonce": secrets.token_hex(32),
        }
        with self.operation_gate("guest_shell", binding):
            limactl = self._verify_exact_current_instance(state, require_guest=True)
            command: tuple[str, ...]
            if artifact:
                command = (
                    limactl,
                    "shell",
                    self._instance,
                    "--",
                    "sudo",
                    PACKVM_GUEST_RUNNER,
                )
            else:
                command = (
                    limactl,
                    "shell",
                    self._instance,
                    "--",
                    "sudo",
                    "timeout",
                    "--signal=TERM",
                    "--kill-after=1s",
                    "60s",
                    PACKVM_GUEST_RUNNER,
                )
            result = self._checked_call(
                command,
                input_text=encoded,
                timeout=180 if artifact else 65,
                max_stdin_bytes=maximum,
            )
            try:
                response = json.loads(_decode(result.stdout))
            except json.JSONDecodeError as exc:
                raise PackVMResponseReconciliationRequired(
                    "PackVM response is invalid; reconciliation is required"
                ) from exc
            self._verify_sensitive_response(request, response)
            self._verify_exact_current_instance(state, require_guest=True)
            response_digest = _canonical_digest(response)
            challenge = _canonical_digest(
                {
                    **binding,
                    "response_digest": response_digest,
                }
            )[7:]
            self._verify_guest_transcript_challenge(limactl, challenge)
            self._verify_exact_current_instance(state, require_guest=True)
            return response

    def _verify_sensitive_response(
        self,
        request: Mapping[str, Any],
        response: object,
    ) -> None:
        """Reject responses missing the request's guest-owned identities."""

        if not isinstance(response, dict) or response.get("protocol") != PACKVM_PROTOCOL:
            raise PackVMResponseReconciliationRequired(
                "PackVM response identity is missing; reconciliation is required"
            )
        operation = request.get("operation")
        fields: tuple[str, ...]
        if operation == "materialize":
            fields = ("artifact_digest", "materialization_digest")
        elif operation == "invoke":
            fields = ("guest_artifact_identity",)
        else:
            fields = ()
        for field in fields:
            expected = request.get(field)
            actual = response.get(field)
            if not isinstance(expected, str) or not isinstance(actual, str):
                raise PackVMResponseReconciliationRequired(
                    "PackVM response identity is missing; reconciliation is required"
                )
            if not hmac.compare_digest(expected, actual):
                raise PackVMResponseReconciliationRequired(
                    "PackVM response identity changed; reconciliation is required"
                )

    def _verify_guest_transcript_challenge(self, limactl: str, challenge: str) -> None:
        """Prove the post-response guest still owns the exact transcript nonce."""

        invoked = self._checked_call(
            (limactl, "shell", self._instance, "--", PACKVM_GUEST_RUNNER),
            input_text=json.dumps(
                {
                    "operation": "invoke",
                    "contract_id": "io.tobkiri.packvm.attestation.v1",
                    "operation_id": "challenge",
                    "payload": {"challenge": challenge},
                },
                sort_keys=True,
                separators=(",", ":"),
            ),
            timeout=10,
        )
        response = json.loads(_decode(invoked.stdout))
        expected = _sha256(challenge.encode())
        if (
            not isinstance(response, dict)
            or response.get("ok") is not True
            or response.get("protocol") != PACKVM_PROTOCOL
            or not isinstance(response.get("payload"), dict)
            or response["payload"].get("challenge_digest") != expected
        ):
            raise PackVMResponseReconciliationRequired(
                "PackVM response transcript mismatch; reconciliation is required"
            )

    def _plan_for_consumed_nonce(self, nonce: str) -> PackVMProvisioningPlan:
        # Rebuild immutable facts while preserving the already reviewed nonce.
        image = _PACKVM_IMAGES[self._machine]
        limactl = self._resolve_command()
        config = self._rendered_config()
        image_cache_status, image_cache_reason = self._packvm_image_cache_status()
        image_download_required = not self._instance_exists(limactl) and (
            image_cache_status != "verified_source"
        )
        image_download_bytes = (
            self._image_cache.remaining_bytes(
                self._image_authority(
                    plan_digest="sha256:" + "0" * 64,
                    session_digest="sha256:" + "0" * 64,
                    operation_id="prepare",
                )
            )
            if image_download_required and image_cache_status != "unsafe"
            else (int(str(image["size_bytes"])) if image_download_required else 0)
        )
        runtime_path_reason = _packvm_runtime_path_diagnostic(self._lima_home)
        required_space = self._required_host_space(image_download_bytes)
        available_space, storage_reason = self._host_free_space(required_space)
        facts = {
            "backend_id": PACKVM_BACKEND_ID,
            "instance": self._instance,
            "limactl_digest": _file_digest(Path(limactl)) if limactl else None,
            "architecture": self._machine,
            "image_source": image["url"],
            "image_digest": image["digest"],
            "image_size_bytes": image["size_bytes"],
            "image_download_required": image_download_required,
            "image_download_bytes": image_download_bytes,
            "image_cache_status": image_cache_status,
            "disk_size_bytes": PACKVM_DISK_SIZE_BYTES,
            "host_free_space_required_bytes": required_space,
            "config_digest": self._config_digest(config),
            "guest_runner_digest": _file_digest(_PACKVM_RUNNER),
            "host_build_digest": _file_digest(Path(__file__)),
            "runtime_root_digest": _sha256(str(self._lima_home).encode()),
            "runtime_path_status": "unsafe" if runtime_path_reason else "ready",
            "ceremony_nonce": nonce,
        }
        digest = _canonical_digest(facts)
        return PackVMProvisioningPlan(
            backend_id=PACKVM_BACKEND_ID,
            instance=self._instance,
            limactl=limactl,
            launcher_reason=self._launcher_reason(limactl),
            architecture=self._machine,
            image_source=str(image["url"]),
            image_digest=str(image["digest"]),
            image_size_bytes=int(str(image["size_bytes"])),
            image_download_required=bool(facts["image_download_required"]),
            image_download_bytes=image_download_bytes,
            image_cache_status=image_cache_status,
            image_cache_reason=image_cache_reason,
            disk_size_bytes=PACKVM_DISK_SIZE_BYTES,
            host_free_space_required_bytes=required_space,
            host_free_space_available_bytes=available_space,
            host_free_space_reason=storage_reason,
            config_digest=str(facts["config_digest"]),
            guest_runner_digest=str(facts["guest_runner_digest"]),
            host_build_digest=str(facts["host_build_digest"]),
            runtime_root_digest=str(facts["runtime_root_digest"]),
            runtime_path_status=str(facts["runtime_path_status"]),
            runtime_path_reason=runtime_path_reason,
            ceremony_nonce=nonce,
            plan_digest=digest,
            confirmation=f"{PACKVM_CONFIRMATION_PREFIX} {self._instance} {digest[7:19]}",
        )

    def _rendered_config(
        self,
        *,
        image_location: str | None = None,
    ) -> bytes:
        image = _PACKVM_IMAGES[self._machine]
        gibibyte = 1024**3
        if (
            PACKVM_DISK_SIZE_BYTES < PACKVM_MIN_DISK_SIZE_BYTES
            or PACKVM_DISK_SIZE_BYTES % gibibyte != 0
        ):
            raise ValueError("PackVM disk policy is below the bounded runtime minimum")
        if image_location is None:
            image_location = "http://127.0.0.1:0/packvm-image/" + "0" * 64
        template = _PACKVM_CONFIG.read_text(encoding="utf-8")
        rendered = (
            template.replace("{{ARCH}}", str(image["lima_arch"]))
            .replace("{{IMAGE_URL}}", image_location)
            .replace("{{IMAGE_DIGEST}}", str(image["digest"]))
            .replace("{{DISK_SIZE_GIB}}", str(PACKVM_DISK_SIZE_BYTES // gibibyte))
        )
        return rendered.encode()

    def _config_digest(self, config: bytes) -> str:
        """Return the consent digest for exact PackVM config semantics."""

        return _packvm_config_semantic_digest(config)

    @staticmethod
    def _require_config_image_location(config: bytes, expected: str) -> None:
        """Require executed YAML to name the exact active one-shot endpoint."""

        loaded = yaml.safe_load(config)
        images = loaded.get("images") if isinstance(loaded, dict) else None
        if (
            not isinstance(images, list)
            or len(images) != 1
            or not isinstance(images[0], dict)
            or images[0].get("location") != expected
        ):
            raise ValueError("PackVM executed Lima image locator changed")

    def _scrub_lima_handoff_artifacts(self, endpoint: str, sensitive_values: Sequence[str]) -> None:
        """Replace the ephemeral endpoint in private Lima metadata and logs."""

        instance_directory = self._lima_home / self._instance
        try:
            initial = instance_directory.lstat()
        except FileNotFoundError:
            return
        if (
            instance_directory.is_symlink()
            or not stat.S_ISDIR(initial.st_mode)
            or (hasattr(os, "getuid") and initial.st_uid != os.getuid())
            or (os.name == "posix" and initial.st_mode & 0o077)
        ):
            raise ValueError("PackVM Lima handoff metadata directory is unsafe")
        descriptor, device, inode, chain = _open_pinned_owned_directory(instance_directory)
        visited = 0
        scanned_bytes = 0
        deadline = time.monotonic() + PACKVM_LIMA_SCRUB_DEADLINE_SECONDS
        patterns = tuple(
            sorted(
                {value.encode() for value in sensitive_values if value},
                key=len,
                reverse=True,
            )
        )
        prepared: list[_PreparedLimaScrub] = []

        def require_budget(count: int) -> None:
            nonlocal scanned_bytes
            scanned_bytes += count
            if scanned_bytes > PACKVM_LIMA_SCRUB_MAX_TOTAL_BYTES:
                raise ValueError("PackVM Lima handoff metadata scan exceeded its byte bound")
            if time.monotonic() >= deadline:
                raise ValueError("PackVM Lima handoff metadata scan timed out")

        def scrub_directory(directory_descriptor: int, *, depth: int) -> None:
            nonlocal visited
            if depth > 16:
                raise ValueError("PackVM Lima handoff metadata is too deeply nested")
            for name in os.listdir(directory_descriptor):
                require_budget(0)
                visited += 1
                if visited > 16_384:
                    raise ValueError("PackVM Lima handoff metadata is too large")
                if name in {".", ".."} or "/" in name or "\x00" in name:
                    raise ValueError("PackVM Lima handoff metadata name is unsafe")
                try:
                    metadata = os.stat(name, dir_fd=directory_descriptor, follow_symlinks=False)
                except FileNotFoundError:
                    continue
                if stat.S_ISLNK(metadata.st_mode):
                    raise ValueError("PackVM Lima handoff metadata link is unsafe")
                if stat.S_ISDIR(metadata.st_mode):
                    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
                    if hasattr(os, "O_NOFOLLOW"):
                        flags |= os.O_NOFOLLOW
                    child_descriptor = os.open(name, flags, dir_fd=directory_descriptor)
                    try:
                        opened = os.fstat(child_descriptor)
                        if (opened.st_dev, opened.st_ino) != (metadata.st_dev, metadata.st_ino) or (
                            hasattr(os, "getuid") and opened.st_uid != os.getuid()
                        ):
                            raise ValueError("PackVM Lima handoff metadata directory changed")
                        scrub_directory(child_descriptor, depth=depth + 1)
                        current = os.stat(
                            name,
                            dir_fd=directory_descriptor,
                            follow_symlinks=False,
                        )
                        if (current.st_dev, current.st_ino) != (
                            opened.st_dev,
                            opened.st_ino,
                        ):
                            raise ValueError("PackVM Lima handoff metadata directory changed")
                    finally:
                        os.close(child_descriptor)
                    continue
                if not stat.S_ISREG(metadata.st_mode):
                    continue
                if depth == 0 and name in _PACKVM_LIMA_BULK_PAYLOAD_NAMES:
                    if (
                        metadata.st_nlink != 1
                        or (hasattr(os, "getuid") and metadata.st_uid != os.getuid())
                    ):
                        raise ValueError("PackVM Lima bulk payload is unsafe")
                    continue
                if metadata.st_size > PACKVM_LIMA_SCRUB_MAX_FILE_BYTES:
                    raise ValueError("PackVM Lima handoff metadata file exceeds its scan bound")
                file_descriptor = os.open(
                    name,
                    os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
                    dir_fd=directory_descriptor,
                )
                try:
                    opened = os.fstat(file_descriptor)
                    if (
                        opened.st_nlink != 1
                        or (opened.st_dev, opened.st_ino) != (metadata.st_dev, metadata.st_ino)
                        or (hasattr(os, "getuid") and opened.st_uid != os.getuid())
                    ):
                        raise ValueError("PackVM Lima handoff metadata is unsafe")
                    replacements = tuple(
                        (
                            pattern,
                            (
                                str(_PACKVM_IMAGES[self._machine]["url"]).encode()
                                if depth == 0
                                and name == "lima.yaml"
                                and hmac.compare_digest(pattern, endpoint.encode())
                                else b"<packvm-handoff-redacted>"
                            ),
                        )
                        for pattern in patterns
                    )
                    candidate = self._stream_redact_lima_metadata_file(
                        directory_descriptor=directory_descriptor,
                        name=name,
                        descriptor=file_descriptor,
                        expected=opened,
                        replacements=replacements,
                        require_budget=require_budget,
                    )
                    if candidate is not None:
                        prepared.append(candidate)
                finally:
                    os.close(file_descriptor)

        try:
            scrub_directory(descriptor, depth=0)
            _require_pinned_directory_identity(instance_directory, descriptor, device, inode, chain)
            for candidate in prepared:
                self._commit_lima_metadata_scrub(candidate)
            _require_pinned_directory_identity(instance_directory, descriptor, device, inode, chain)
        finally:
            for candidate in prepared:
                try:
                    os.unlink(
                        candidate.temporary_name,
                        dir_fd=candidate.directory_descriptor,
                    )
                except FileNotFoundError:
                    pass
                os.close(candidate.temporary_descriptor)
                os.close(candidate.source_descriptor)
                os.close(candidate.directory_descriptor)
            os.close(descriptor)

    @staticmethod
    def _stream_redact_lima_metadata_file(
        *,
        directory_descriptor: int,
        name: str,
        descriptor: int,
        expected: os.stat_result,
        replacements: Sequence[tuple[bytes, bytes]],
        require_budget: Callable[[int], None],
    ) -> _PreparedLimaScrub | None:
        """Stream-scan and atomically redact one pinned private Lima file."""

        max_pattern = max((len(pattern) for pattern, _value in replacements), default=1)
        carry = b""
        matched = False
        binary = False
        offset = 0
        while offset < expected.st_size:
            chunk = os.pread(
                descriptor,
                min(PACKVM_LIMA_SCRUB_CHUNK_BYTES, expected.st_size - offset),
                offset,
            )
            if not chunk:
                raise ValueError("PackVM Lima handoff metadata was truncated")
            require_budget(len(chunk))
            binary = binary or b"\x00" in chunk
            window = carry + chunk
            if any(pattern in window for pattern, _value in replacements):
                matched = True
            carry = window[-(max_pattern - 1) :] if max_pattern > 1 else b""
            offset += len(chunk)
        if os.pread(descriptor, 1, offset):
            raise ValueError("PackVM Lima handoff metadata exceeded its attested size")
        after_scan = os.fstat(descriptor)
        stable_fields = ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns")
        if any(getattr(after_scan, field) != getattr(expected, field) for field in stable_fields):
            raise ValueError("PackVM Lima handoff metadata changed during scan")
        if not matched:
            return None
        if binary:
            raise ValueError("PackVM Lima binary metadata contains a handoff token")

        current = os.stat(name, dir_fd=directory_descriptor, follow_symlinks=False)
        if (current.st_dev, current.st_ino) != (expected.st_dev, expected.st_ino):
            raise ValueError("PackVM Lima handoff metadata identity changed")
        temporary = f".packvm-scrub-{secrets.token_hex(16)}"
        flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        temporary_descriptor = os.open(temporary, flags, 0o600, dir_fd=directory_descriptor)
        keep_temporary = False
        try:
            os.fchmod(temporary_descriptor, stat.S_IMODE(expected.st_mode))
            buffer = b""
            offset = 0
            while offset < expected.st_size:
                chunk = os.pread(
                    descriptor,
                    min(PACKVM_LIMA_SCRUB_CHUNK_BYTES, expected.st_size - offset),
                    offset,
                )
                if not chunk:
                    raise ValueError("PackVM Lima handoff metadata was truncated")
                require_budget(len(chunk))
                buffer += chunk
                offset += len(chunk)
                safe_limit = max(0, len(buffer) - max_pattern + 1)
                output = bytearray()
                cursor = 0
                while cursor < safe_limit:
                    matches = (
                        (position, -len(pattern), pattern, value)
                        for pattern, value in replacements
                        if (position := buffer.find(pattern, cursor)) >= 0
                        and position < safe_limit
                    )
                    match = min(matches, default=None)
                    if match is None:
                        output.extend(buffer[cursor:safe_limit])
                        cursor = safe_limit
                        continue
                    position, _negative_length, pattern, value = match
                    output.extend(buffer[cursor:position])
                    output.extend(value)
                    cursor = position + len(pattern)
                PackVMLimaProvisioner._write_all(temporary_descriptor, bytes(output))
                buffer = buffer[cursor:]
            cursor = 0
            output = bytearray()
            while cursor < len(buffer):
                matches = (
                    (position, -len(pattern), pattern, value)
                    for pattern, value in replacements
                    if (position := buffer.find(pattern, cursor)) >= 0
                )
                match = min(matches, default=None)
                if match is None:
                    output.extend(buffer[cursor:])
                    break
                position, _negative_length, pattern, value = match
                output.extend(buffer[cursor:position])
                output.extend(value)
                cursor = position + len(pattern)
            PackVMLimaProvisioner._write_all(temporary_descriptor, bytes(output))
            require_budget(0)
            after_rewrite = os.fstat(descriptor)
            if any(
                getattr(after_rewrite, field) != getattr(expected, field)
                for field in stable_fields
            ):
                raise ValueError("PackVM Lima handoff metadata changed during redaction")
            current = os.stat(name, dir_fd=directory_descriptor, follow_symlinks=False)
            current_source = os.fstat(descriptor)
            if (
                (current.st_dev, current.st_ino) != (expected.st_dev, expected.st_ino)
                or current_source.st_nlink != 1
                or (
                    hasattr(os, "getuid")
                    and current_source.st_uid != os.getuid()
                )
            ):
                raise ValueError("PackVM Lima handoff metadata identity changed")
            os.fsync(temporary_descriptor)
            require_budget(0)
            pinned_directory = os.dup(directory_descriptor)
            try:
                pinned_source = os.dup(descriptor)
            except Exception:
                os.close(pinned_directory)
                raise
            candidate = _PreparedLimaScrub(
                directory_descriptor=pinned_directory,
                source_descriptor=pinned_source,
                temporary_descriptor=temporary_descriptor,
                name=name,
                temporary_name=temporary,
                expected=expected,
            )
            keep_temporary = True
            return candidate
        finally:
            if not keep_temporary:
                os.close(temporary_descriptor)
                try:
                    os.unlink(temporary, dir_fd=directory_descriptor)
                except FileNotFoundError:
                    pass

    @staticmethod
    def _commit_lima_metadata_scrub(candidate: _PreparedLimaScrub) -> None:
        """Publish one prepared redaction after final alias and identity checks."""

        source = os.fstat(candidate.source_descriptor)
        current = os.stat(
            candidate.name,
            dir_fd=candidate.directory_descriptor,
            follow_symlinks=False,
        )
        expected = candidate.expected
        stable_fields = ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns")
        if (
            any(getattr(source, field) != getattr(expected, field) for field in stable_fields)
            or source.st_nlink != 1
            or (current.st_dev, current.st_ino) != (source.st_dev, source.st_ino)
            or current.st_nlink != 1
            or (hasattr(os, "getuid") and source.st_uid != os.getuid())
        ):
            raise ValueError("PackVM Lima handoff metadata acquired an unsafe alias")
        replacement = os.fstat(candidate.temporary_descriptor)
        if (
            not stat.S_ISREG(replacement.st_mode)
            or replacement.st_nlink != 1
            or (hasattr(os, "getuid") and replacement.st_uid != os.getuid())
        ):
            raise ValueError("PackVM Lima handoff replacement is unsafe")
        os.replace(
            candidate.temporary_name,
            candidate.name,
            src_dir_fd=candidate.directory_descriptor,
            dst_dir_fd=candidate.directory_descriptor,
        )
        os.fsync(candidate.directory_descriptor)
        old_after = os.fstat(candidate.source_descriptor)
        published = os.stat(
            candidate.name,
            dir_fd=candidate.directory_descriptor,
            follow_symlinks=False,
        )
        if (
            (old_after.st_dev, old_after.st_ino) != (source.st_dev, source.st_ino)
            or old_after.st_nlink != 0
            or (hasattr(os, "getuid") and old_after.st_uid != os.getuid())
            or (published.st_dev, published.st_ino)
            != (replacement.st_dev, replacement.st_ino)
            or published.st_nlink != 1
        ):
            raise ValueError("PackVM Lima handoff metadata alias survived redaction")

    @staticmethod
    def _write_all(descriptor: int, payload: bytes) -> None:
        """Write all bytes to one already pinned descriptor."""

        written = 0
        while written < len(payload):
            count = os.write(descriptor, payload[written:])
            if count <= 0:
                raise ValueError("PackVM Lima handoff metadata write was incomplete")
            written += count

    def _image_authority(
        self, *, plan_digest: str, session_digest: str, operation_id: str
    ) -> PackVMImageAuthority:
        """Build acquisition authority exclusively from server-owned signed facts."""

        image = _PACKVM_IMAGES[self._machine]
        return PackVMImageAuthority(
            source_url=str(image["url"]),
            digest=str(image["digest"]),
            size_bytes=int(str(image["size_bytes"])),
            platform="macos",
            architecture=self._machine,
            plan_digest=plan_digest,
            session_digest=session_digest,
            operation_id=operation_id,
        )

    def _packvm_image_cache_status(self) -> tuple[str, str | None]:
        """Classify only the dedicated PackVM cache, never Lima user state."""

        return self._image_cache.status(
            self._image_authority(
                plan_digest="sha256:" + "0" * 64,
                session_digest="sha256:" + "0" * 64,
                operation_id="prepare",
            )
        )

    def _staging_image_path(self, authority: PackVMImageAuthority) -> Path:
        """Return the deterministic Lima handoff path for one content digest."""

        return (
            self._state_dir
            / "packvm-image-staging"
            / f"{authority.digest.removeprefix('sha256:')}.img"
        )

    @contextmanager
    def _staged_image(
        self,
        pinned: PackVMPinnedImage,
        *,
        progress: Callable[[PackVMImageProgress], None] | None,
        cancelled: Callable[[], bool] | None,
    ) -> Iterator[_PinnedStagedImage]:
        """Copy verified bytes into an unlinked crash-safe staging inode."""

        authority = self._image_authority(
            plan_digest="sha256:" + "0" * 64,
            session_digest="sha256:" + "0" * 64,
            operation_id="staging",
        )
        verified = pinned.verified
        staging_path = self._staging_image_path(authority)
        staging_directory = staging_path.parent
        directory_descriptor = -1
        source_descriptor = -1
        temporary_descriptor = -1
        temporary_name = f".packvm-stage-{secrets.token_hex(16)}"
        try:
            (
                directory_descriptor,
                directory_device,
                directory_inode,
                directory_chain,
            ) = _open_pinned_owned_directory(staging_directory)
            source_descriptor = os.dup(pinned.descriptor)
            self._reconcile_legacy_staging(staging_path, directory_descriptor, verified)
            temporary_flags = os.O_CREAT | os.O_EXCL | os.O_RDWR
            if hasattr(os, "O_NOFOLLOW"):
                temporary_flags |= os.O_NOFOLLOW
            temporary_descriptor = os.open(
                temporary_name,
                temporary_flags,
                0o600,
                dir_fd=directory_descriptor,
            )
            # The inode has no pathname before any callback or image copy.  A
            # crash closes the descriptor and the filesystem reclaims it.
            os.unlink(temporary_name, dir_fd=directory_descriptor)
            os.fsync(directory_descriptor)
            source_metadata = os.fstat(source_descriptor)
            if (
                not stat.S_ISREG(source_metadata.st_mode)
                or source_metadata.st_nlink != 1
                or source_metadata.st_dev != verified.device
                or source_metadata.st_ino != verified.inode
            ):
                raise ValueError("PackVM verified source changed before Lima staging")
            hasher = hashlib.sha256()
            copied = 0
            while copied < verified.size_bytes:
                if cancelled is not None and cancelled():
                    raise PackVMImageCancelled(
                        "packvm_image_cancelled",
                        "PackVM image staging was cancelled",
                    )
                chunk = os.pread(
                    source_descriptor,
                    min(1024 * 1024, verified.size_bytes - copied),
                    copied,
                )
                if not chunk:
                    raise ValueError("PackVM verified source truncated during staging")
                if os.write(temporary_descriptor, chunk) != len(chunk):
                    raise ValueError("PackVM Lima staging write was incomplete")
                hasher.update(chunk)
                copied += len(chunk)
            if os.pread(source_descriptor, 1, copied):
                raise ValueError("PackVM verified source grew during staging")
            digest = "sha256:" + hasher.hexdigest()
            if not hmac.compare_digest(digest, verified.digest):
                raise ValueError("PackVM Lima staging digest changed")
            if cancelled is not None and cancelled():
                raise PackVMImageCancelled(
                    "packvm_image_cancelled",
                    "PackVM image staging was cancelled",
                )
            os.fsync(temporary_descriptor)
            os.fchmod(temporary_descriptor, 0o400)
            _require_pinned_directory_identity(
                staging_directory,
                directory_descriptor,
                directory_device,
                directory_inode,
                directory_chain,
            )
            staged_metadata = os.fstat(temporary_descriptor)
            if staged_metadata.st_nlink != 0:
                raise ValueError("PackVM Lima staging inode remained named")
            staged_verified = PackVMVerifiedImage(
                path=staging_path,
                digest=digest,
                size_bytes=copied,
                device=staged_metadata.st_dev,
                inode=staged_metadata.st_ino,
                source_url=verified.source_url,
            )
            staged = _PinnedStagedImage(staged_verified, temporary_descriptor)
            self._verify_sealed_staged_identity(staged)
            if progress is not None:
                progress(
                    PackVMImageProgress(
                        "verified",
                        verified.size_bytes,
                        verified.size_bytes,
                        verified.size_bytes,
                    )
                )
            if cancelled is not None and cancelled():
                raise PackVMImageCancelled(
                    "packvm_image_cancelled",
                    "PackVM image staging was cancelled",
                )
            yield staged
        finally:
            if directory_descriptor >= 0:
                try:
                    os.unlink(temporary_name, dir_fd=directory_descriptor)
                except FileNotFoundError:
                    pass
                os.fsync(directory_descriptor)
            if temporary_descriptor >= 0:
                os.close(temporary_descriptor)
            if source_descriptor >= 0:
                os.close(source_descriptor)
            if directory_descriptor >= 0:
                os.close(directory_descriptor)

    def _reconcile_legacy_staging(
        self,
        staging_path: Path,
        directory_descriptor: int,
        verified: PackVMVerifiedImage,
    ) -> None:
        """Remove only a valid staging inode stranded by the flag-based design."""

        if platform.system() != "Darwin" or not hasattr(stat, "UF_IMMUTABLE"):
            return
        directory_metadata = os.fstat(directory_descriptor)
        try:
            descriptor = os.open(
                staging_path.name,
                os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=directory_descriptor,
            )
        except FileNotFoundError:
            return
        try:
            metadata = os.fstat(descriptor)
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_nlink != 1
                or metadata.st_size != verified.size_bytes
                or (hasattr(os, "getuid") and metadata.st_uid != os.getuid())
                or not _darwin_stat_flags(metadata) & stat.UF_IMMUTABLE
                or not _darwin_stat_flags(directory_metadata) & stat.UF_IMMUTABLE
            ):
                raise ValueError("PackVM legacy staging residue is unsafe")
            hasher = hashlib.sha256()
            offset = 0
            while offset < verified.size_bytes:
                chunk = os.pread(
                    descriptor,
                    min(1024 * 1024, verified.size_bytes - offset),
                    offset,
                )
                if not chunk:
                    raise ValueError("PackVM legacy staging residue is truncated")
                hasher.update(chunk)
                offset += len(chunk)
            if os.pread(descriptor, 1, offset) or not hmac.compare_digest(
                "sha256:" + hasher.hexdigest(), verified.digest
            ):
                raise ValueError("PackVM legacy staging residue digest changed")
            current = os.stat(
                staging_path.name,
                dir_fd=directory_descriptor,
                follow_symlinks=False,
            )
            if (current.st_dev, current.st_ino) != (metadata.st_dev, metadata.st_ino):
                raise ValueError("PackVM legacy staging residue identity changed")
            _set_descriptor_flags(
                descriptor,
                _darwin_stat_flags(metadata) & ~int(stat.UF_IMMUTABLE),
            )
            _set_descriptor_flags(
                directory_descriptor,
                _darwin_stat_flags(directory_metadata) & ~int(stat.UF_IMMUTABLE),
            )
            os.unlink(staging_path.name, dir_fd=directory_descriptor)
            os.fsync(directory_descriptor)
        finally:
            os.close(descriptor)

    def _seal_staged_image(self, staged: _PinnedStagedImage) -> None:
        """Rehash the unlinked local image immediately around consumption."""

        self._verify_sealed_staged_identity(staged)
        verified = staged.verified
        before = os.fstat(staged.image_descriptor)
        hasher = hashlib.sha256()
        offset = 0
        while offset < verified.size_bytes:
            chunk = os.pread(
                staged.image_descriptor,
                min(1024 * 1024, verified.size_bytes - offset),
                offset,
            )
            if not chunk:
                raise ValueError("PackVM Lima staged image truncated before execution")
            hasher.update(chunk)
            offset += len(chunk)
        if os.pread(staged.image_descriptor, 1, offset):
            raise ValueError("PackVM Lima staged image grew before execution")
        after = os.fstat(staged.image_descriptor)
        if (before.st_dev, before.st_ino, before.st_size) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
        ) or not hmac.compare_digest("sha256:" + hasher.hexdigest(), verified.digest):
            raise ValueError("PackVM Lima staged image digest changed before execution")
        self._verify_sealed_staged_identity(staged)

    def _verify_sealed_staged_identity(self, staged: _PinnedStagedImage) -> None:
        """Require the complete staging inode to remain unlinked and pinned."""

        metadata = os.fstat(staged.image_descriptor)
        verified = staged.verified
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 0
            or metadata.st_dev != verified.device
            or metadata.st_ino != verified.inode
            or metadata.st_size != verified.size_bytes
        ):
            raise ValueError("PackVM Lima sealed image identity changed")

    def _required_host_space(self, image_download_bytes: int) -> int:
        """Return the fail-closed host capacity needed for sparse VM growth."""

        return (
            PACKVM_DISK_SIZE_BYTES
            + PACKVM_HOST_STORAGE_RESERVE_BYTES
            + PACKVM_PINNED_IMAGE_VIRTUAL_SIZE_BYTES
            # Lima 2.2 may materialize source, converted, and raw cache copies
            # below its isolated operation HOME before creating the VM disk.
            + 4 * int(str(_PACKVM_IMAGES[self._machine]["size_bytes"]))
            + image_download_bytes
        )

    def _host_storage_path(self) -> Path:
        """Return the volume on which Lima stores its managed instance."""

        path = self._lima_home
        while not path.exists() and path != path.parent:
            path = path.parent
        if not path.exists():
            raise ValueError("PackVM host storage preflight path is unavailable")
        return path

    def _host_free_space(self, required_space: int) -> tuple[int, str | None]:
        """Measure host capacity and format a stable insufficiency diagnostic."""

        try:
            self._reconcile_stale_lima_operation_root()
            available = int(self._disk_usage(self._host_storage_path()).free)
        except (AttributeError, OSError, TypeError, ValueError) as exc:
            return 0, f"PackVM host storage preflight failed: {exc}"
        if available < required_space:
            return available, (
                "PackVM provisioning requires at least "
                f"{_format_gib(required_space)} free on the Lima host volume; "
                f"only {_format_gib(available)} is available"
            )
        return available, None

    def _require_host_capacity(self, image_download_bytes: int) -> None:
        """Fail before Lima mutation when bounded host capacity is unavailable."""

        required = self._required_host_space(image_download_bytes)
        _available, reason = self._host_free_space(required)
        if reason is not None:
            raise ValueError(reason)

    def _validate_managed_identity(self) -> None:
        """Require the fixed instance and state-owned dedicated Lima home."""

        if self._instance != PACKVM_LIMA_INSTANCE:
            raise ValueError("PackVM instance must use the fixed managed identity")
        if not self._requested_lima_home.is_absolute():
            raise ValueError("PackVM LIMA_HOME must be an absolute dedicated directory")
        if self._requested_lima_home != self._lima_home:
            raise ValueError("PackVM LIMA_HOME must not contain symlinks or traversal")
        user_home = str(os.environ.get("HOME") or "").strip()
        if user_home and self._lima_home == Path(user_home).resolve() / ".lima":
            raise ValueError("PackVM must never use the user default ~/.lima directory")

    def _ensure_private_managed_directories(self) -> None:
        """Create and revalidate the private state and dedicated Lima roots."""

        self._validate_managed_identity()
        for directory in (
            self._state_dir,
            self._lima_home,
            self._state_dir / "packvm-lima-process-home",
        ):
            _ensure_owned_directory_chain(directory)
        self._validate_managed_identity()

    @contextmanager
    def _lima_handoff_operation_environment(self) -> Iterator[Path]:
        """Isolate and deterministically reclaim Lima downloader cache state."""

        if self._active_lima_operation_root is not None:
            raise ValueError("PackVM Lima operation environment is already active")
        root = self._state_dir / "packvm-lima-handoff-operation"
        with self._lima_operation_cache_lock():
            _ensure_owned_directory_chain(root)
            self._clear_private_operation_root(root)
            for child in ("home", "cache", "tmp"):
                _ensure_owned_directory_chain(root / child)
            self._active_lima_operation_root = root
            try:
                yield root
            finally:
                self._active_lima_operation_root = None
                self._clear_private_operation_root(root)

    def _reconcile_stale_lima_operation_root(self) -> None:
        """Reclaim crash residue only when no live process owns its OS lock."""

        root = self._state_dir / "packvm-lima-handoff-operation"
        if not root.exists() and not root.is_symlink():
            return
        try:
            with self._lima_operation_cache_lock():
                self._clear_private_operation_root(root)
        except _FileLockUnavailable:
            # A live/unknown process retains authority over this cache. Its
            # allocated bytes remain visible to the host free-space check.
            return

    @contextmanager
    def _lima_operation_cache_lock(self) -> Iterator[int]:
        """Hold the stable cross-process ownership lock for downloader cache."""

        _ensure_owned_directory_chain(self._state_dir)
        path = self._state_dir / "packvm-lima-handoff-operation.lock"
        flags = os.O_CREAT | os.O_RDWR
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(path, flags, 0o600)
        locked = False
        try:
            metadata = os.fstat(descriptor)
            current = path.lstat()
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_nlink != 1
                or (metadata.st_dev, metadata.st_ino)
                != (current.st_dev, current.st_ino)
                or (hasattr(os, "getuid") and metadata.st_uid != os.getuid())
            ):
                raise ValueError("PackVM Lima operation lock is unsafe")
            _acquire_exclusive_file_lock(descriptor, timeout_seconds=0)
            locked = True
            yield descriptor
        finally:
            try:
                if locked:
                    _release_exclusive_file_lock(descriptor)
            finally:
                os.close(descriptor)

    @staticmethod
    def _clear_private_operation_root(root: Path) -> None:
        """Descriptor-relatively delete only proven owned operation artifacts."""

        descriptor, device, inode, chain = _open_pinned_owned_directory(root)

        def clear(directory_descriptor: int) -> None:
            for name in os.listdir(directory_descriptor):
                if name in {".", ".."} or "/" in name or "\x00" in name:
                    raise ValueError("PackVM Lima operation artifact name is unsafe")
                metadata = os.stat(
                    name, dir_fd=directory_descriptor, follow_symlinks=False
                )
                if stat.S_ISLNK(metadata.st_mode):
                    os.unlink(name, dir_fd=directory_descriptor)
                    continue
                if stat.S_ISDIR(metadata.st_mode):
                    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
                    if hasattr(os, "O_NOFOLLOW"):
                        flags |= os.O_NOFOLLOW
                    child = os.open(name, flags, dir_fd=directory_descriptor)
                    try:
                        opened = os.fstat(child)
                        if (
                            (opened.st_dev, opened.st_ino)
                            != (metadata.st_dev, metadata.st_ino)
                            or (
                                hasattr(os, "getuid")
                                and opened.st_uid != os.getuid()
                            )
                        ):
                            raise ValueError(
                                "PackVM Lima operation directory changed"
                            )
                        clear(child)
                    finally:
                        os.close(child)
                    current = os.stat(
                        name, dir_fd=directory_descriptor, follow_symlinks=False
                    )
                    if (current.st_dev, current.st_ino) != (
                        metadata.st_dev,
                        metadata.st_ino,
                    ):
                        raise ValueError("PackVM Lima operation directory changed")
                    os.rmdir(name, dir_fd=directory_descriptor)
                    continue
                if (
                    not stat.S_ISREG(metadata.st_mode)
                    or metadata.st_nlink != 1
                    or (
                        hasattr(os, "getuid")
                        and metadata.st_uid != os.getuid()
                    )
                ):
                    raise ValueError("PackVM Lima operation artifact is unsafe")
                os.unlink(name, dir_fd=directory_descriptor)
            os.fsync(directory_descriptor)

        try:
            clear(descriptor)
            _require_pinned_directory_identity(root, descriptor, device, inode, chain)
        finally:
            os.close(descriptor)

    def _lima_process_environment(self) -> dict[str, str]:
        """Return a minimal environment pinned to the dedicated Lima home."""

        self._ensure_private_managed_directories()
        operation_root = self._active_lima_operation_root
        process_root = operation_root or (self._state_dir / "packvm-lima-process-home")
        home = process_root / "home"
        cache = process_root / "cache"
        temporary = process_root / "tmp"
        for directory in (home, cache, temporary):
            _ensure_owned_directory_chain(directory)
        environment = {
            "PATH": LIMA_PROCESS_PATH,
            "LIMA_HOME": str(self._lima_home),
            "HOME": str(home),
            "XDG_CACHE_HOME": str(cache),
            "TMPDIR": str(temporary),
        }
        return environment

    def _recovery_facts(
        self,
        plan: PackVMProvisioningPlan,
        request: PackVMProvisioningRequest,
        verified_image: PackVMVerifiedImage,
    ) -> dict[str, Any]:
        """Capture immutable server evidence before the first Lima mutation."""

        if request.session_digest is None:
            session_digest = _sha256(b"direct-local-lifecycle")
        else:
            session_digest = request.session_digest
        metadata = self._lima_home.lstat()
        return {
            "version": 1,
            "backend_id": PACKVM_BACKEND_ID,
            "instance": self._instance,
            "session_digest": session_digest,
            "plan_digest": plan.plan_digest,
            "ceremony_nonce_digest": _sha256(request.ceremony_nonce.encode()),
            "config_digest": plan.config_digest,
            "executed_config_digest": plan.config_digest,
            "image_digest": plan.image_digest,
            "image_source": plan.image_source,
            "image_local_device": verified_image.device,
            "image_local_inode": verified_image.inode,
            "guest_runner_digest": plan.guest_runner_digest,
            "host_build_digest": plan.host_build_digest,
            "limactl_digest": _file_digest(Path(str(plan.limactl))),
            "lima_home_digest": _sha256(str(self._lima_home).encode()),
            "lima_home_device": int(metadata.st_dev),
            "lima_home_inode": int(metadata.st_ino),
            "created_unix": int(time.time()),
        }

    def _sign_recovery(self, recovery: Mapping[str, Any]) -> str:
        key = generate_or_load_signing_key(self._state_dir / "packvm-recovery.key")
        unsigned = {key: value for key, value in recovery.items() if key != "authentication"}
        return hmac.new(key, _canonical_json(unsigned), hashlib.sha256).hexdigest()

    def _load_authenticated_recovery(self) -> dict[str, Any]:
        try:
            raw = _read_private_file(self.recovery_path, MAX_LIMA_STATE_BYTES)
        except FileNotFoundError as exc:
            raise ValueError("PackVM failed-provision recovery evidence is unavailable") from exc
        try:
            recovery = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError("PackVM failed-provision recovery evidence is invalid") from exc
        if not isinstance(recovery, dict) or recovery.get("version") != 1:
            raise ValueError("PackVM failed-provision recovery evidence is invalid")
        authentication = recovery.get("authentication")
        if not isinstance(authentication, str):
            raise ValueError("PackVM failed-provision recovery evidence is unauthenticated")
        expected = self._sign_recovery(recovery)
        if not hmac.compare_digest(authentication, expected):
            raise ValueError("PackVM failed-provision recovery authentication failed")
        return recovery

    def _verify_recovery_environment(self, recovery: Mapping[str, Any]) -> None:
        """Recompute every Host-owned recovery binding before deletion."""

        self._ensure_private_managed_directories()
        metadata = self._lima_home.lstat()
        current = {
            "backend_id": PACKVM_BACKEND_ID,
            "instance": self._instance,
            "config_digest": self._config_digest(self._rendered_config()),
            "executed_config_digest": self._config_digest(self._rendered_config()),
            "image_digest": _PACKVM_IMAGES[self._machine]["digest"],
            "image_source": _PACKVM_IMAGES[self._machine]["url"],
            "guest_runner_digest": _file_digest(_PACKVM_RUNNER),
            "host_build_digest": _file_digest(Path(__file__)),
            "limactl_digest": _file_digest(Path(self._require_command())),
            "lima_home_digest": _sha256(str(self._lima_home).encode()),
            "lima_home_device": int(metadata.st_dev),
            "lima_home_inode": int(metadata.st_ino),
        }
        for field, value in current.items():
            if field not in recovery and field == "image_source":
                # A compact operation-ledger proof can establish idempotent
                # absence without local-image identity because no destructive
                # target exists. Authenticated recovery always carries it.
                continue
            if recovery.get(field) != value:
                raise ValueError(f"PackVM recovery {field} changed")

    def _instance_directory_identity(self) -> dict[str, int]:
        """Return the stable filesystem identity of the fixed Lima instance."""

        instance_dir = self._lima_home / self._instance
        metadata = instance_dir.lstat()
        if (
            instance_dir.is_symlink()
            or not stat.S_ISDIR(metadata.st_mode)
            or (hasattr(os, "getuid") and metadata.st_uid != os.getuid())
            or instance_dir.parent != self._lima_home
        ):
            raise PackVMForeignInstanceError(
                "PackVM current instance directory is unsafe; reconciliation is required"
            )
        return {
            "instance_directory_device": int(metadata.st_dev),
            "instance_directory_inode": int(metadata.st_ino),
        }

    def _current_instance_payload(self, limactl: str) -> dict[str, Any]:
        """Load the fixed-name instance through the canonical dedicated Lima root."""

        instance_dir = self._lima_home / self._instance
        self._instance_directory_identity()
        payload = lima_instance_payload(
            limactl,
            self._instance,
            runner=self._runner,
            environment=self._lima_process_environment(),
        )
        payload_dir = Path(str(payload.get("dir") or ""))
        if payload_dir != instance_dir or payload_dir.is_symlink():
            raise PackVMForeignInstanceError(
                "PackVM current instance escaped the dedicated Lima home; "
                "reconciliation is required"
            )
        violation = validate_lima_instance_config(payload)
        if violation:
            raise PackVMForeignInstanceError(f"{violation}; PackVM reconciliation is required")
        return payload

    def _verify_exact_current_instance(
        self,
        state: Mapping[str, Any],
        *,
        require_guest: bool,
    ) -> str:
        """Bind one destructive target to the complete authenticated attestation.

        Lima exposes destructive operations only by name, so an external process
        can still race after this final verification.  The command is nevertheless
        pinned to the verified executable, fixed name, and isolated ``LIMA_HOME``;
        no default/user Lima namespace is ever consulted.
        """

        try:
            local_identity = {
                "config_digest": self._config_digest(self._rendered_config()),
                "image_digest": _PACKVM_IMAGES[self._machine]["digest"],
                "image_source": _PACKVM_IMAGES[self._machine]["url"],
                "guest_runner_digest": _file_digest(_PACKVM_RUNNER),
                "host_build_digest": _file_digest(Path(__file__)),
            }
            for field, value in local_identity.items():
                if state.get(field) != value:
                    raise ValueError(f"managed PackVM {field} changed")
            self._verify_attested_host_binding(state)
            limactl = self._require_command()
            identity = self._instance_directory_identity()
            for field, value in identity.items():
                if state.get(field) != value:
                    raise ValueError(f"managed PackVM {field} changed")
            payload = self._current_instance_payload(limactl)
            if state.get("instance_config_hash") != stable_lima_config_hash(
                self._instance, payload
            ):
                raise ValueError("managed PackVM config changed")
            is_running = str(payload.get("status") or "").casefold() == "running"
            if require_guest and not is_running:
                raise ValueError("managed PackVM instance is not running")
            if is_running:
                if state.get("instance_machine_id") != self._guest_machine_id(limactl):
                    raise ValueError("managed PackVM instance identity changed")
                if state.get("guest_runner_digest") != self._guest_runner_digest(limactl):
                    raise ValueError("managed PackVM guest supervisor changed")
                self._verify_guest_doctor(limactl)
            return limactl
        except PackVMForeignInstanceError:
            raise
        except (OSError, ValueError) as exc:
            raise PackVMForeignInstanceError(
                "PackVM current instance does not match authenticated state; "
                "reconciliation is required"
            ) from exc

    def _verify_exact_recovery_instance(self, recovery: Mapping[str, Any]) -> str:
        """Bind orphan deletion to authenticated recovery and directory evidence."""

        try:
            self._verify_recovery_environment(recovery)
            limactl = self._require_command()
            identity = self._instance_directory_identity()
            for field, value in identity.items():
                if recovery.get(field) != value:
                    raise ValueError(f"PackVM recovery {field} changed")
            payload = self._current_instance_payload(limactl)
            config_hash = recovery.get("instance_config_hash")
            if not isinstance(config_hash, str) or config_hash != stable_lima_config_hash(
                self._instance, payload
            ):
                raise ValueError("PackVM recovery instance config changed")
            return limactl
        except PackVMForeignInstanceError as exc:
            raise PackVMOrphanReconciliationRequired(str(exc)) from exc
        except (OSError, ValueError) as exc:
            raise PackVMOrphanReconciliationRequired(
                "PackVM orphan identity is incomplete or changed; reconciliation is required"
            ) from exc

    def _bind_recovery_instance(self, recovery: Mapping[str, Any]) -> dict[str, Any]:
        """Authenticate the created instance identity before any recovery mutation."""

        limactl = self._require_command()
        payload = self._current_instance_payload(limactl)
        updated = {
            **recovery,
            **self._instance_directory_identity(),
            "instance_config_hash": stable_lima_config_hash(self._instance, payload),
            "phase": "instance_bound",
            "updated_unix": int(time.time()),
        }
        updated.pop("authentication", None)
        updated["authentication"] = self._sign_recovery(updated)
        _atomic_private_json(self.recovery_path, updated)
        return updated

    def _write_recovery_phase(
        self,
        recovery: Mapping[str, Any],
        phase: str,
    ) -> None:
        updated = {**recovery, "phase": phase, "updated_unix": int(time.time())}
        updated.pop("authentication", None)
        updated["authentication"] = self._sign_recovery(updated)
        _atomic_private_json(self.recovery_path, updated)

    def _reconcile_failed_provision(
        self,
        limactl: str | None,
        recovery: Mapping[str, Any],
    ) -> str:
        """Best-effort delete an instance created by a failed Lima start."""

        if limactl is None or not self._instance_exists(limactl):
            self.recovery_path.unlink(missing_ok=True)
            return "missing"
        try:
            self._verify_recovery_environment(recovery)
            bound_recovery = self._bind_recovery_instance(recovery)
            limactl = self._verify_exact_recovery_instance(bound_recovery)
            self._checked_call(
                (limactl, "stop", "--force", self._instance),
                timeout=60,
                stage="reconcile_stop",
            )
            limactl = self._verify_exact_recovery_instance(bound_recovery)
            self._checked_call(
                (limactl, "delete", "--force", self._instance),
                timeout=120,
                stage="reconcile_delete",
            )
        except Exception:
            current_recovery = bound_recovery if "bound_recovery" in locals() else recovery
            self._write_recovery_phase(current_recovery, "orphaned")
            return "orphaned"
        self.recovery_path.unlink(missing_ok=True)
        self._audit("failed_provision_reconciled", None)
        return "reconciled"

    def _resolve_command(self) -> str | None:
        if self._command_path is None and platform.system() != "Darwin":
            return None
        candidate = self._command_path or resolve_limactl_path()
        if candidate is None:
            return None
        path = Path(candidate)
        try:
            metadata = path.lstat()
        except OSError:
            return None
        if path.is_symlink():
            try:
                resolved = path.resolve(strict=True)
            except OSError:
                return None
            trusted_roots = (
                Path("/opt/homebrew/Cellar/lima"),
                Path("/usr/local/Cellar/lima"),
            )
            if path not in {
                Path("/opt/homebrew/bin/limactl"),
                Path("/usr/local/bin/limactl"),
            } or not any(resolved.is_relative_to(root) for root in trusted_roots):
                return None
            path = resolved
            try:
                metadata = path.lstat()
            except OSError:
                return None
        if not stat.S_ISREG(metadata.st_mode) or not os.access(path, os.X_OK):
            return None
        return str(path.resolve())

    def _launcher_reason(self, resolved: str | None) -> str | None:
        if resolved is not None:
            return None
        if self._command_path is None and platform.system() != "Darwin":
            return "Lima PackVM provisioning is available only on macOS"
        candidate = self._command_path or resolve_limactl_path()
        if candidate is None:
            return "limactl was not detected; install an approved pinned Lima launcher"
        return "limactl must be a regular executable or a trusted versioned Homebrew link"

    def _require_command(self) -> str:
        command = self._resolve_command()
        if command is None:
            raise ValueError("limactl is unavailable or is not a regular executable")
        return command

    def _instance_exists(self, limactl: str | None) -> bool:
        if limactl is None:
            return False
        result = self._call((limactl, "list", "--format", "{{.Name}}"), timeout=10)
        return result.returncode == 0 and self._instance in {
            line.strip() for line in _decode(result.stdout).splitlines()
        }

    def _install_guest_runner(self, limactl: str) -> None:
        script = _PACKVM_RUNNER.read_text(encoding="utf-8")
        self._checked_call(
            (
                limactl,
                "shell",
                self._instance,
                "--",
                "sudo",
                "install",
                "-o",
                "root",
                "-g",
                "root",
                "-m",
                "0755",
                "/dev/stdin",
                PACKVM_GUEST_RUNNER,
            ),
            input_text=script,
            timeout=30,
        )

    def _guest_machine_id(self, limactl: str) -> str:
        result = self._checked_call(
            (limactl, "shell", self._instance, "--", "cat", "/etc/machine-id"),
            timeout=10,
        )
        machine_id = _decode(result.stdout).strip()
        if len(machine_id) != 32 or any(char not in "0123456789abcdef" for char in machine_id):
            raise ValueError("managed PackVM machine identity is invalid")
        return machine_id

    def _guest_runner_digest(self, limactl: str) -> str:
        result = self._checked_call(
            (limactl, "shell", self._instance, "--", "sha256sum", PACKVM_GUEST_RUNNER),
            timeout=10,
        )
        value = _decode(result.stdout).split(maxsplit=1)[0].lower()
        if len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
            raise ValueError("managed PackVM guest supervisor digest is invalid")
        return f"sha256:{value}"

    def _verify_guest_doctor(self, limactl: str) -> None:
        result = self._checked_call(
            (limactl, "shell", self._instance, "--", PACKVM_GUEST_RUNNER),
            input_text='{"operation":"doctor"}',
            timeout=10,
        )
        response = json.loads(_decode(result.stdout))
        if (
            not isinstance(response, dict)
            or response.get("ok") is not True
            or response.get("protocol") != PACKVM_PROTOCOL
        ):
            raise ValueError("managed PackVM guest supervisor doctor failed")
        challenge = secrets.token_hex(32)
        invoked = self._checked_call(
            (limactl, "shell", self._instance, "--", PACKVM_GUEST_RUNNER),
            input_text=json.dumps(
                {
                    "operation": "invoke",
                    "contract_id": "io.tobkiri.packvm.attestation.v1",
                    "operation_id": "challenge",
                    "payload": {"challenge": challenge},
                },
                sort_keys=True,
                separators=(",", ":"),
            ),
            timeout=10,
        )
        invoke_response = json.loads(_decode(invoked.stdout))
        expected_digest = _sha256(challenge.encode())
        if (
            not isinstance(invoke_response, dict)
            or invoke_response.get("ok") is not True
            or invoke_response.get("protocol") != PACKVM_PROTOCOL
            or not isinstance(invoke_response.get("payload"), dict)
            or invoke_response["payload"].get("challenge_digest") != expected_digest
        ):
            raise ValueError("managed PackVM guest supervisor invoke challenge failed")

    def _call(
        self,
        command: Sequence[str],
        *,
        input_text: str | None = None,
        timeout: float,
        max_stdin_bytes: int = 1024 * 1024,
        inherited_fds: Sequence[int] = (),
    ) -> Any:
        if self._runner is not None:
            if inherited_fds:
                if input_text is None or input_text.count(PACKVM_IMAGE_FD_TOKEN) != 1:
                    raise ValueError("PackVM image descriptor token is invalid")
                materialized = input_text.replace(PACKVM_IMAGE_FD_TOKEN, str(inherited_fds[0]))
                return self._runner(command, materialized, timeout, inherited_fds)
            return self._runner(command, input_text, timeout)
        environment = self._lima_process_environment()
        argv = tuple(str(item) for item in command)
        cwd = Path.cwd().resolve()
        bounded_timeout = min(max(float(timeout), 1.0), 900.0)
        result = HostBoundedProcessRunner().run_local(
            argv=argv,
            cwd=cwd,
            stdin=input_text,
            timeout_seconds=bounded_timeout,
            environment=environment,
            policy=ProcessExecutionPolicy(
                allowed_executables=frozenset({argv[0]}),
                allowed_argv=(argv,),
                allowed_cwds=(cwd,),
                allowed_environment=LIMA_PROCESS_ENVIRONMENT_KEYS,
                max_stdin_bytes=max_stdin_bytes,
                max_stdout_bytes=MAX_LIMA_STATE_BYTES,
                max_stderr_bytes=MAX_LIMA_STATE_BYTES,
                max_timeout_seconds=bounded_timeout,
                allow_inherited_readonly_fds=bool(inherited_fds),
            ),
            inherited_fds=inherited_fds,
            inherited_fd_tokens=((PACKVM_IMAGE_FD_TOKEN,) if inherited_fds else ()),
        )
        return _LimaCallResult(
            returncode=result.exit_code if result.exit_code is not None else 1,
            stdout=result.stdout,
            stderr=result.stderr or result.transport_error or "",
            timed_out=result.timed_out,
        )

    def _checked_call(
        self,
        command: Sequence[str],
        *,
        timeout: float,
        input_text: str | None = None,
        max_stdin_bytes: int = 1024 * 1024,
        inherited_fds: Sequence[int] = (),
        stage: str | None = None,
        sensitive_values: Sequence[str] = (),
    ) -> Any:
        result = self._call(
            command,
            input_text=input_text,
            timeout=timeout,
            max_stdin_bytes=max_stdin_bytes,
            inherited_fds=inherited_fds,
        )
        operation_stage = stage or (str(command[1]) if len(command) > 1 else "execute")
        if bool(getattr(result, "timed_out", False)):
            raise PackVMProcessError(
                stage=operation_stage,
                kind="timeout",
                stderr=_safe_process_diagnostic(
                    _decode(getattr(result, "stderr", "")), sensitive_values
                ),
            )
        if result.returncode != 0:
            raise PackVMProcessError(
                stage=operation_stage,
                kind="exit",
                exit_code=int(result.returncode),
                stderr=_safe_process_diagnostic(_decode(result.stderr), sensitive_values),
            )
        return result

    def _sign_state(self, state: Mapping[str, Any]) -> str:
        key_path = self._state_dir / "packvm-lima-attestation.key"
        key = generate_or_load_signing_key(key_path)
        unsigned = {key: value for key, value in state.items() if key != "authentication"}
        return hmac.new(key, _canonical_json(unsigned), hashlib.sha256).hexdigest()

    def _verify_attested_host_binding(self, state: Mapping[str, Any]) -> None:
        """Recheck executable and dedicated Lima-root identity before mutation."""

        for field, value in self.recovery_identity().items():
            if state.get(field) != value:
                raise ValueError(f"managed PackVM {field} changed")

    def _load_authenticated_state(self) -> dict[str, Any]:
        try:
            raw = _read_private_file(self.state_path, MAX_LIMA_STATE_BYTES)
        except FileNotFoundError as exc:
            raise ValueError("managed PackVM has not completed explicit provisioning") from exc
        try:
            state = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError("PackVM attestation state is invalid") from exc
        if not isinstance(state, dict) or state.get("version") != PACKVM_ATTESTATION_VERSION:
            raise ValueError("PackVM attestation state is unsupported")
        if state.get("backend_id") != PACKVM_BACKEND_ID or state.get("instance") != self._instance:
            raise ValueError("PackVM attestation is bound to another runtime")
        authentication = str(state.get("authentication") or "")
        key = _read_private_file(self._state_dir / "packvm-lima-attestation.key", 64)
        unsigned = {key: value for key, value in state.items() if key != "authentication"}
        expected = hmac.new(key, _canonical_json(unsigned), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(authentication, expected):
            raise ValueError("PackVM attestation authentication failed")
        attested = dict(unsigned)
        attestation_digest = str(attested.pop("attestation_digest", ""))
        if attestation_digest != _canonical_digest(attested):
            raise ValueError("PackVM attestation digest failed")
        return state

    def _audit(
        self,
        event: str,
        attestation_digest: str | None,
        *,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        self._state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        record = {
            "event": event,
            "backend_id": PACKVM_BACKEND_ID,
            "instance": self._instance,
            "attestation_digest": attestation_digest,
            "timestamp_unix": int(time.time()),
            **({"details": dict(details)} if details else {}),
        }
        flags = os.O_APPEND | os.O_CREAT | os.O_WRONLY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(self.audit_path, flags, 0o600)
        try:
            metadata = os.fstat(descriptor)
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_mode & 0o077
                or (hasattr(os, "getuid") and metadata.st_uid != os.getuid())
            ):
                raise ValueError("PackVM audit history is unsafe")
            encoded = (json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n").encode()
            os.write(descriptor, encoded)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def _normalize_packvm_machine(value: str) -> str:
    machine = {"aarch64": "arm64", "x86_64": "amd64", "AMD64": "amd64"}.get(value, value.lower())
    if machine not in _PACKVM_IMAGES:
        raise ValueError(f"unsupported PackVM architecture: {machine}")
    return machine


def _sha256(value: bytes) -> str:
    return f"sha256:{hashlib.sha256(value).hexdigest()}"


def _set_descriptor_flags(descriptor: int, flags: int) -> None:
    """Set Darwin file flags on a pinned descriptor for legacy reconciliation."""

    function = ctypes.CDLL(None, use_errno=True).fchflags
    function.argtypes = (ctypes.c_int, ctypes.c_uint)
    function.restype = ctypes.c_int
    if function(descriptor, flags) != 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error))


def _darwin_stat_flags(metadata: os.stat_result) -> int:
    """Read Darwin-only stat flags without requiring them on Linux."""

    return int(getattr(metadata, "st_flags", 0))


def _format_gib(value: int) -> str:
    return f"{value / (1024**3):.2f} GiB"


def _safe_process_diagnostic(value: str | None, sensitive_values: Sequence[str] = ()) -> str | None:
    """Bound stderr and remove control characters and absolute host paths."""

    if not value:
        return None
    sanitized = "".join(char if char in "\n\t" or ord(char) >= 32 else "?" for char in value)
    for sensitive in sorted(set(sensitive_values), key=len, reverse=True):
        if sensitive:
            sanitized = sanitized.replace(sensitive, "<packvm-handoff-redacted>")
    sanitized = re.sub(
        r"http://127\.0\.0\.1:[0-9]+/packvm-image/[0-9a-f]{64}",
        "<packvm-handoff-redacted>",
        sanitized,
    )
    sanitized = re.sub(
        r"(?<=packvm-image/)[0-9a-f]{64}",
        "<packvm-handoff-redacted>",
        sanitized,
    )
    sanitized = re.sub(r"(?<![A-Za-z0-9_.-])/(?:[^\s:'\"]+/?)+", "<host-path>", sanitized)
    sanitized = sanitized.strip()[:1000]
    return sanitized or None


def _file_digest(path: Path) -> str:
    metadata = path.lstat()
    if path.is_symlink() or not stat.S_ISREG(metadata.st_mode):
        raise ValueError(f"attested file is not a regular file: {path.name}")
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return f"sha256:{digest.hexdigest()}"


def _stable_regular_file_digest(path: Path, expected_size: int) -> str:
    """Hash one unlinked regular file descriptor and reject concurrent changes."""

    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_size != expected_size
            or (hasattr(os, "getuid") and before.st_uid != os.getuid())
        ):
            raise ValueError("PackVM pinned image cache file is unsafe")
        digest = hashlib.sha256()
        with os.fdopen(descriptor, "rb", closefd=False) as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        after = os.fstat(descriptor)
        if (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
            before.st_ctime_ns,
        ) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        ):
            raise ValueError("PackVM pinned image cache changed during verification")
        return f"sha256:{digest.hexdigest()}"
    finally:
        os.close(descriptor)


def _canonical_json(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def _canonical_digest(value: object) -> str:
    return _sha256(_canonical_json(value))


def _constant_mapping_equal(left: Mapping[str, Any], right: Mapping[str, Any]) -> bool:
    """Compare canonical claim documents without field-by-field timing variance."""

    return hmac.compare_digest(_canonical_json(left), _canonical_json(right))


def _claim_binding_equal(left: Mapping[str, Any], right: Mapping[str, Any]) -> bool:
    """Compare the operation and authorization binding while ignoring a dead PID."""

    left_binding = {key: left.get(key) for key in ("version", "operation", "instance", "binding")}
    right_binding = {key: right.get(key) for key in ("version", "operation", "instance", "binding")}
    return _constant_mapping_equal(left_binding, right_binding)


def _process_is_alive(value: object) -> bool:
    """Return whether a positive local PID still denotes a live process."""

    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return False
    if os.name == "nt":
        ctypes = importlib.import_module("ctypes")
        process_query_limited_information = 0x1000
        still_active = 259
        kernel32 = ctypes.windll.kernel32
        open_process = kernel32.OpenProcess
        open_process.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
        open_process.restype = ctypes.c_void_p
        get_exit_code = kernel32.GetExitCodeProcess
        get_exit_code.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong)]
        get_exit_code.restype = ctypes.c_int
        close_handle = kernel32.CloseHandle
        close_handle.argtypes = [ctypes.c_void_p]
        close_handle.restype = ctypes.c_int
        handle = open_process(
            process_query_limited_information,
            False,
            value,
        )
        if not handle:
            return ctypes.get_last_error() == 5  # ERROR_ACCESS_DENIED is fail-closed.
        exit_code = ctypes.c_ulong()
        try:
            if not get_exit_code(
                handle,
                ctypes.byref(exit_code),
            ):
                return True
            return int(exit_code.value) == still_active
        finally:
            close_handle(handle)
    try:
        os.kill(value, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _provision_claim_binding(proof: Mapping[str, Any]) -> dict[str, str]:
    """Project the exact durable owner of one provision ceremony."""

    fields = ("session_digest", "plan_digest", "ceremony_nonce_digest")
    binding: dict[str, str] = {}
    for field in fields:
        value = proof.get(field)
        if not isinstance(value, str):
            raise ValueError("PackVM provision recovery proof is incomplete")
        binding[field] = value
    return binding


def _read_private_file(path: Path, maximum: int) -> bytes:
    metadata = path.lstat()
    if path.is_symlink() or not stat.S_ISREG(metadata.st_mode) or metadata.st_size > maximum:
        raise ValueError(f"unsafe PackVM state file: {path.name}")
    if os.name == "posix" and metadata.st_mode & 0o077:
        raise ValueError(f"PackVM state permissions are too broad: {path.name}")
    if hasattr(os, "getuid") and metadata.st_uid != os.getuid():
        raise ValueError(f"PackVM state owner changed: {path.name}")
    return path.read_bytes()


def _atomic_private_bytes(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except OSError:
            pass


def _atomic_private_json(path: Path, payload: Mapping[str, Any]) -> None:
    _atomic_private_bytes(path, _canonical_json(payload) + b"\n")
