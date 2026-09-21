"""Profile-scoped canonical Project state owned by the UI settings Pack."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import threading
from copy import deepcopy
from pathlib import Path
from typing import Any, Mapping

from core_runtime.host_provider_backend_v4 import (
    CapturedHostProviderV4,
    HostProviderCaptureContextV4,
    HostProviderContributionV4,
    HostProviderInvocationContextV4,
)
from core_runtime.runtime_locks import NamedLock
from tobkiri_protocol.canonical import canonical_digest

PACK_ID = "tobkiri_ui_settings_pack"
READ_FUNCTION_ID = "tobkiri.project.state.read"
WRITE_FUNCTION_ID = "tobkiri.project.state.replace"
READ_CONTRACT_ID = "tobkiri.resource.project.state.v1"
WRITE_CONTRACT_ID = "tobkiri.action.project.state.v1"
READ_OPERATION_ID = "tobkiri_ui_settings_pack.projects-read"
WRITE_OPERATION_ID = "tobkiri_ui_settings_pack.projects-replace"
_NAMESPACE = "defaultspack.projects.v1"
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,255}\Z")
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
_MAX_PROJECTS = 256
_MAX_RECEIPTS = 512
_LOCK = threading.RLock()


def _optional_text(value: Any, *, limit: int) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise ValueError("Project text is invalid")
    if len(value) > limit or "\x00" in value:
        raise ValueError("Project text exceeds its bound")
    return value


def _projects(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list) or len(value) > _MAX_PROJECTS:
        raise ValueError("Project collection is invalid")
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    expected = {
        "id", "title", "workspace_id", "workspace_label", "workspace_root",
        "rumi_data_path",
    }
    for item in value:
        if not isinstance(item, dict) or set(item) != expected:
            raise ValueError("Project record fields are invalid")
        identifier = item.get("id")
        if not isinstance(identifier, str) or _ID.fullmatch(identifier) is None:
            raise ValueError("Project identity is invalid")
        if identifier in seen:
            raise ValueError("Project identities must be unique")
        seen.add(identifier)
        title = _optional_text(item.get("title"), limit=256)
        if title is None:
            raise ValueError("Project title is required")
        result.append({
            "id": identifier,
            "title": title,
            "workspace_id": _optional_text(item.get("workspace_id"), limit=256),
            "workspace_label": _optional_text(item.get("workspace_label"), limit=512),
            # These are inert owner metadata. Filesystem access must still pass
            # through the workspace handle/jail; this Provider never opens them.
            "workspace_root": _optional_text(item.get("workspace_root"), limit=4096),
            "rumi_data_path": _optional_text(item.get("rumi_data_path"), limit=4096),
        })
    return result


class ProjectStateStore:
    """Atomically store one captured Profile/caller Project namespace."""

    def __init__(self, root: Path, profile_id: str, owner_principal_id: str) -> None:
        identity = canonical_digest({
            "namespace": _NAMESPACE,
            "profile_id": profile_id,
            "owner_principal_id": owner_principal_id,
        }).removeprefix("sha256:")
        self.path = root / "defaultspack" / "project_state" / f"{identity}.json"
        self.profile_id = profile_id
        self.owner_principal_id = owner_principal_id

    def _empty(self) -> dict[str, Any]:
        return {
            "kind": "tobkiri.project.state.v1",
            "namespace": _NAMESPACE,
            "profile_id": self.profile_id,
            "owner_principal_id": self.owner_principal_id,
            "revision": 0,
            "projects": [],
            "mutation_receipts": {},
        }

    def _read(self) -> dict[str, Any]:
        if not self.path.exists():
            return self._empty()
        value = json.loads(self.path.read_text(encoding="utf-8"))
        expected = set(self._empty())
        if (
            not isinstance(value, dict)
            or set(value) != expected
            or value.get("kind") != "tobkiri.project.state.v1"
            or value.get("namespace") != _NAMESPACE
            or value.get("profile_id") != self.profile_id
            or value.get("owner_principal_id") != self.owner_principal_id
            or type(value.get("revision")) is not int
            or value["revision"] < 0
            or not isinstance(value.get("mutation_receipts"), dict)
        ):
            raise ValueError("Canonical Project state is invalid")
        value["projects"] = _projects(value.get("projects"))
        return value

    def _write(self, value: Mapping[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        encoded = (json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n").encode()
        descriptor, temporary_name = tempfile.mkstemp(
            dir=self.path.parent, prefix=".projects-", suffix=".tmp"
        )
        temporary = Path(temporary_name)
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
        finally:
            temporary.unlink(missing_ok=True)

    @staticmethod
    def _projection(state: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "namespace": _NAMESPACE,
            "revision": state["revision"],
            "projects": deepcopy(state["projects"]),
        }

    def read(self) -> dict[str, Any]:
        """Read without creating owner state."""
        with _LOCK, NamedLock(self.path.parent, "projects-state"):
            return self._projection(self._read())

    def replace(
        self,
        *,
        projects: Any,
        expected_revision: Any,
        mutation_id: Any,
        migration_digest: Any,
    ) -> dict[str, Any]:
        """CAS-replace the bounded collection and return a durable receipt."""
        if type(expected_revision) is not int or expected_revision < 0:
            raise ValueError("Expected Project revision is invalid")
        if not isinstance(mutation_id, str) or _ID.fullmatch(mutation_id) is None:
            raise ValueError("Project mutation identity is invalid")
        if migration_digest is not None and (
            not isinstance(migration_digest, str)
            or _DIGEST.fullmatch(migration_digest) is None
        ):
            raise ValueError("Project migration digest is invalid")
        normalized = _projects(projects)
        input_digest = canonical_digest({
            "projects": normalized,
            "expected_revision": expected_revision,
            "migration_digest": migration_digest,
        })
        with _LOCK, NamedLock(self.path.parent, "projects-state"):
            state = self._read()
            existing = state["mutation_receipts"].get(mutation_id)
            if existing is not None:
                if existing.get("input_digest") != input_digest:
                    raise ValueError("Project mutation identity was reused")
                return deepcopy(existing["acknowledgement"])
            if state["revision"] != expected_revision:
                raise ValueError("Project revision conflict")
            state["projects"] = normalized
            state["revision"] += 1
            acknowledgement = {
                **self._projection(state),
                "receipt": canonical_digest({
                    "namespace": _NAMESPACE,
                    "profile_id": self.profile_id,
                    "owner_principal_id": self.owner_principal_id,
                    "mutation_id": mutation_id,
                    "revision": state["revision"],
                    "input_digest": input_digest,
                }),
                "mutation_id": mutation_id,
                "migration_digest": migration_digest,
            }
            receipts = state["mutation_receipts"]
            receipts[mutation_id] = {
                "input_digest": input_digest,
                "acknowledgement": acknowledgement,
            }
            while len(receipts) > _MAX_RECEIPTS:
                receipts.pop(next(iter(receipts)))
            self._write(state)
            return deepcopy(acknowledgement)


class ProjectStateHostFactoryV4:
    """Bind one finite Project operation to captured Host identity."""

    def __init__(self, function_id: str) -> None:
        self.function_id = function_id

    def capture(self, context: HostProviderCaptureContextV4) -> CapturedHostProviderV4:
        """Capture Profile/root while leaving caller identity Host-authenticated."""
        expected = {
            READ_FUNCTION_ID: (READ_CONTRACT_ID, READ_OPERATION_ID),
            WRITE_FUNCTION_ID: (WRITE_CONTRACT_ID, WRITE_OPERATION_ID),
        }[self.function_id]
        if not context.profile_id or context.user_data_root is None or len(context.provider_bindings) != 1:
            raise PermissionError("Project state capture is incomplete")
        binding = context.provider_bindings[0]
        operation = binding.operation
        if binding.function.function_id != self.function_id or (
            operation.contract_id, operation.operation_id
        ) != expected:
            raise PermissionError("Project state binding is invalid")
        domain_id = context.domain_ids.get(
            (operation.contract_id, operation.operation_id, binding.principal_ref.value)
        )
        if domain_id is None:
            raise PermissionError("Project state domain is unavailable")

        def invoke(
            operation_id: str,
            payload: Mapping[str, Any],
            invocation: HostProviderInvocationContextV4,
        ) -> Mapping[str, Any]:
            invocation.assert_current()
            caller = invocation.presentation_owner_principal_id
            session = invocation.presentation_owner_session_id
            if not caller or not session or payload.get("profile_id") != context.profile_id:
                raise PermissionError("Project caller identity is unavailable")
            store = ProjectStateStore(context.user_data_root, context.profile_id, caller)
            if operation_id == READ_OPERATION_ID:
                if set(payload) - {"profile_id", "_session_id"}:
                    raise PermissionError("Project read request is invalid")
                return store.read()
            if operation_id != WRITE_OPERATION_ID or set(payload) - {
                "profile_id", "projects", "expected_revision", "mutation_id",
                "migration_digest", "_session_id",
            } or not {"projects", "expected_revision", "mutation_id"} <= set(payload):
                raise PermissionError("Project replace request is invalid")
            result = store.replace(
                projects=payload["projects"],
                expected_revision=payload["expected_revision"],
                mutation_id=payload["mutation_id"],
                migration_digest=payload.get("migration_digest"),
            )
            # Session is authenticated and intentionally recorded only in this
            # per-call receipt projection, never accepted from client payload.
            return {**result, "caller_session_digest": hashlib.sha256(session.encode()).hexdigest()}

        return CapturedHostProviderV4((HostProviderContributionV4(
            contract_id=operation.contract_id,
            contract_version=operation.contract_version,
            operation_id=operation.operation_id,
            principal_id=binding.principal_ref.value,
            artifact_digest=binding.artifact.digest,
            implementation_digest=binding.function.implementation_digest,
            domain_id=domain_id,
            invoke=invoke,
        ),), lambda: None)


HOST_PROVIDER_FACTORY = {
    READ_FUNCTION_ID: ProjectStateHostFactoryV4(READ_FUNCTION_ID),
    WRITE_FUNCTION_ID: ProjectStateHostFactoryV4(WRITE_FUNCTION_ID),
}
