"""Verified in-process providers for defaultspack coding contract tests.

The compatibility blocks no longer own workspace paths or filesystem state.  These
tests therefore bind the real Wave 8 providers behind a selected workspace id and
the real one-shot Host authority bridge instead of passing a caller-controlled
``workspace_root`` directly to legacy blocks.
"""

from __future__ import annotations

import hashlib
import importlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from ecosystem.rumi_file_inspect_pack.runtime.inspect import FileInspectService
from ecosystem.rumi_file_mutation_pack.runtime.mutate import FileMutationService
from ecosystem.rumi_file_patch_pack.runtime.patch import FilePatchService
from ecosystem.rumi_git_read_pack.runtime.read import GitReadService
from ecosystem.rumi_git_write_pack.runtime.write import GitWriteService
from ecosystem.rumi_host_authority_bridge_pack.runtime.bridge import (
    create_authority_operation,
)
from ecosystem.rumi_shell_execute_pack.runtime.execute import ShellExecuteService
from ecosystem.rumi_shell_policy_pack.runtime.policy import (
    create_shell_policy_operation,
)
from ecosystem.rumi_terminal_session_pack.runtime.sessions import (
    create_terminal_control,
)

from domain.coding.contract_adapter import (
    FILE_INSPECT,
    FILE_MUTATE,
    FILE_PATCH,
    GIT_READ,
    GIT_WRITE,
    HOST_AUTHORITY,
    SHELL_EXECUTE,
    SHELL_INSPECT,
    TERMINAL_CONTROL,
    WORKSPACE_ACTION,
    WORKSPACE_RESOURCE,
)


class VerifiedCodingContracts:
    """Bind canonical providers to one exact workspace mount."""

    profile_id = "tooling-hardening-test"

    def __init__(
        self,
        root: Path,
        workspace_id: str = "trusted",
        *,
        trusted: bool = True,
    ) -> None:
        self.root = root.resolve(strict=True)
        self.workspace_id = workspace_id
        self.mount_revision = 1
        self.trusted = bool(trusted)
        self.label = workspace_id
        self.trust_granted_at = (
            datetime.now(timezone.utc).isoformat() if self.trusted else None
        )
        self.selected_workspace_id = workspace_id
        self.revision = 1
        self.authority = create_authority_operation(self)
        self.file_inspect = FileInspectService(self)
        self.file_mutate = FileMutationService(self)
        self.file_patch = FilePatchService(self)
        self.git_read = GitReadService(self)
        self.git_write = GitWriteService(self)
        self.shell_policy = create_shell_policy_operation(self)
        self.shell_execute = ShellExecuteService(self)
        self.terminal_control = create_terminal_control(self)

    def invoke(
        self,
        contract_id: str,
        operation: str,
        payload: Mapping[str, Any],
        **_: Any,
    ) -> dict[str, Any]:
        request = {"profile_id": self.profile_id, **dict(payload)}
        if contract_id == WORKSPACE_RESOURCE:
            if operation == "list":
                return {
                    "selected_workspace_id": self.selected_workspace_id,
                    "revision": self.revision,
                    "mounts": [self._mount()],
                    "workspaces": [self._mount()],
                }
            if operation == "get":
                if str(request.get("workspace_id") or "") != self.workspace_id:
                    raise KeyError("workspace mount is unknown")
                return self._mount()
        if contract_id == WORKSPACE_ACTION:
            return self._workspace_action(operation, request)
        if contract_id == HOST_AUTHORITY:
            request.setdefault(
                "_contract_consumer_pack_id",
                "defaultspack"
                if operation == "authorize"
                else str(request.get("service_pack_id") or ""),
            )
            if (
                operation == "authorize"
                and not self.trusted
                and str(request.get("authority") or "")
                in {"file.write", "file.create", "file.delete", "file.move", "git.write"}
            ):
                return {
                    "authorized": False,
                    "reason": "workspace_untrusted",
                    "code": "WORKSPACE_UNTRUSTED",
                    "message": "trusted workspace required",
                }
            return self.authority(operation, request)
        if contract_id == FILE_INSPECT:
            request["_workspace_binding"] = self._read_only_binding()
            return self.file_inspect.invoke(operation, request)
        if contract_id == FILE_MUTATE:
            return self.file_mutate.invoke(operation, request)
        if contract_id == FILE_PATCH:
            return self.file_patch.invoke(operation, request)
        if contract_id == GIT_READ:
            return self.git_read.invoke(operation, request)
        if contract_id == GIT_WRITE:
            return self.git_write.invoke(operation, request)
        if contract_id == SHELL_INSPECT:
            return self.shell_policy(operation, request)
        if contract_id == SHELL_EXECUTE:
            return self.shell_execute.invoke(operation, request)
        if contract_id == TERMINAL_CONTROL:
            return self.terminal_control(operation, request)
        raise AssertionError((contract_id, operation, request))

    def _mount(self) -> dict[str, Any]:
        return {
            "id": self.workspace_id,
            "workspace_id": self.workspace_id,
            "root_path": str(self.root),
            "revision": str(self.mount_revision),
            "mount_revision": self.mount_revision,
            "updated_at": "2026-01-01T00:00:00+00:00",
            "metadata": {
                "label": self.label,
                "trusted": self.trusted,
                "trust_granted_at": self.trust_granted_at,
                "metadata": {},
            },
            "trusted": self.trusted,
        }

    def _workspace_action(
        self,
        operation: str,
        request: Mapping[str, Any],
    ) -> dict[str, Any]:
        workspace_id = str(request.get("workspace_id") or "").strip()
        if workspace_id and workspace_id != self.workspace_id:
            if operation != "mount":
                raise KeyError("workspace mount is unknown")
            self.workspace_id = workspace_id
        metadata = request.get("metadata")
        metadata = dict(metadata) if isinstance(metadata, Mapping) else {}
        if operation == "mount":
            root_path = str(request.get("root_path") or "").strip()
            if not root_path:
                raise ValueError("workspace root is required")
            self.root = Path(root_path).resolve(strict=True)
            self.label = str(metadata.get("label") or self.workspace_id)
            self.trusted = bool(metadata.get("trusted", False))
            self.trust_granted_at = metadata.get("trust_granted_at")
        elif operation == "trust":
            self.trusted = True
            self.trust_granted_at = datetime.now(timezone.utc).isoformat()
        elif operation == "update":
            root_path = str(request.get("root_path") or "").strip()
            if root_path:
                self.root = Path(root_path).resolve(strict=True)
            self.label = str(metadata.get("label") or self.label)
            self.trusted = bool(metadata.get("trusted", self.trusted))
            self.trust_granted_at = metadata.get(
                "trust_granted_at", self.trust_granted_at
            )
        elif operation == "select":
            self.selected_workspace_id = self.workspace_id
        else:
            raise ValueError(f"unknown workspace action: {operation}")
        self.revision += 1
        result = {"mount": self._mount(), "revision": self.revision}
        if operation == "select":
            result["selected_workspace_id"] = self.selected_workspace_id
        return result

    def _read_only_binding(self) -> dict[str, Any]:
        stat = self.root.stat()
        binding = {
            "workspace_id": self.workspace_id,
            "access": "read_only",
            "mount_revision": str(self.mount_revision),
            "canonical_root": str(self.root),
            "root_st_dev": int(stat.st_dev),
            "root_st_ino": int(stat.st_ino),
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


def bind_verified_coding_contracts(
    monkeypatch: Any,
    root: Path,
    *,
    workspace_id: str = "trusted",
    trusted: bool = True,
) -> VerifiedCodingContracts:
    """Patch only the compatibility adapter seam to selected real providers."""

    contracts = VerifiedCodingContracts(root, workspace_id, trusted=trusted)
    monkeypatch.setattr(
        "domain.coding.contract_adapter._profile_id",
        lambda: contracts.profile_id,
    )
    monkeypatch.setattr(
        "domain.coding.contract_adapter.invoke_coding_contract",
        contracts.invoke,
    )
    for module_name in (
        "file_create",
        "file_delete",
        "file_diff",
        "file_list",
        "file_patch",
        "file_read",
        "file_search",
        "file_write",
        "context",
        "git_branch",
        "git_diff",
        "git_status",
        "git_push",
        "terminal_exec",
        "terminal_stream",
        "git_commit",
    ):
        module = importlib.import_module(f"blocks.coding.{module_name}")
        if hasattr(module, "invoke_coding_contract"):
            monkeypatch.setattr(
                module,
                "invoke_coding_contract",
                contracts.invoke,
            )
    workspace_contract = importlib.import_module("blocks.coding.workspace._contract")
    monkeypatch.setattr(workspace_contract, "invoke_coding_contract", contracts.invoke)
    if hasattr(workspace_contract, "contract_adapter"):
        monkeypatch.setattr(
            workspace_contract.contract_adapter,
            "invoke_coding_contract",
            contracts.invoke,
        )
    coding_workspace_module = importlib.import_module("blocks.coding._workspace")
    monkeypatch.setattr(
        coding_workspace_module.contract_adapter,
        "invoke_coding_contract",
        contracts.invoke,
    )
    return contracts
