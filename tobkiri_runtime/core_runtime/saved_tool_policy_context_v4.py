"""Capture actual saved ancestry, conversation metadata and owned workspace."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Mapping

from tobkiri_host.saved_tool_context import (
    SAVED_TOOL_CAPTURE_FIELDS,
    saved_tool_owner_and_request_scope,
)
from tobkiri_protocol.canonical import canonical_digest


@dataclass(frozen=True)
class SavedToolPolicyContextV4:
    """Host-private current facts supplied to native policy selection."""

    root: Any
    mode: str
    conversation_id: str
    turn_id: str
    workspace_id: str
    workspace_binding: Any
    capture_digest: str
    assert_current: Callable[[], None]
    saved_scope: Any


def capture_saved_tool_policy_context(
    invocation: Any,
    *,
    profile_id: str,
    read_conversation: Callable[[Any, str], Mapping[str, Any]],
    resolve_workspace: Callable[[str, str], Any],
    selected_workspace: Callable[[], Mapping[str, Any]],
) -> SavedToolPolicyContextV4:
    """Use formal conversation reads and current native-owned mount receipts."""
    root, accepted = saved_tool_owner_and_request_scope(invocation)
    saved = [accepted]
    request = saved[0].envelope.payload.get("request")
    if not isinstance(request, Mapping):
        raise PermissionError("saved policy request is unavailable")
    mode, conversation, turn = (
        request.get("action_approval_mode", "ask"),
        request.get("conversation_id"),
        request.get("turn_id"),
    )
    if (
        mode not in {"agent", "full"}
        or not isinstance(conversation, str)
        or not conversation
        or not isinstance(turn, str)
        or not turn
    ):
        raise PermissionError("saved policy elevated request is unavailable")
    request_digest = canonical_digest(dict(request))
    document = read_conversation(invocation, conversation)
    metadata = document.get("metadata")
    workspace_id = metadata.get("workspace_id") if isinstance(metadata, Mapping) else None
    if document.get("id") != conversation or not isinstance(workspace_id, str) or not workspace_id:
        raise PermissionError("saved policy owned workspace is unavailable")
    binding = resolve_workspace(profile_id, workspace_id)
    if binding.profile_id != profile_id or binding.workspace_id != workspace_id:
        raise PermissionError("saved policy workspace receipt changed")
    identity = (
        str(binding.canonical_root),
        binding.mount_revision,
        binding.root_st_dev,
        binding.root_st_ino,
    )
    capture = canonical_digest({key: getattr(root, key) for key in SAVED_TOOL_CAPTURE_FIELDS})

    def guard() -> None:
        invocation.assert_current()
        saved[0].assert_current()
        current_root, current_scope = saved_tool_owner_and_request_scope(invocation)
        if current_scope is not saved[0]:
            raise PermissionError("saved policy accepted scope changed")
        if (
            canonical_digest({key: getattr(current_root, key) for key in SAVED_TOOL_CAPTURE_FIELDS})
            != capture
            or current_root.caller_principal != root.caller_principal
            or current_root.caller_session_id != root.caller_session_id
            or canonical_digest(dict(saved[0].envelope.payload.get("request", {})))
            != request_digest
        ):
            raise PermissionError("saved policy original capture changed")
        current = resolve_workspace(profile_id, workspace_id)
        if (
            str(current.canonical_root),
            current.mount_revision,
            current.root_st_dev,
            current.root_st_ino,
        ) != identity:
            raise PermissionError("saved policy workspace receipt changed")
        selected = selected_workspace()
        if (
            selected.get("workspace_id") != workspace_id
            or selected.get("canonical_root") != identity[0]
            or selected.get("mount_revision") != identity[1]
            or selected.get("root_st_dev") != identity[2]
            or selected.get("root_st_ino") != identity[3]
        ):
            raise PermissionError("saved policy workspace is not Host selected")

    guard()
    return SavedToolPolicyContextV4(
        root, mode, conversation, turn, workspace_id, binding, capture, guard, saved[0]
    )


def assert_saved_native_policy_boundary(
    invocation: Any,
    authenticated_root: Any,
    capture: Mapping[str, Any],
    *,
    workspace_id: str,
    resolve_workspace: Callable[[str, str], Any],
    selected_workspace: Callable[[], Mapping[str, Any]],
) -> None:
    """Repeat saved owner, request and native mount proof without Broker I/O."""
    current_root, accepted = saved_tool_owner_and_request_scope(invocation)
    if current_root != authenticated_root:
        raise PermissionError("native policy saved owner changed")
    if current_root.caller_principal.value != capture.get(
        "owner_principal"
    ) or current_root.caller_session_id != capture.get("owner_session"):
        raise PermissionError("native policy presentation owner changed")
    native_context = capture.get("context")
    if not isinstance(native_context, Mapping) or any(
        getattr(current_root, key) != native_context.get(key) for key in SAVED_TOOL_CAPTURE_FIELDS
    ):
        raise PermissionError("native policy saved capture changed")
    saved = [accepted]
    request = saved[0].envelope.payload.get("request")
    if (
        not isinstance(request, Mapping)
        or request.get("conversation_id") != capture.get("conversation")
        or request.get("turn_id") != capture.get("turn")
        or request.get("action_approval_mode", "ask") != capture.get("mode")
    ):
        raise PermissionError("native policy saved request changed")
    binding = resolve_workspace(current_root.profile_id, workspace_id)
    selected = selected_workspace()
    expected_receipt = {
        "workspace_id": workspace_id,
        "canonical_root": str(binding.canonical_root),
        "mount_revision": binding.mount_revision,
        "root_st_dev": binding.root_st_dev,
        "root_st_ino": binding.root_st_ino,
    }
    routes = capture.get("delegation_boundary", {}).get("routes")
    if (
        not isinstance(routes, list)
        or not routes
        or any(route.get("workspace_receipt") != expected_receipt for route in routes)
    ):
        raise PermissionError("native policy original workspace receipt changed")
    if (
        binding.workspace_id != workspace_id
        or str(binding.canonical_root) != capture.get("workspace")
        or selected.get("workspace_id") != workspace_id
        or selected.get("canonical_root") != str(binding.canonical_root)
        or selected.get("mount_revision") != binding.mount_revision
        or selected.get("root_st_dev") != binding.root_st_dev
        or selected.get("root_st_ino") != binding.root_st_ino
    ):
        raise PermissionError("native policy owned workspace changed")
