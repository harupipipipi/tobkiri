"""Finite defaultspack compatibility adapter for Wave 8 coding contracts."""

from __future__ import annotations

from typing import Any, Callable, Mapping

from core_runtime.di_container import get_container
from core_runtime.global_contract_dispatch import (
    captured_profile_id,
    invoke_global_contract,
)
from domain.safety import approval
from domain.tool_policy.internal_context import (
    tool_server_approval_context_is_internal,
)

FILE_INSPECT = "rumi.service.file.inspect.v1"
FILE_MUTATE = "rumi.service.file.mutate.v1"
FILE_PATCH = "rumi.service.file.patch.v1"
SHELL_INSPECT = "rumi.service.shell.inspect.v1"
SHELL_EXECUTE = "rumi.service.shell.execute.v1"
TERMINAL_RESOURCE = "rumi.resource.terminal.session.v1"
TERMINAL_CONTROL = "rumi.action.terminal.session.v1"
SANDBOX_OBSERVE = "rumi.resource.coding.sandbox.v1"
SANDBOX_CONTROL = "rumi.action.coding.sandbox.v1"
WORKSPACE_RESOURCE = "rumi.resource.workspace.v1"
WORKSPACE_ACTION = "rumi.action.workspace.mount.v1"
GIT_READ = "rumi.service.git.read.v1"
GIT_WRITE = "rumi.service.git.write.v1"
GIT_PUBLISH = "rumi.service.git.publish.v1"
HOST_AUTHORITY = "rumi.service.host.authorize.v1"

MutationGuard = Callable[
    [str, Mapping[str, Any], Mapping[str, Any] | None, str],
    Mapping[str, Any] | None,
]


def preflight_legacy_coding_operation(
    *,
    legacy_operation: str,
    input_data: Mapping[str, Any],
    context: Mapping[str, Any] | None,
    selected_workspace_id: str,
    mutation_guard: MutationGuard,
    allow_without_approval: bool = False,
) -> dict[str, Any]:
    """Check approval and the adaptive lease without minting a receipt.

    Coding adapters use this before reading a repository snapshot.  It keeps
    approval and lease denials deterministic even when the workspace provider
    or repository is unavailable, while leaving one-shot token consumption and
    host receipt issuance to ``authorize_legacy_coding_operation``.
    """

    request = dict(input_data)
    internal = tool_server_approval_context_is_internal(
        dict(context) if isinstance(context, Mapping) else None
    )
    if not internal and not allow_without_approval:
        token = _approval_token(request)
        if not token:
            return {"authorized": False, "reason": "approval_required"}
        verification = approval.verify_execution_token(
            token,
            legacy_operation,
            approval.hash_arguments(request),
            consume=False,
        )
        if not verification.valid:
            return {
                "authorized": False,
                "reason": "approval_invalid",
                "code": verification.code or "APPROVAL_INVALID",
                "message": verification.message or "approval token is invalid",
            }
    guard_denial = mutation_guard(
        selected_workspace_id,
        request,
        context,
        legacy_operation,
    )
    if guard_denial is not None:
        return {"authorized": False, **dict(guard_denial)}
    return {"authorized": True}


def invoke_coding_contract(
    contract_id: str,
    operation: str,
    payload: Mapping[str, Any],
) -> dict[str, Any]:
    """Invoke exactly one selected coding provider for the active profile."""

    registry = get_container().get_or_none("v4_dispatch_session")
    if registry is None:
        raise RuntimeError("global coding provider is unavailable")
    request = {
        "profile_id": captured_profile_id(registry),
        **dict(payload),
        "_contract_consumer_pack_id": "defaultspack",
    }
    result = invoke_global_contract(registry, contract_id, operation, request)
    if not isinstance(result, dict):
        raise RuntimeError("coding provider returned an invalid result")
    return result


def workspace_id(input_data: Mapping[str, Any]) -> str:
    """Require the canonical workspace identifier; never infer a root path."""

    value = str(input_data.get("workspace_id") or "").strip()
    if not value:
        raise ValueError("workspace_id is required")
    return value


def git_snapshot(
    selected_workspace_id: str,
    *,
    paths: list[str] | None = None,
    capture_commit: bool = False,
    all_tracked: bool = False,
    branch: str | None = None,
    source: str | None = None,
    expect_branch_absent: bool = False,
) -> dict[str, Any]:
    """Read the exact Git and mount snapshot required by a write provider.

    ``paths`` lets stage/restore bind the content blobs they will touch;
    ``branch`` additionally binds a branch operation's destination ref.
    Neither field is caller authority: each is captured by the selected
    read-only Git provider and later embedded in the Host receipt.
    """

    workspace = invoke_coding_contract(
        WORKSPACE_RESOURCE,
        "get",
        {"workspace_id": selected_workspace_id},
    )
    mount_revision = int(workspace.get("mount_revision") or 0)
    if mount_revision < 1:
        raise RuntimeError("workspace mount revision is unavailable")
    request: dict[str, Any] = {"workspace_id": selected_workspace_id}
    if paths is not None:
        request["paths"] = [str(path) for path in paths]
    if capture_commit:
        request["capture_commit"] = True
        request["all_tracked"] = bool(all_tracked)
    if branch:
        request["branch"] = str(branch)
        request["expect_branch_absent"] = bool(expect_branch_absent)
    if source:
        request["source"] = str(source)
    snapshot = invoke_coding_contract(
        GIT_READ,
        "snapshot",
        request,
    )
    required = {
        "expected_head",
        "expected_tree",
        "expected_index_tree",
        "expected_status_hash",
        "expected_worktree_hash",
    }
    if not required.issubset(snapshot):
        raise RuntimeError("Git snapshot is incomplete")
    return {
        "expected_head": str(snapshot["expected_head"]),
        "expected_tree": str(snapshot["expected_tree"]),
        "expected_index_tree": str(snapshot["expected_index_tree"]),
        "expected_status_hash": str(snapshot["expected_status_hash"]),
        "expected_worktree_hash": str(snapshot["expected_worktree_hash"]),
        "expected_mount_revision": mount_revision,
        **(
            {"expected_path_entries": list(snapshot.get("expected_path_entries") or [])}
            if paths is not None and not capture_commit
            else {}
        ),
        **(
            {
                "expected_head_ref": str(snapshot["expected_head_ref"]),
                "expected_commit_entries": list(
                    snapshot.get("expected_commit_entries") or []
                ),
            }
            if capture_commit
            else {}
        ),
        **(
            {"expected_restore_tree": str(snapshot["expected_restore_tree"])}
            if snapshot.get("expected_restore_tree") and not capture_commit
            else {}
        ),
        **(
            {"expected_branch_oid": str(snapshot["expected_branch_oid"])}
            if snapshot.get("expected_branch_oid")
            else {}
        ),
    }


def git_publish_snapshot(
    selected_workspace_id: str,
    *,
    remote: str,
    branch: str,
) -> dict[str, Any]:
    """Capture immutable local/remote ref inputs for one Git publication."""

    workspace = invoke_coding_contract(
        WORKSPACE_RESOURCE,
        "get",
        {"workspace_id": selected_workspace_id},
    )
    mount_revision = int(workspace.get("mount_revision") or 0)
    if mount_revision < 1:
        raise RuntimeError("workspace mount revision is unavailable")
    snapshot = invoke_coding_contract(
        GIT_READ,
        "publish_snapshot",
        {
            "workspace_id": selected_workspace_id,
            "remote": str(remote),
            "branch": str(branch),
        },
    )
    required = {
        "expected_source_oid",
        "expected_remote_oid",
        "expected_remote_url",
        "expected_remote_url_hash",
    }
    if not required.issubset(snapshot):
        raise RuntimeError("Git publication snapshot is incomplete")
    return {
        "expected_source_oid": str(snapshot["expected_source_oid"]),
        "expected_remote_oid": str(snapshot["expected_remote_oid"]),
        "expected_remote_url": str(snapshot["expected_remote_url"]),
        "expected_remote_url_hash": str(snapshot["expected_remote_url_hash"]),
        "expected_mount_revision": mount_revision,
    }


def authorize_legacy_coding_operation(
    *,
    legacy_operation: str,
    service_pack_id: str,
    service_operation: str,
    authority: str,
    arguments: Mapping[str, Any],
    input_data: Mapping[str, Any],
    context: Mapping[str, Any] | None,
    selected_workspace_id: str,
    mutation_guard: MutationGuard,
    allow_without_approval: bool = False,
) -> dict[str, Any]:
    """Validate authority, gate the mutation, then mint one service receipt."""

    request = dict(input_data)
    internal = tool_server_approval_context_is_internal(
        dict(context) if isinstance(context, Mapping) else None
    )
    verification = None
    token = ""
    arguments_hash = ""
    if not internal and not allow_without_approval:
        token = _approval_token(request)
        if not token:
            return {"authorized": False, "reason": "approval_required"}
        arguments_hash = approval.hash_arguments(request)
        verification = approval.verify_execution_token(
            token,
            legacy_operation,
            arguments_hash,
            consume=False,
        )
        if not verification.valid:
            return {
                "authorized": False,
                "reason": "approval_invalid",
                "code": verification.code or "APPROVAL_INVALID",
                "message": verification.message or "approval token is invalid",
            }
    guard_denial = mutation_guard(
        selected_workspace_id,
        request,
        context,
        legacy_operation,
    )
    if guard_denial is not None:
        return {"authorized": False, **dict(guard_denial)}
    if token:
        verification = approval.verify_execution_token(
            token,
            legacy_operation,
            arguments_hash,
            consume=True,
        )
        if not verification.valid:
            return {
                "authorized": False,
                "reason": "approval_invalid",
                "code": verification.code or "APPROVAL_INVALID",
                "message": verification.message or "approval token is invalid",
            }
    ctx = dict(context) if isinstance(context, Mapping) else {}
    caller_id = str(
        ctx.get("principal_id") or ctx.get("user_id") or "defaultspack.local_user"
    )
    scope = {
        "service_pack_id": service_pack_id,
        "operation": service_operation,
        "authority": authority,
        "caller_id": caller_id,
        "caller_pack_id": "defaultspack",
        "caller_function_id": legacy_operation,
        "profile_id": _profile_id(),
        "workspace_id": selected_workspace_id,
        "session_id": str(ctx.get("session_id") or ctx.get("conversation_id") or ""),
        "arguments": dict(arguments),
        "approval_required": False,
    }
    issued = invoke_coding_contract(HOST_AUTHORITY, "authorize", scope)
    if not issued.get("authorized"):
        return issued
    return {
        **issued,
        **scope,
        "approval_request_id": getattr(verification, "request_id", ""),
    }


def service_payload(
    authorization: Mapping[str, Any],
    arguments: Mapping[str, Any],
) -> dict[str, Any]:
    """Bind an issued receipt and its exact scope to a service request."""

    return {
        **dict(arguments),
        "authority_receipt": str(authorization.get("receipt") or ""),
        "caller_id": str(authorization.get("caller_id") or ""),
        "caller_pack_id": "defaultspack",
        "caller_function_id": str(authorization.get("caller_function_id") or ""),
        "workspace_id": str(authorization.get("workspace_id") or ""),
        "session_id": str(authorization.get("session_id") or ""),
    }


def _approval_token(input_data: Mapping[str, Any]) -> str:
    token = str(input_data.get("approval_token") or "").strip()
    if token:
        return token
    headers = input_data.get("_headers")
    if isinstance(headers, Mapping):
        return str(
            headers.get("X-Rumi-Approval") or headers.get("x-rumi-approval") or ""
        ).strip()
    return ""


def _profile_id() -> str:
    session = get_container().get_or_none("v4_dispatch_session")
    if session is None:
        raise RuntimeError("resolved profile is unavailable")
    return captured_profile_id(session)
