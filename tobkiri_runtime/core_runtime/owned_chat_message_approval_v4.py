"""Present one saved chat_message tool's approval using its live Host ancestry.

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
    "rumi_default_tools_pack.chat-message-operation",
)
EXECUTOR = (
    "tobkiri.service.tool.execute.v1",
    "rumi_tool_local_executor_pack.tool-local-execute",
)
BROKER = ("tobkiri.service.tool.invoke.v1", "rumi_tool_broker_pack.tool-invoke")
SAVED = ("conversation.saved-turn.v1", "saved_complete")
SAVED_CANONICAL = "tobkiri.action.turn.saved.v1"


@dataclass
class _LiveChatMessageRequest:
    digest: str
    capture: tuple[Any, ...]
    guard: Any
    deadline: float
    cancel: Any = None
    plan_digest: str | None = None
    execute_envelope: Any = None
    execute_guard: Any = None
    presenter_principal: str = ""


_live_lock = RLock()
_live_requests: dict[str, _LiveChatMessageRequest] = {}
_chat_message_effects: dict[str, str] = {}


def register_chat_message_tool_request(
    request: Mapping[str, Any], invocation: Any
) -> dict[str, Any]:
    """Retain a guarded Host invocation before any durable effect is created."""
    owner = authenticated_chat_message_tool_owner(invocation)
    source = authenticated_chat_message_source(invocation)
    if request.get("profile_id") != owner.profile_id or any(
        request.get(key) != value for key, value in source.items()
    ):
        raise PermissionError("message source changed")
    key = str(uuid.uuid4())
    bound = {**dict(request), "invocation_key": key}
    record = _LiveChatMessageRequest(
        canonical_digest(bound),
        _capture_identity(invocation.envelope.context),
        lambda: _assert_source_live(invocation, source),
        time.monotonic() + 90,
    )
    record.presenter_principal = owner.caller_principal.value
    with _live_lock:
        _live_requests[key] = record
    return bound


def assert_chat_message_request_live(request: Mapping[str, Any], context: Any) -> None:
    """Require the exact live saved request in addition to Broker authority."""
    key = request.get("invocation_key")
    if not isinstance(key, str):
        raise PermissionError("chat_message create original invocation is unavailable")
    with _live_lock:
        record = _live_requests.get(key)
    if (
        record is None
        or record.capture != _capture_identity(context)
        or record.digest != canonical_digest(dict(request))
        or time.monotonic() >= record.deadline
    ):
        raise PermissionError("chat_message create original invocation is unavailable")
    record.guard()
    with _live_lock:
        if _live_requests.get(key) is not record:
            raise PermissionError("chat_message create original invocation drained")


def bind_chat_message_effect(
    effect_id: str,
    request: Mapping[str, Any],
    context: Any,
    cancel: Any,
    *,
    plan: Mapping[str, Any] | None = None,
) -> None:
    """Bind Host-only cleanup before returning the durable prepare receipt."""
    try:
        assert_chat_message_request_live(request, context)
        with _live_lock:
            record = _live_requests.get(request["invocation_key"])
            if (
                record is None
                or record.cancel is not None
                or effect_id in _chat_message_effects
            ):
                raise PermissionError(
                    "chat_message create effect binding is unavailable"
                )
            if plan is None:
                raise PermissionError("message effect plan binding is unavailable")
            from tobkiri_protocol.chat_message_v1 import validate_execute_payload

            validate_execute_payload(request, plan)
            record.plan_digest = canonical_digest(dict(plan))
            record.cancel = cancel
            _chat_message_effects[effect_id] = request["invocation_key"]
    except BaseException:
        cancel()
        raise


def chat_message_effect_execution_guard(effect_id: str, context: Any) -> Any:
    """Return a fail-closed guard for claim, Broker entry and Provider dispatch."""

    def guard() -> None:
        with _live_lock:
            key = _chat_message_effects.get(effect_id)
            record = _live_requests.get(key) if key is not None else None
        if (
            key is None
            or record is None
            or record.capture != _capture_identity(context)
            or time.monotonic() >= record.deadline
        ):
            raise PermissionError(
                "chat_message create original invocation is unavailable"
            )
        record.guard()
        with _live_lock:
            if _live_requests.get(key) is not record:
                raise PermissionError("chat_message create original invocation drained")

    guard()
    return guard


def close_chat_message_tool_request(key: str) -> None:
    """Drain the private lifetime and cancel via Host state even after Stop."""
    with _live_lock:
        record = _live_requests.pop(key, None)
        _receipts.pop(key, None)
        effects = [
            effect for effect, value in _chat_message_effects.items() if value == key
        ]
        for effect in effects:
            _chat_message_effects.pop(effect, None)
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


def authenticated_chat_message_tool_owner(invocation: Any) -> Any:
    """Return the actual live root context for one finite saved tool call."""
    invocation.assert_current()
    envelope = invocation.envelope
    if (envelope.contract_id, envelope.operation_id) != LOCAL:
        raise PermissionError("chat_message approval invocation is unavailable")
    scopes: list[Any] = []
    scope = invocation.parent_invocation
    while scope is not None:
        if len(scopes) >= 16 or any(scope is prior for prior in scopes):
            raise PermissionError("chat_message approval ancestry is unavailable")
        scope.assert_current()
        if _capture_identity(scope.envelope.context) != _capture_identity(
            envelope.context
        ):
            raise PermissionError("chat_message approval capture changed")
        scopes.append(scope)
        scope = scope.parent
    operations = [
        (item.envelope.contract_id, item.envelope.operation_id) for item in scopes
    ]
    if (
        len(scopes) < 3
        or operations[:2] != [EXECUTOR, BROKER]
        or not any(op == SAVED or op[0] == SAVED_CANONICAL for op in operations[2:])
    ):
        raise PermissionError("chat_message approval saved ancestry is unavailable")
    if any(
        op[0] == "tobkiri.action.chat.message.delivery.v1"
        or op[1] == "rumi_default_tools_pack.chat-message-send"
        for op in operations
    ):
        raise PermissionError("nested incoming message send is unavailable")
    root = scopes[-1].envelope.context
    if (
        root.caller_principal.value != invocation.presentation_owner_principal_id
        or root.caller_session_id != invocation.presentation_owner_session_id
    ):
        raise PermissionError("chat_message approval owner changed")
    invocation.assert_current()
    return root


def open_chat_message_tool_approval(
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
        raise PermissionError("chat_message approval is unavailable")
    root = authenticated_chat_message_tool_owner(invocation)
    request_id = effect_status["approval_request_id"]
    # The authoritative port checks the durable record's exact presenter,
    # authenticated session, Prochat_message and capture. No owner is synthesized.
    status = approval_port.get_interactive_approval(
        InteractiveApprovalGetQuery(context=root, request_id=request_id)
    )
    if status.request_id != request_id or status.state != "pending":
        raise PermissionError("chat_message approval request changed")
    authenticated_chat_message_tool_owner(invocation)
    result = window_port.open_authority_approval_window(
        AuthorityApprovalWindowOpenCommand(
            context=root,
            request_id=request_id,
            presentation_owner_principal_id=root.caller_principal.value,
            presentation_owner_session_id=root.caller_session_id,
        )
    )
    if result != {"opened": True, "request_id": request_id}:
        raise PermissionError("chat_message approval window is unavailable")
    authenticated_chat_message_tool_owner(invocation)


def authenticated_chat_message_source(invocation: Any) -> dict[str, str]:
    """Derive the nearest saved source from authenticated normalized ancestry."""
    authenticated_chat_message_tool_owner(invocation)
    scope = invocation.parent_invocation
    while scope is not None:
        envelope = scope.envelope
        if (
            envelope.contract_id == SAVED_CANONICAL
            or (envelope.contract_id, envelope.operation_id) == SAVED
        ):
            payload = envelope.payload
            request = payload.get("request", payload)
            if not isinstance(request, Mapping):
                raise PermissionError("saved source is unavailable")
            chat = request.get("conversation_id")
            turn = request.get("turn_id")
            if (
                not isinstance(chat, str)
                or not chat
                or not isinstance(turn, str)
                or not turn
            ):
                raise PermissionError("saved source is unavailable")
            return {"source_conversation_id": chat, "source_turn_id": turn}
        scope = scope.parent
    raise PermissionError("saved source is unavailable")


_receipts: dict[str, dict[str, Any]] = {}


def record_chat_message_receipt(
    request: Mapping[str, Any], context: Any, receipt: Mapping[str, Any]
) -> None:
    """Retain actual provider outcome only for the original live tool request."""
    assert_chat_message_request_live(request, context)
    with _live_lock:
        key = request["invocation_key"]
        if key in _receipts:
            raise PermissionError("message receipt already recorded")
        _receipts[key] = dict(receipt)


def take_chat_message_receipt(
    request: Mapping[str, Any], context: Any
) -> dict[str, Any]:
    """Read a complete executed receipt rather than infer delivery from status."""
    assert_chat_message_request_live(request, context)
    with _live_lock:
        receipt = _receipts.pop(request["invocation_key"], None)
    if receipt is None:
        raise PermissionError("message delivery receipt is unavailable")
    return receipt


def _assert_source_live(invocation: Any, expected: Mapping[str, str]) -> None:
    if authenticated_chat_message_source(invocation) != dict(expected):
        raise PermissionError("message saved source changed")


def bind_chat_message_execute_authority(
    request: Mapping[str, Any], plan: Mapping[str, Any], invocation: Any
) -> None:
    """Retain the actual Broker-admitted execute envelope, never an approval flag."""
    invocation.assert_current()
    envelope = invocation.envelope
    if (envelope.contract_id, envelope.operation_id) != (
        "tobkiri.service.chat.message.send.v1",
        "rumi_default_tools_pack.chat-message-send",
    ):
        raise PermissionError("approved message execute ancestry is unavailable")
    assert_chat_message_request_live(request, envelope.context)
    if canonical_digest(dict(envelope.payload)) != canonical_digest(
        {"request": dict(request), "plan": dict(plan)}
    ):
        raise PermissionError("approved message execute payload changed")
    with _live_lock:
        record = _live_requests.get(request["invocation_key"])
        if (
            record is None
            or record.cancel is None
            or record.plan_digest != canonical_digest(dict(plan))
        ):
            raise PermissionError("approved message effect binding is unavailable")
        if (
            record.execute_envelope is not None
            and record.execute_envelope is not envelope
        ):
            raise PermissionError("approved message execute envelope changed")
        record.execute_envelope = envelope
        record.execute_guard = invocation.assert_current


def assert_chat_message_interrupt_authority(
    invocation: Any, state: Mapping[str, Any]
) -> tuple[Any, str]:
    """Prove one exact native-approved interrupt belongs to a live execute tree."""
    invocation.assert_current()
    envelope = invocation.envelope
    if (envelope.contract_id, envelope.operation_id) != (
        "tobkiri.action.chat.message.delivery.v1",
        "rumi_turn_runtime_pack.chat-message-deliver",
    ):
        raise PermissionError("interrupt delivery ancestry is unavailable")
    parent = invocation.parent_invocation
    if parent is None:
        raise PermissionError("interrupt execute parent is unavailable")
    parent.assert_current()
    outer = parent.envelope
    if (outer.contract_id, outer.operation_id) != (
        "tobkiri.service.chat.message.send.v1",
        "rumi_default_tools_pack.chat-message-send",
    ):
        raise PermissionError("interrupt execute parent is unavailable")
    payload = outer.payload
    request, plan = payload.get("request"), payload.get("plan")
    from tobkiri_protocol.chat_message_v1 import (
        validate_execute_payload,
        assert_interrupt_targets,
    )

    validate_execute_payload(request, plan)
    if request["delivery"] != "interrupt" or canonical_digest(
        dict(envelope.payload)
    ) != canonical_digest(dict(payload)):
        raise PermissionError("interrupt request changed")
    assert_chat_message_request_live(request, outer.context)
    with _live_lock:
        record = _live_requests.get(request["invocation_key"])
        if (
            record is None
            or record.execute_envelope is not outer
            or record.plan_digest != canonical_digest(dict(plan))
            or record.execute_guard is None
        ):
            raise PermissionError("interrupt lacks approved Broker execution proof")
        guard = record.execute_guard
    guard()
    approved = next(
        (
            item
            for item in plan["target_states"]
            if item["conversation_id"] == state.get("conversation_id")
        ),
        None,
    )
    if approved is None:
        raise PermissionError("interrupt target was not approved")
    assert_interrupt_targets([approved], [state])
    invocation.assert_current()
    parent.assert_current()
    # Original presenter remains private and was verified at saved-call registration.
    record.guard()
    principal = invocation.presentation_owner_principal_id
    if (
        not isinstance(principal, str)
        or not principal
        or principal != record.presenter_principal
    ):
        raise PermissionError("interrupt presenter is unavailable")
    return outer, principal
