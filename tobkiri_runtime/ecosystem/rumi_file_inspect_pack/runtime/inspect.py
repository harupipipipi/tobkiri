"""Read-only workspace-jailed file inspection service."""

from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import stat
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Callable, Mapping

from core_runtime.host_provider_backend_v4 import (
    CapturedHostProviderV4,
    HostProviderCaptureContextV4,
    HostProviderContributionV4,
    HostProviderInvocationContextV4,
)
from tobkiri_protocol.canonical import canonical_digest

PACK_ID = "rumi_file_inspect_pack"
FUNCTION_ID = "rumi_file_inspect_pack.file-inspect.service"
CONTRACT_ID = "tobkiri.service.file.inspect.v1"
CONTRACT_VERSION = "1.0.0"
WORKSPACE = "tobkiri.resource.workspace.v1"
WORKSPACE_OPERATION = "rumi_workspace_mount_pack.workspace-resource"
OPERATIONS = frozenset(
    {
        "rumi_file_inspect_pack.file-inspect",
        "rumi_file_inspect_pack.file-inspect.for-media",
    }
)
_MAX_READ_BYTES = 4 * 1024 * 1024
_MAX_RESULTS = 10_000
_PROTECTED_PARTS = frozenset({".git", ".rumi_snapshots"})
_SECRET_PARTS = frozenset({
    ".aws",
    ".azure",
    ".docker",
    ".gnupg",
    ".kube",
    ".ssh",
    "secrets",
})
_SECRET_NAMES = frozenset({
    ".dockercfg",
    ".git-credentials",
    ".npmrc",
    ".pypirc",
    ".netrc",
    "credentials",
    "credentials.json",
    "id_dsa",
    "id_ecdsa",
    "id_ed25519",
    "id_rsa",
    "kubeconfig",
    "token",
    "tokens.json",
})
_SECRET_SUFFIXES = (".key", ".pem", ".p12", ".pfx", ".crt")
_SAFE_ENV_SUFFIXES = (".example", ".sample", ".template")


class FileInspectService:
    """Inspect files under an exact selected workspace mount."""

    def __init__(
        self,
        client: Any,
        *,
        guard: Callable[[], None] | None = None,
    ) -> None:
        self.client = client
        self._guard = guard or (lambda: None)

    def invoke(self, name: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        """Dispatch one read-only file operation."""
        self._guard()
        _check_lifecycle(payload)
        root, root_fd, binding = self._workspace(payload)
        try:
            if name == "read":
                result = self._read(root, payload, root_fd=root_fd)
            elif name == "stat":
                result = self._stat(payload, root_fd=root_fd)
            elif name == "list":
                result = self._list(root, payload, root_fd=root_fd)
            elif name == "search":
                result = self._search(payload, root_fd=root_fd)
            else:
                raise ValueError(f"unknown file inspect operation: {name}")
            self._validate_workspace_still_current(
                root,
                root_fd,
                binding,
                payload,
            )
            return result
        finally:
            os.close(root_fd)

    def _workspace(
        self,
        payload: Mapping[str, Any],
    ) -> tuple[Path, int, dict[str, Any]]:
        workspace_id = str(payload.get("workspace_id") or "").strip()
        if not workspace_id:
            raise ValueError("workspace_id is required")
        if payload.get("require_selected"):
            if self._selected_workspace_id(payload) != workspace_id:
                raise PermissionError(
                    "workspace is not the selected Host binding"
                )
        self._guard()
        mount = self.client.invoke(
            WORKSPACE,
            WORKSPACE_OPERATION,
            {
                "operation": "get",
                "profile_id": _profile(payload),
                "workspace_id": workspace_id,
            },
        )
        self._guard()
        if not isinstance(mount, Mapping):
            raise KeyError("workspace mount is unknown")
        root = Path(str(mount.get("root_path") or "")).resolve(strict=True)
        if not root.is_dir():
            raise PermissionError("workspace root is unavailable")
        root_fd = os.open(
            root,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
        )
        binding = dict(payload.get("_workspace_binding") or {})
        try:
            self._validate_mount_binding(
                workspace_id,
                mount,
                root,
                root_fd,
                binding,
            )
        except Exception:
            os.close(root_fd)
            raise
        if (
            payload.get("require_selected")
            and self._selected_workspace_id(payload) != workspace_id
        ):
            os.close(root_fd)
            raise PermissionError(
                "workspace selection changed during Host binding"
            )
        return root, root_fd, binding

    @staticmethod
    def _validate_mount_binding(
        workspace_id: str,
        mount: Mapping[str, Any],
        root: Path,
        root_fd: int,
        binding: Mapping[str, Any],
    ) -> None:
        if str(mount.get("workspace_id") or workspace_id) != workspace_id:
            raise PermissionError("workspace mount identity changed")
        reported_root = Path(str(mount.get("root_path") or "")).resolve(
            strict=True
        )
        if reported_root != root:
            raise PermissionError("workspace mount root changed")
        if str(binding.get("workspace_id") or "") != workspace_id:
            raise PermissionError("Host workspace binding is required")
        if str(binding.get("access") or "") != "read_only":
            raise PermissionError("Host workspace binding must be read_only")
        root_stat = os.fstat(root_fd)
        actual = {
            "workspace_id": workspace_id,
            "access": "read_only",
            "mount_revision": int(mount.get("mount_revision") or 0),
            "canonical_root": str(root),
            "root_st_dev": int(root_stat.st_dev),
            "root_st_ino": int(root_stat.st_ino),
        }
        actual_identity = hashlib.sha256(
            json.dumps(
                actual,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        for key, value in actual.items():
            if binding.get(key) != value:
                raise PermissionError(
                    f"workspace mount binding changed: {key}"
                )
        if str(binding.get("root_identity") or "") != actual_identity:
            raise PermissionError("workspace root identity changed")

    @staticmethod
    def _validate_root_binding(
        root: Path,
        root_fd: int,
        binding: Mapping[str, Any],
    ) -> None:
        opened = os.fstat(root_fd)
        current = root.stat()
        if (
            int(opened.st_dev) != int(binding.get("root_st_dev") or -1)
            or int(opened.st_ino) != int(binding.get("root_st_ino") or -1)
            or current.st_dev != opened.st_dev
            or current.st_ino != opened.st_ino
        ):
            raise PermissionError("workspace root changed during inspection")

    def _validate_workspace_still_current(
        self,
        root: Path,
        root_fd: int,
        binding: Mapping[str, Any],
        payload: Mapping[str, Any],
    ) -> None:
        """Reconfirm the canonical Host mount after descriptor-bound I/O."""
        workspace_id = str(payload["workspace_id"])
        self._guard()
        mount = self.client.invoke(
            WORKSPACE,
            WORKSPACE_OPERATION,
            {
                "operation": "get",
                "profile_id": _profile(payload),
                "workspace_id": workspace_id,
            },
        )
        self._guard()
        if not isinstance(mount, Mapping):
            raise PermissionError("workspace mount changed during inspection")
        self._validate_mount_binding(
            workspace_id,
            mount,
            root,
            root_fd,
            binding,
        )
        self._validate_root_binding(root, root_fd, binding)
        if (
            payload.get("require_selected")
            and self._selected_workspace_id(payload) != workspace_id
        ):
            raise PermissionError("workspace selection changed during inspection")

    def _selected_workspace_id(
        self,
        payload: Mapping[str, Any],
    ) -> str:
        self._guard()
        snapshot = self.client.invoke(
            WORKSPACE,
            WORKSPACE_OPERATION,
            {"operation": "list", "profile_id": _profile(payload)},
        )
        self._guard()
        return (
            str(snapshot.get("selected_workspace_id") or "").strip()
            if isinstance(snapshot, Mapping)
            else ""
        )

    def _read(
        self,
        root: Path,
        payload: Mapping[str, Any],
        *,
        root_fd: int,
    ) -> dict[str, Any]:
        relative = _safe_relative(payload.get("path"))
        max_bytes = max(
            1,
            min(
                _MAX_READ_BYTES,
                int(payload.get("max_bytes") or _MAX_READ_BYTES),
            ),
        )
        content, size = _read_text_no_follow(
            root,
            relative,
            encoding=str(payload.get("encoding") or "utf-8"),
            max_bytes=max_bytes,
            root_fd=root_fd,
        )
        start = max(1, int(payload.get("start_line") or 1))
        end_value = payload.get("end_line")
        lines = content.splitlines(keepends=True)
        end = len(lines) if end_value is None else max(start, int(end_value))
        selected = "".join(lines[start - 1 : end])
        return {
            "workspace_id": str(payload["workspace_id"]),
            "path": relative.as_posix(),
            "content": selected,
            "size": size,
            "encoding": str(payload.get("encoding") or "utf-8"),
            "start_line": start,
            "end_line": min(end, len(lines)),
            "total_lines": len(lines),
            "read_only": True,
        }

    def _stat(
        self,
        payload: Mapping[str, Any],
        *,
        root_fd: int,
    ) -> dict[str, Any]:
        relative = _safe_relative(payload.get("path"))
        descriptor = _open_relative_fd(root_fd, relative)
        try:
            path_stat = os.fstat(descriptor)
        finally:
            os.close(descriptor)
        return {
            "workspace_id": str(payload["workspace_id"]),
            "path": relative.as_posix(),
            "is_file": stat.S_ISREG(path_stat.st_mode),
            "is_dir": stat.S_ISDIR(path_stat.st_mode),
            "size": path_stat.st_size,
            "modified_ns": path_stat.st_mtime_ns,
            "read_only": True,
        }

    def _list(
        self,
        root: Path,
        payload: Mapping[str, Any],
        *,
        root_fd: int,
    ) -> dict[str, Any]:
        directory = _safe_relative(payload.get("directory") or ".")
        recursive = bool(payload.get("recursive", False))
        if payload.get("tracked_only"):
            relative_paths = _git_tracked_paths(
                root,
                root_fd,
                directory,
                recursive=recursive,
                deadline_epoch_ms=int(payload.get("_deadline_epoch_ms") or 0),
            )
        else:
            relative_paths = _descriptor_relative_entries(
                root_fd,
                directory,
                recursive=recursive,
                guard=self._guard,
            )
        items: list[dict[str, Any]] = []
        for relative in relative_paths:
            self._guard()
            try:
                _deny_restricted_path(relative)
            except PermissionError:
                continue
            descriptor: int | None = None
            try:
                descriptor = _open_relative_fd(root_fd, relative)
                path_stat = os.fstat(descriptor)
            except OSError:
                continue
            finally:
                if descriptor is not None:
                    os.close(descriptor)
            items.append(
                {
                    "path": relative.as_posix(),
                    "name": relative.name,
                    "is_file": stat.S_ISREG(path_stat.st_mode),
                    "is_dir": stat.S_ISDIR(path_stat.st_mode),
                    "size": path_stat.st_size,
                }
            )
            if len(items) >= _MAX_RESULTS:
                break
        items.sort(key=lambda item: item["path"])
        return {"workspace_id": str(payload["workspace_id"]), "items": items}

    def _search(
        self,
        payload: Mapping[str, Any],
        *,
        root_fd: int,
    ) -> dict[str, Any]:
        pattern = str(payload.get("pattern") or "").strip()
        if not pattern:
            raise ValueError("file search pattern is required")
        directory = _safe_relative(payload.get("directory") or ".")
        matches = []
        for candidate in _descriptor_relative_entries(
            root_fd,
            directory,
            recursive=True,
            include_directories=True,
            guard=self._guard,
        ):
            self._guard()
            relative = candidate.as_posix()
            try:
                _deny_restricted_path(Path(relative))
            except PermissionError:
                continue
            if fnmatch.fnmatch(relative, pattern) or fnmatch.fnmatch(
                candidate.name,
                pattern,
            ):
                matches.append(relative)
            if len(matches) >= _MAX_RESULTS:
                break
        return {
            "workspace_id": str(payload["workspace_id"]),
            "pattern": pattern,
            "matches": sorted(matches),
        }


def create_file_inspect_operation(
    client: Any,
) -> Callable[[str, Mapping[str, Any]], Any]:
    """Create read-only file operations."""
    service = FileInspectService(client)
    return service.invoke


class FileInspectHostFactoryV4:
    """Bind file inspection to exact verified Host dispatch edges."""

    function_id = FUNCTION_ID

    def capture(
        self,
        context: HostProviderCaptureContextV4,
    ) -> CapturedHostProviderV4:
        """Capture the selected subset of the Function's two operations."""
        bindings = context.provider_bindings
        if not bindings:
            raise PermissionError("file inspect bindings are unavailable")
        contribution_fields: list[dict[str, Any]] = []
        captured: dict[str, tuple[str, str]] = {}
        for binding in bindings:
            operation_id = binding.operation.operation_id
            key = (
                binding.operation.contract_id,
                operation_id,
                binding.principal_ref.value,
            )
            if (
                binding.function.function_id != self.function_id
                or binding.operation.contract_id != CONTRACT_ID
                or binding.operation.contract_version != CONTRACT_VERSION
                or operation_id not in OPERATIONS
                or operation_id in captured
                or key not in context.domain_ids
            ):
                raise PermissionError("file inspect bindings are incomplete")
            captured[operation_id] = (
                binding.principal_ref.value,
                context.domain_ids[key],
            )
            contribution_fields.append(
                {
                    "contract_id": CONTRACT_ID,
                    "contract_version": CONTRACT_VERSION,
                    "operation_id": operation_id,
                    "principal_id": binding.principal_ref.value,
                    "artifact_digest": binding.artifact.digest,
                    "implementation_digest": (
                        binding.function.implementation_digest
                    ),
                    "domain_id": context.domain_ids[key],
                }
            )

        closed = threading.Event()
        activation_id = str(context.activation.get("activation_id") or "")
        activation_digest = canonical_digest(dict(context.activation))

        def invoke(
            operation_id: str,
            payload: Mapping[str, Any],
            invocation: HostProviderInvocationContextV4,
        ) -> Mapping[str, Any]:
            invocation.assert_current()
            if closed.is_set() or operation_id not in captured:
                raise PermissionError("file inspect capture is unavailable")
            principal_id, domain_id = captured[operation_id]
            envelope = invocation.envelope
            request = envelope.context
            if (
                envelope.contract_id != CONTRACT_ID
                or envelope.contract_version != CONTRACT_VERSION
                or envelope.operation_id != operation_id
                or envelope.target_principal.value != principal_id
                or envelope.target_domain.value != domain_id
                or dict(envelope.payload) != dict(payload)
                or request.profile_id != context.profile_id
                or request.activation_id != activation_id
                or request.activation_digest != activation_digest
                or request.plan_digest != context.plan_digest
                or request.security_epoch != context.security_epoch
            ):
                raise PermissionError("file inspect invocation changed")
            if payload.get("profile_id") != context.profile_id:
                raise PermissionError("file inspect profile changed")
            name = payload.get("name")
            if name not in {"read", "stat", "list", "search"}:
                raise ValueError("file inspect operation is invalid")
            client = invocation.contract_client(
                allowed_contract_ids=frozenset({WORKSPACE}),
                consumer_pack_id=PACK_ID,
                include_credentials=False,
            )
            result = FileInspectService(
                client,
                guard=invocation.assert_current,
            ).invoke(str(name), payload)
            invocation.assert_current()
            if closed.is_set():
                raise PermissionError("file inspect capture is unavailable")
            return result

        contributions = tuple(
            HostProviderContributionV4(**fields, invoke=invoke)
            for fields in contribution_fields
        )
        return CapturedHostProviderV4(contributions, closed.set)


HOST_PROVIDER_FACTORY = FileInspectHostFactoryV4()


def _jailed(root: Path, value: Any, *, must_exist: bool) -> Path:
    raw = Path(str(value or "").strip() or ".")
    if raw.is_absolute() or ".." in raw.parts:
        raise PermissionError("absolute or traversing paths are not accepted")
    _deny_restricted_path(raw)
    candidate = root / raw
    if must_exist:
        resolved = candidate.resolve(strict=True)
    else:
        parent = candidate.parent.resolve(strict=True)
        resolved = parent / candidate.name
    if not _within(root, resolved):
        raise PermissionError("path escapes the workspace mount")
    return resolved


def _safe_relative(value: Any) -> Path:
    raw = Path(str(value or "").strip() or ".")
    if raw.is_absolute() or ".." in raw.parts:
        raise PermissionError("absolute or traversing paths are not accepted")
    _deny_restricted_path(raw)
    return raw


def _deny_restricted_path(path: Path) -> None:
    parts = tuple(
        part.casefold()
        for part in path.parts
        if part not in {"", "."}
    )
    if any(part in _PROTECTED_PARTS for part in parts):
        raise PermissionError("protected workspace paths are not readable")
    if any(part in _SECRET_PARTS for part in parts):
        raise PermissionError("secret workspace directories are not readable")
    if not parts:
        return
    name = parts[-1]
    is_env = name == ".env" or (
        name.startswith(".env.")
        and not name.endswith(_SAFE_ENV_SUFFIXES)
    )
    if (
        is_env
        or name in _SECRET_NAMES
        or name.endswith(_SECRET_SUFFIXES)
    ):
        raise PermissionError("secret workspace files are not readable")


def _within(root: Path, candidate: Path) -> bool:
    try:
        candidate.relative_to(root)
        return True
    except ValueError:
        return False


def _require_descriptor_support() -> None:
    if (
        not getattr(os, "O_NOFOLLOW", 0)
        or os.open not in os.supports_dir_fd
        or os.scandir not in os.supports_fd
    ):
        raise PermissionError(
            "secure descriptor-relative file inspection is unavailable"
        )


def _open_relative_fd(root_fd: int, relative: Path) -> int:
    _require_descriptor_support()
    if relative.is_absolute() or ".." in relative.parts:
        raise PermissionError("unsafe workspace path")
    parts = tuple(part for part in relative.parts if part not in {"", "."})
    current_fd = os.dup(root_fd)
    try:
        for index, part in enumerate(parts):
            is_parent = index < len(parts) - 1
            flags = os.O_RDONLY | os.O_NOFOLLOW
            if is_parent:
                flags |= getattr(os, "O_DIRECTORY", 0)
            else:
                flags |= getattr(os, "O_NONBLOCK", 0)
            next_fd = os.open(part, flags, dir_fd=current_fd)
            os.close(current_fd)
            current_fd = next_fd
        return current_fd
    except Exception:
        os.close(current_fd)
        raise


def _open_directory_fd(root_fd: int, relative: Path) -> int:
    descriptor = _open_relative_fd(root_fd, relative)
    if not stat.S_ISDIR(os.fstat(descriptor).st_mode):
        os.close(descriptor)
        raise NotADirectoryError("workspace path is not a directory")
    return descriptor


def _descriptor_relative_entries(
    root_fd: int,
    directory: Path,
    *,
    recursive: bool,
    include_directories: bool = False,
    guard: Callable[[], None] | None = None,
) -> list[Path]:
    guard = guard or (lambda: None)
    directory_fd = _open_directory_fd(root_fd, directory)
    result: list[Path] = []
    pending = [(directory, directory_fd)]
    excluded = {
        ".git",
        ".venv",
        "build",
        "dist",
        "node_modules",
        "target",
        "vendor",
    }
    try:
        while pending and len(result) < _MAX_RESULTS:
            relative_directory, current_fd = pending.pop()
            try:
                guard()
                try:
                    with os.scandir(current_fd) as entries:
                        names = sorted(entry.name for entry in entries)
                except OSError:
                    continue
                for name in names:
                    guard()
                    if name in excluded:
                        continue
                    relative = relative_directory / name
                    try:
                        _deny_restricted_path(relative)
                    except PermissionError:
                        continue
                    try:
                        child_fd = _open_relative_fd(root_fd, relative)
                        child_stat = os.fstat(child_fd)
                    except OSError:
                        continue
                    if stat.S_ISDIR(child_stat.st_mode):
                        if include_directories or not recursive:
                            result.append(relative)
                        if recursive:
                            pending.append((relative, child_fd))
                        else:
                            os.close(child_fd)
                    else:
                        result.append(relative)
                        os.close(child_fd)
                    if len(result) >= _MAX_RESULTS:
                        break
            finally:
                os.close(current_fd)
            if not recursive:
                break
    finally:
        for _, descriptor in pending:
            try:
                os.close(descriptor)
            except OSError:
                pass
    return sorted(result, key=lambda item: item.as_posix())[:_MAX_RESULTS]


def _git_tracked_paths(
    root: Path,
    root_fd: int,
    directory: Path,
    *,
    recursive: bool,
    deadline_epoch_ms: int = 0,
) -> list[Path]:
    _require_descriptor_support()
    timeout = 15.0
    if deadline_epoch_ms:
        timeout = max(
            0.001,
            min(
                timeout,
                (deadline_epoch_ms - int(time.time() * 1000)) / 1000,
            ),
        )
        if timeout <= 0.001:
            raise TimeoutError("file inspection deadline exceeded")
    try:
        completed = subprocess.run(
            ["git", "-C", str(root), "ls-files", "-z", "--cached"],
            check=True,
            capture_output=True,
            timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    prefix = "" if directory == Path(".") else directory.as_posix().rstrip("/") + "/"
    result: list[Path] = []
    for raw in completed.stdout.split(b"\0"):
        if not raw:
            continue
        try:
            relative = Path(raw.decode("utf-8"))
        except UnicodeDecodeError:
            continue
        if relative.is_absolute() or ".." in relative.parts:
            continue
        value = relative.as_posix()
        if prefix and not value.startswith(prefix):
            continue
        if not recursive and "/" in value[len(prefix) :]:
            continue
        # Git's output is only an untrusted candidate set. Each result is
        # authorized again through the captured root descriptor, so replacing
        # the root pathname cannot expose content or entries outside that root.
        descriptor: int | None = None
        try:
            _deny_restricted_path(relative)
            descriptor = _open_relative_fd(root_fd, relative)
            path_stat = os.fstat(descriptor)
        except (OSError, PermissionError):
            continue
        finally:
            if descriptor is not None:
                os.close(descriptor)
        if stat.S_ISREG(path_stat.st_mode):
            result.append(relative)
        if len(result) >= _MAX_RESULTS:
            break
    return sorted(result, key=lambda item: item.as_posix())


def _read_text_no_follow(
    root: Path,
    relative: Path,
    *,
    encoding: str,
    max_bytes: int,
    root_fd: int | None = None,
) -> tuple[str, int]:
    if relative.is_absolute() or ".." in relative.parts:
        raise PermissionError("unsafe workspace path")
    close_root = root_fd is None
    if root_fd is None:
        root_fd = os.open(
            root,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
        )
    try:
        file_fd = _open_relative_fd(root_fd, relative)
        try:
            file_stat = os.fstat(file_fd)
            if not stat.S_ISREG(file_stat.st_mode):
                raise PermissionError("workspace path is not a regular file")
            size = file_stat.st_size
            if size > max_bytes:
                raise ValueError("file exceeds requested read budget")
            content = os.read(file_fd, max_bytes + 1)
            if len(content) > max_bytes:
                raise ValueError("file exceeds requested read budget")
            return content.decode(encoding), size
        finally:
            os.close(file_fd)
    finally:
        if close_root:
            os.close(root_fd)


def _profile(payload: Mapping[str, Any]) -> str:
    return str(payload.get("profile_id") or "default")


def _check_lifecycle(payload: Mapping[str, Any]) -> None:
    deadline = int(payload.get("_deadline_epoch_ms") or 0)
    if deadline and int(time.time() * 1000) >= deadline:
        raise TimeoutError("file inspection deadline exceeded")
    token = payload.get("_cancellation_token")
    if callable(token) and bool(token()):
        raise TimeoutError("file inspection cancelled")
    for name in ("is_cancelled", "is_set", "cancelled"):
        value = getattr(token, name, None) if token is not None else None
        if callable(value) and bool(value()):
            raise TimeoutError("file inspection cancelled")
        if value is not None and not callable(value) and bool(value):
            raise TimeoutError("file inspection cancelled")
