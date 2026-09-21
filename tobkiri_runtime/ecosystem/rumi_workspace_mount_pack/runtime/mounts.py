"""Profile-bound authoritative workspace mount metadata."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import time
from pathlib import Path
from typing import Any, Callable, Mapping

from core_runtime.host_provider_backend_v4 import (
    CapturedHostProviderV4,
    HostProviderCaptureContextV4,
    HostProviderContributionV4,
    HostProviderInvocationContextV4,
)
from core_runtime.paths import USER_DATA_DIR
from core_runtime.profile_workspace import validate_profile_id
from core_runtime.runtime_locks import NamedLock
from tobkiri_host.workspace_mutation import (
    WorkspaceMutationBinding,
    open_directory_nofollow,
)

AUTHORITY = "rumi.service.host.authorize.v1"
VERSION = "rumi.workspace-mounts.v1"
_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_RESOURCE_FUNCTION_ID = "rumi_workspace_mount_pack.workspace-mount.resource"
_ACTION_FUNCTION_ID = "rumi_workspace_mount_pack.workspace-mount.manage"
_RESOURCE_SERVICE_OPERATIONS = frozenset({"list", "get"})
_ACTION_SERVICE_OPERATIONS = frozenset({"mount", "unmount", "update", "select", "trust"})


class WorkspaceConflict(RuntimeError):
    """Raised for stale workspace mount mutations."""


class WorkspaceMountStore:
    """Own canonical workspace mount metadata for one profile."""

    def __init__(self, profile_id: str, *, user_data_root: Path | None = None) -> None:
        self.profile_id = validate_profile_id(profile_id)
        self.root = (
            Path(user_data_root or USER_DATA_DIR)
            / "packs"
            / "rumi_workspace_mount_pack"
            / "profiles"
            / self.profile_id
        )
        self.path = self.root / "mounts.json"
        self.lock_root = self.root / "locks"

    def snapshot(self) -> dict[str, Any]:
        """Return all canonical mounts without filesystem probing."""
        state = self._read()
        return {
            "version": VERSION,
            "profile_id": self.profile_id,
            "revision": state["revision"],
            "selected_workspace_id": state["selected_workspace_id"],
            "mounts": [state["mounts"][key] for key in sorted(state["mounts"])],
        }

    def get(self, workspace_id: str) -> dict[str, Any] | None:
        """Return one exact workspace mount."""
        value = self._read()["mounts"].get(_identifier(workspace_id))
        return _copy(value) if isinstance(value, Mapping) else None

    def mount(
        self,
        workspace_id: str,
        root_path: str,
        *,
        expected_revision: int,
        metadata: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Register an existing canonical directory at an exact revision."""
        workspace_id = _identifier(workspace_id)
        canonical = Path(root_path).expanduser().resolve(strict=True)
        if not canonical.is_dir():
            raise ValueError("workspace root is not a directory")
        with NamedLock(self.lock_root, "mounts"):
            state = self._read()
            _assert_revision(state, expected_revision)
            now = int(time.time() * 1000)
            current = state["mounts"].get(workspace_id)
            record = {
                "id": workspace_id,
                "root_path": str(canonical),
                "metadata": _copy(metadata or {}),
                "created_at": current.get("created_at") if current else now,
                "updated_at": now,
                "mount_revision": int(current.get("mount_revision") or 0) + 1 if current else 1,
            }
            state["mounts"][workspace_id] = record
            state["revision"] += 1
            self._write(state)
        return {"mount": _copy(record), "revision": state["revision"]}

    def unmount(self, workspace_id: str, *, expected_revision: int) -> dict[str, Any]:
        """Remove mount metadata without deleting workspace files."""
        workspace_id = _identifier(workspace_id)
        with NamedLock(self.lock_root, "mounts"):
            state = self._read()
            _assert_revision(state, expected_revision)
            if workspace_id not in state["mounts"]:
                raise KeyError("workspace mount is unknown")
            del state["mounts"][workspace_id]
            if state["selected_workspace_id"] == workspace_id:
                state["selected_workspace_id"] = None
            state["revision"] += 1
            self._write(state)
        return {"unmounted": workspace_id, "revision": state["revision"]}

    def update(
        self,
        workspace_id: str,
        *,
        expected_revision: int,
        root_path: str | None,
        metadata: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Update exact mount metadata without creating an implicit record."""

        workspace_id = _identifier(workspace_id)
        with NamedLock(self.lock_root, "mounts"):
            state = self._read()
            _assert_revision(state, expected_revision)
            current = state["mounts"].get(workspace_id)
            if current is None:
                raise KeyError("workspace mount is unknown")
            canonical = Path(current["root_path"])
            if root_path:
                canonical = Path(root_path).expanduser().resolve(strict=True)
                if not canonical.is_dir():
                    raise ValueError("workspace root is not a directory")
            current["root_path"] = str(canonical)
            current["metadata"] = {**current["metadata"], **_copy(metadata)}
            current["updated_at"] = int(time.time() * 1000)
            current["mount_revision"] = int(current["mount_revision"]) + 1
            state["revision"] += 1
            self._write(state)
        return {"mount": _copy(current), "revision": state["revision"]}

    def select(self, workspace_id: str, *, expected_revision: int) -> dict[str, Any]:
        """Select one existing mount for the profile."""

        workspace_id = _identifier(workspace_id)
        with NamedLock(self.lock_root, "mounts"):
            state = self._read()
            _assert_revision(state, expected_revision)
            if workspace_id not in state["mounts"]:
                raise KeyError("workspace mount is unknown")
            state["selected_workspace_id"] = workspace_id
            state["revision"] += 1
            self._write(state)
        return {
            "mount": _copy(state["mounts"][workspace_id]),
            "selected_workspace_id": workspace_id,
            "revision": state["revision"],
        }

    def _read(self) -> dict[str, Any]:
        if not self.path.exists():
            return {
                "version": VERSION,
                "profile_id": self.profile_id,
                "revision": 0,
                "selected_workspace_id": None,
                "mounts": {},
            }
        value = json.loads(self.path.read_text(encoding="utf-8"))
        if not isinstance(value, Mapping) or value.get("version") != VERSION:
            raise ValueError("workspace mount state is invalid")
        if value.get("profile_id") != self.profile_id:
            raise ValueError("workspace mount profile does not match")
        mounts = value.get("mounts")
        if not isinstance(mounts, Mapping):
            raise ValueError("workspace mount records are invalid")
        return {
            "version": VERSION,
            "profile_id": self.profile_id,
            "revision": max(0, int(value.get("revision") or 0)),
            "selected_workspace_id": value.get("selected_workspace_id"),
            "mounts": {str(key): _copy(item) for key, item in mounts.items()},
        }

    def _write(self, state: Mapping[str, Any]) -> None:
        _atomic_json(self.path, state)


def create_workspace_resource(client: Any) -> Callable[[str, Mapping[str, Any]], Any]:
    """Create workspace metadata read operations."""
    del client

    def operation(name: str, payload: Mapping[str, Any]) -> Any:
        store = WorkspaceMountStore(_profile(payload))
        if name == "list":
            return store.snapshot()
        if name == "get":
            return store.get(str(payload.get("workspace_id") or ""))
        raise ValueError(f"unknown workspace resource operation: {name}")

    return operation


def create_workspace_action(client: Any) -> Callable[[str, Mapping[str, Any]], Any]:
    """Create receipt-gated workspace mount mutations."""

    def operation(name: str, payload: Mapping[str, Any]) -> Any:
        return _execute_workspace_action(client, name, payload)

    return operation


def _execute_workspace_action(
    client: Any,
    name: str,
    payload: Mapping[str, Any],
    *,
    user_data_root: Path | None = None,
) -> Mapping[str, Any]:
    if name not in _ACTION_SERVICE_OPERATIONS:
        raise ValueError(f"unknown workspace action: {name}")
    arguments = _action_arguments(name, payload)
    _redeem(client, payload, f"workspace.{name}", arguments)
    store = WorkspaceMountStore(_profile(payload), user_data_root=user_data_root)
    expected = int(arguments["expected_revision"])
    if name == "mount":
        return store.mount(
            arguments["workspace_id"],
            arguments["root_path"],
            expected_revision=expected,
            metadata=arguments["metadata"],
        )
    if name == "unmount":
        return store.unmount(arguments["workspace_id"], expected_revision=expected)
    if name == "select":
        return store.select(arguments["workspace_id"], expected_revision=expected)
    metadata = {"trusted": True} if name == "trust" else arguments["metadata"]
    return store.update(
        arguments["workspace_id"],
        expected_revision=expected,
        root_path=arguments.get("root_path") or None,
        metadata=metadata,
    )


def capture_selected_workspace_binding(
    profile_id: str,
    *,
    user_data_root: Path | None = None,
) -> dict[str, object]:
    """Capture the selected root with immutable mount and filesystem identity."""

    store = WorkspaceMountStore(profile_id, user_data_root=user_data_root)
    snapshot = store.snapshot()
    workspace_id = str(snapshot.get("selected_workspace_id") or "").strip()
    if not workspace_id:
        raise ValueError("a Host-selected workspace is required")
    captured = capture_workspace_binding(
        profile_id,
        workspace_id,
        user_data_root=user_data_root,
    )
    binding: dict[str, object] = {
        "workspace_id": workspace_id,
        "access": "read_only",
        "mount_revision": captured.mount_revision,
        "canonical_root": str(captured.canonical_root),
        "root_st_dev": captured.root_st_dev,
        "root_st_ino": captured.root_st_ino,
    }
    binding["root_identity"] = hashlib.sha256(
        json.dumps(
            binding,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    return binding


def capture_workspace_binding(
    profile_id: str,
    workspace_id: str,
    *,
    user_data_root: Path | None = None,
) -> WorkspaceMutationBinding:
    """Capture one exact mount record and its nofollow root identity."""

    store = WorkspaceMountStore(profile_id, user_data_root=user_data_root)
    mount = store.get(_identifier(workspace_id))
    if not isinstance(mount, Mapping):
        raise ValueError("the requested workspace is unavailable")
    unresolved = Path(str(mount.get("root_path") or ""))
    if unresolved.is_symlink():
        raise PermissionError("the workspace root must not be a symlink")
    root = unresolved.resolve(strict=True)
    if not root.is_dir():
        raise PermissionError("the workspace root is unavailable")
    root_fd = open_directory_nofollow(root)
    try:
        root_stat = os.fstat(root_fd)
    finally:
        os.close(root_fd)
    return WorkspaceMutationBinding(
        profile_id=store.profile_id,
        workspace_id=workspace_id,
        mount_revision=int(mount.get("mount_revision") or 0),
        canonical_root=root,
        root_st_dev=int(root_stat.st_dev),
        root_st_ino=int(root_stat.st_ino),
    )


class WorkspaceResourceHostFactoryV4:
    """Capture exact workspace metadata reads and the mutation binding hook."""

    function_id = _RESOURCE_FUNCTION_ID

    def capture_workspace_binding_resolver(
        self,
        context: HostProviderCaptureContextV4,
    ) -> Callable[[str, str], WorkspaceMutationBinding]:
        """Return the Host-owned resolver for exact mounted workspace roots."""

        user_data_root = _capture_user_data_root(context)

        def resolve(profile_id: str, workspace_id: str) -> WorkspaceMutationBinding:
            if profile_id != context.profile_id:
                raise PermissionError("workspace binding profile changed")
            return capture_workspace_binding(
                profile_id,
                workspace_id,
                user_data_root=user_data_root,
            )

        return resolve

    def capture(
        self,
        context: HostProviderCaptureContextV4,
    ) -> CapturedHostProviderV4:
        """Bind list/get operations to exact resolved Function principals."""

        _validate_factory_context(context, self.function_id)
        user_data_root = _capture_user_data_root(context)

        def invoke(
            operation_id: str,
            payload: Mapping[str, Any],
            invocation: HostProviderInvocationContextV4,
        ) -> Mapping[str, Any]:
            _assert_invocation_profile(context, invocation)
            service_operation = _payload_operation(
                operation_id,
                payload,
                _RESOURCE_SERVICE_OPERATIONS,
            )
            request = dict(payload)
            request["profile_id"] = context.profile_id
            store = WorkspaceMountStore(
                context.profile_id,
                user_data_root=user_data_root,
            )
            if service_operation == "list":
                return store.snapshot()
            mount = store.get(str(request.get("workspace_id") or ""))
            if mount is None:
                raise KeyError("workspace mount is unknown")
            return mount

        return CapturedHostProviderV4(
            tuple(_contributions(context, invoke)),
            lambda: None,
        )


class WorkspaceActionHostFactoryV4:
    """Preserve the existing receipt-gated workspace mount action adapter."""

    function_id = _ACTION_FUNCTION_ID

    def capture(
        self,
        context: HostProviderCaptureContextV4,
    ) -> CapturedHostProviderV4:
        """Bind existing mount actions without changing their receipt semantics."""

        _validate_factory_context(context, self.function_id)
        user_data_root = _capture_user_data_root(context)

        def invoke(
            operation_id: str,
            payload: Mapping[str, Any],
            invocation: HostProviderInvocationContextV4,
        ) -> Mapping[str, Any]:
            _assert_invocation_profile(context, invocation)
            service_operation = _payload_operation(
                operation_id,
                payload,
                _ACTION_SERVICE_OPERATIONS,
            )
            client = invocation.contract_client(
                allowed_contract_ids=frozenset({AUTHORITY}),
                consumer_pack_id="rumi_workspace_mount_pack",
            )
            request = dict(payload)
            request["profile_id"] = context.profile_id
            return _execute_workspace_action(
                client,
                service_operation,
                request,
                user_data_root=user_data_root,
            )

        return CapturedHostProviderV4(
            tuple(_contributions(context, invoke)),
            lambda: None,
        )


def _capture_user_data_root(context: HostProviderCaptureContextV4) -> Path:
    root = Path(context.user_data_root or context.state_root)
    if not root.is_absolute():
        raise PermissionError("workspace Host state root is invalid")
    return root


def _validate_factory_context(
    context: HostProviderCaptureContextV4,
    function_id: str,
) -> None:
    if not context.provider_bindings or any(
        binding.function.function_id != function_id for binding in context.provider_bindings
    ):
        raise PermissionError("workspace Host Provider bindings are incomplete")


def _assert_invocation_profile(
    context: HostProviderCaptureContextV4,
    invocation: HostProviderInvocationContextV4,
) -> None:
    request = invocation.envelope.context
    if (
        request.profile_id != context.profile_id
        or request.plan_digest != context.plan_digest
        or request.security_epoch != context.security_epoch
    ):
        raise PermissionError("workspace Host Provider invocation binding changed")


def _payload_operation(
    operation_id: str,
    payload: Mapping[str, Any],
    allowed: frozenset[str],
) -> str:
    if operation_id not in {
        "rumi_workspace_mount_pack.workspace-resource",
        "rumi_workspace_mount_pack.workspace-mount",
    }:
        raise PermissionError("workspace Host Provider operation is unavailable")
    operation = str(payload.get("operation") or payload.get("action") or "")
    if operation not in allowed:
        raise ValueError("workspace service operation is invalid")
    return operation


def _contributions(
    context: HostProviderCaptureContextV4,
    invoke: Callable[
        [str, Mapping[str, Any], HostProviderInvocationContextV4],
        Mapping[str, Any],
    ],
) -> list[HostProviderContributionV4]:
    contributions: list[HostProviderContributionV4] = []
    for binding in context.provider_bindings:
        key = (
            binding.operation.contract_id,
            binding.operation.operation_id,
            binding.principal_ref.value,
        )
        domain_id = context.domain_ids.get(key)
        if domain_id is None:
            raise PermissionError("workspace Host Provider domain is unavailable")
        contributions.append(
            HostProviderContributionV4(
                contract_id=binding.operation.contract_id,
                contract_version=binding.operation.contract_version,
                operation_id=binding.operation.operation_id,
                principal_id=binding.principal_ref.value,
                artifact_digest=binding.artifact.digest,
                implementation_digest=binding.function.implementation_digest,
                domain_id=domain_id,
                invoke=invoke,
            )
        )
    return contributions


HOST_PROVIDER_FACTORY = {
    _RESOURCE_FUNCTION_ID: WorkspaceResourceHostFactoryV4(),
    _ACTION_FUNCTION_ID: WorkspaceActionHostFactoryV4(),
}


def _redeem(
    client: Any,
    payload: Mapping[str, Any],
    operation: str,
    arguments: Mapping[str, Any],
) -> None:
    result = client.invoke(
        AUTHORITY,
        "redeem",
        {
            "receipt": str(payload.get("authority_receipt") or ""),
            "service_pack_id": "rumi_workspace_mount_pack",
            "operation": operation,
            "authority": "workspace.mount.manage",
            "caller_id": str(payload.get("caller_id") or ""),
            "caller_pack_id": str(payload.get("caller_pack_id") or ""),
            "caller_function_id": str(payload.get("caller_function_id") or ""),
            "profile_id": _profile(payload),
            "workspace_id": str(payload.get("workspace_id") or ""),
            "session_id": str(payload.get("session_id") or ""),
            "arguments": dict(arguments),
        },
    )
    if not result.get("authorized"):
        raise PermissionError(str(result.get("reason") or "workspace authority denied"))


def _identifier(value: Any) -> str:
    identifier = str(value or "").strip()
    if not _ID.fullmatch(identifier):
        raise ValueError("workspace identifier is invalid")
    return identifier


def _action_arguments(name: str, payload: Mapping[str, Any]) -> dict[str, Any]:
    arguments: dict[str, Any] = {
        "workspace_id": str(payload.get("workspace_id") or ""),
        "expected_revision": max(0, int(payload.get("expected_revision") or 0)),
    }
    if name in {"mount", "update"}:
        arguments["root_path"] = str(payload.get("root_path") or "")
        arguments["metadata"] = dict(_mapping(payload.get("metadata")))
    return arguments


def _profile(payload: Mapping[str, Any]) -> str:
    return str(payload.get("profile_id") or "default")


def _mapping(value: Any) -> Mapping[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ValueError("workspace metadata must be an object")
    return value


def _assert_revision(state: Mapping[str, Any], expected: int) -> None:
    if int(state.get("revision") or 0) != expected:
        raise WorkspaceConflict("workspace mount revision is stale")


def _copy(value: Any) -> Any:
    return json.loads(json.dumps(value, ensure_ascii=False, default=str))


def _atomic_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=".mount-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
