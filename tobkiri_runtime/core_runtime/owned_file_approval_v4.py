"""Present one saved file tool's approval using its live Host ancestry.

This helper is Host-only. It receives captured ports and an authenticated
invocation, never wire owner identities, grants, tokens, or request contexts.
"""

from __future__ import annotations

from typing import Any, Mapping
from dataclasses import dataclass
from threading import RLock
import time
import uuid

from tobkiri_protocol.canonical import canonical_digest

from tobkiri_host.ports import (
    AuthorityApprovalWindowOpenCommand,
    InteractiveApprovalGetQuery,
)

LOCAL = (
    "tobkiri.service.tool.local.operation.v1",
    "rumi_default_tools_pack.file-create-operation",
)
EXECUTOR = (
    "tobkiri.service.tool.execute.v1",
    "rumi_tool_local_executor_pack.tool-local-execute",
)
BROKER = ("tobkiri.service.tool.invoke.v1", "rumi_tool_broker_pack.tool-invoke")
SAVED = ("conversation.saved-turn.v1", "saved_complete")


@dataclass
class _LiveFileRequest:
    digest: str
    capture: tuple[Any, ...]
    guard: Any
    deadline: float
    cancel: Any = None
    file_edit_receipt: dict[str, Any] | None = None
    policy_inheritance: Any = None


_live_lock = RLock()
_live_requests: dict[str, _LiveFileRequest] = {}
_file_effects: dict[str, str] = {}


def register_file_tool_request(
    request: Mapping[str, Any], invocation: Any, *, policy_inheritance: Any = None,
) -> dict[str, Any]:
    """Retain a guarded Host invocation before any durable effect is created."""
    authenticated_file_tool_owner(invocation)
    if policy_inheritance is not None:
        policy_inheritance.assert_current()
        if policy_inheritance.policy.mode not in {"agent", "full"}:
            raise PermissionError("file create selected policy is unavailable")

    def current_file_guard() -> None:
        authenticated_file_tool_owner(invocation)
        if policy_inheritance is not None:
            policy_inheritance.assert_current()

    key = str(uuid.uuid4())
    bound = {**dict(request), "invocation_key": key}
    record = _LiveFileRequest(
        canonical_digest(bound),
        _capture_identity(invocation.envelope.context),
        current_file_guard,
        time.monotonic() + 90,
        policy_inheritance=policy_inheritance,
    )
    with _live_lock:
        _live_requests[key] = record
    return bound


def assert_file_request_live(request: Mapping[str, Any], context: Any) -> None:
    """Require the exact live saved request in addition to Broker authority."""
    key = request.get("invocation_key")
    if not isinstance(key, str):
        raise PermissionError("file create original invocation is unavailable")
    with _live_lock:
        record = _live_requests.get(key)
    if (
        record is None
        or record.capture != _capture_identity(context)
        or record.digest != canonical_digest(dict(request))
        or time.monotonic() >= record.deadline
    ):
        raise PermissionError("file create original invocation is unavailable")
    record.guard()
    with _live_lock:
        if _live_requests.get(key) is not record:
            raise PermissionError("file create original invocation drained")


def file_request_policy(request: Mapping[str, Any], context: Any) -> Any:
    """Return only the private policy already bound to this exact live file request."""
    assert_file_request_live(request, context)
    with _live_lock:
        record = _live_requests.get(request["invocation_key"])
        if record is None:
            raise PermissionError("file create original invocation drained")
        inheritance = record.policy_inheritance
    if inheritance is not None:
        inheritance.assert_current()
    return inheritance


def bind_file_effect(effect_id: str, request: Mapping[str, Any], context: Any, cancel: Any) -> None:
    """Bind Host-only cleanup before returning the durable prepare receipt."""
    try:
        assert_file_request_live(request, context)
        with _live_lock:
            record = _live_requests.get(request["invocation_key"])
            if record is None or record.cancel is not None or effect_id in _file_effects:
                raise PermissionError("file create effect binding is unavailable")
            record.cancel = cancel
            _file_effects[effect_id] = request["invocation_key"]
    except BaseException:
        cancel()
        raise


def file_effect_execution_guard(effect_id: str, context: Any) -> Any:
    """Return a fail-closed guard for claim, Broker entry and Provider dispatch."""

    def guard() -> None:
        with _live_lock:
            key = _file_effects.get(effect_id)
            record = _live_requests.get(key) if key is not None else None
        if (
            key is None
            or record is None
            or record.capture != _capture_identity(context)
            or time.monotonic() >= record.deadline
        ):
            raise PermissionError("file create original invocation is unavailable")
        record.guard()
        with _live_lock:
            if _live_requests.get(key) is not record:
                raise PermissionError("file create original invocation drained")

    guard()
    return guard


def record_file_tool_edit(
    request: Mapping[str, Any], context: Any, receipt: Mapping[str, Any]
) -> None:
    """Hold published-file metadata privately until the effect succeeds.

    This is not an authority receipt and never changes execution permissions.
    The existing guarded request lifetime owns and drains the metadata.
    """
    from core_runtime.file_edit_receipts import validate_file_edit_receipt

    assert_file_request_live(request, context)
    value = validate_file_edit_receipt(receipt)
    if (
        value is None
        or value["operation"] != "create"
        or value["profile_id"] != context.profile_id
        or value["workspace_id"] != request.get("workspace_id")
        or value["path"] != request.get("path")
    ):
        raise PermissionError("file edit metadata binding is invalid")
    with _live_lock:
        record = _live_requests.get(request["invocation_key"])
        if record is None or record.file_edit_receipt is not None:
            raise PermissionError("file edit metadata is unavailable")
        record.file_edit_receipt = value


def file_tool_edit_after_success(
    request: Mapping[str, Any], context: Any, effect_id: str
) -> dict[str, Any] | None:
    """Release metadata after the caller observes its exact terminal success.

    Only the finite Host tool's succeeded/resumed branch calls this function;
    unknown/recovered effects and unsuccessful outcomes have no public record.
    """
    assert_file_request_live(request, context)
    with _live_lock:
        key = request["invocation_key"]
        record = _live_requests.get(key)
        if record is None or _file_effects.get(effect_id) != key:
            return None
        value = record.file_edit_receipt
        return {**value, "stats": dict(value["stats"])} if value else None


def close_file_tool_request(key: str) -> None:
    """Drain the private lifetime and cancel via Host state even after Stop."""
    with _live_lock:
        record = _live_requests.pop(key, None)
        effects = [effect for effect, value in _file_effects.items() if value == key]
        for effect in effects:
            _file_effects.pop(effect, None)
    if record is not None and record.cancel is not None:
        record.cancel()


def _capture_identity(context: Any) -> tuple[Any, ...]:
    return tuple(
        getattr(context, name)
        for name in (
            "profile_id",
            "profile_revision",
            "activation_id",
            "activation_digest",
            "plan_digest",
            "profile_authority_digest",
            "security_epoch",
            "fencing_token",
        )
    )


def authenticated_file_tool_owner(invocation: Any) -> Any:
    """Return the actual live root context for one finite saved tool call."""
    invocation.assert_current()
    envelope = invocation.envelope
    if (envelope.contract_id, envelope.operation_id) != LOCAL:
        raise PermissionError("file approval invocation is unavailable")
    scopes: list[Any] = []
    scope = invocation.parent_invocation
    while scope is not None:
        if len(scopes) >= 16 or any(scope is prior for prior in scopes):
            raise PermissionError("file approval ancestry is unavailable")
        scope.assert_current()
        if _capture_identity(scope.envelope.context) != _capture_identity(envelope.context):
            raise PermissionError("file approval capture changed")
        scopes.append(scope)
        scope = scope.parent
    operations = [(item.envelope.contract_id, item.envelope.operation_id) for item in scopes]
    if len(scopes) < 3 or operations[:2] != [EXECUTOR, BROKER] or SAVED not in operations[2:]:
        raise PermissionError("file approval saved ancestry is unavailable")
    root = scopes[-1].envelope.context
    if (
        root.caller_principal.value != invocation.presentation_owner_principal_id
        or root.caller_session_id != invocation.presentation_owner_session_id
    ):
        raise PermissionError("file approval owner changed")
    invocation.assert_current()
    return root


def open_file_tool_approval(
    invocation: Any,
    *,
    effect_status: Mapping[str, Any],
    approval_port: Any,
    window_port: Any,
) -> None:
    """Open only the exact pending request returned by the Host coordinator."""
    if (
        effect_status.get("state") != "approval_pending"
        or not isinstance(effect_status.get("approval_request_id"), str)
        or not isinstance(effect_status.get("effect_id"), str)
        or approval_port is None
        or window_port is None
    ):
        raise PermissionError("file approval is unavailable")
    root = authenticated_file_tool_owner(invocation)
    request_id = effect_status["approval_request_id"]
    # The authoritative port checks the durable record's exact presenter,
    # authenticated session, Profile and capture. No owner is synthesized.
    status = approval_port.get_interactive_approval(
        InteractiveApprovalGetQuery(context=root, request_id=request_id)
    )
    if status.request_id != request_id or status.state != "pending":
        raise PermissionError("file approval request changed")
    authenticated_file_tool_owner(invocation)
    result = window_port.open_authority_approval_window(
        AuthorityApprovalWindowOpenCommand(
            context=root,
            request_id=request_id,
            presentation_owner_principal_id=root.caller_principal.value,
            presentation_owner_session_id=root.caller_session_id,
        )
    )
    if result != {"opened": True, "request_id": request_id}:
        raise PermissionError("file approval window is unavailable")
    authenticated_file_tool_owner(invocation)
