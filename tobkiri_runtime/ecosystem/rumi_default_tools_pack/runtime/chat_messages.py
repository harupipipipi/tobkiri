"""Finite source-bound native approval for messages to Tobkiri chats."""

from __future__ import annotations

import json
import re
import time
import uuid
from threading import BoundedSemaphore
from typing import Any, Mapping

from core_runtime.host_provider_function_v4 import (
    HostFunction,
    SingleOperationHostFactoryV4,
)
from core_runtime.owned_chat_message_approval_v4 import (
    assert_chat_message_request_live,
    bind_chat_message_execute_authority,
    authenticated_chat_message_source,
    authenticated_chat_message_tool_owner,
    close_chat_message_tool_request,
    open_chat_message_tool_approval,
    register_chat_message_tool_request,
    record_chat_message_receipt,
    take_chat_message_receipt,
)
from tobkiri_protocol.chat_message_v1 import (
    assert_current_recipients,
    assert_interrupt_targets,
    create_plan,
    message_arguments,
    validate_execute_payload,
    validate_request,
)

PACK = "rumi_default_tools_pack"
CONTRACT = "tobkiri.service.chat.message.send.v1"
PREPARE = f"{PACK}.chat-message-prepare"
EXECUTE = f"{PACK}.chat-message-send"
LOCAL_CONTRACT = "tobkiri.service.tool.local.operation.v1"
LOCAL_OPERATION = f"{PACK}.chat-message-operation"
LOCAL_FUNCTION = f"{PACK}.chat-message-tool"
REFERENCE = "tobkiri.resource.chat.reference.v1"
TURN_RESOURCE = "tobkiri.resource.turn.v1"
TURN_RESOURCE_OPERATION = "rumi_turn_runtime_pack.turn-resource"
REFERENCE_OPERATION = "rumi_conversation_store_pack.chat-reference-read"
DELIVERY = "tobkiri.action.chat.message.delivery.v1"
DELIVERY_OPERATION = "rumi_turn_runtime_pack.chat-message-deliver"
EFFECT = "tobkiri.service.interactive-effect.v1"
EFFECT_OPERATION = "interactive_effect.manage"


def _snapshot(request: Mapping[str, Any], client: Any) -> Mapping[str, Any]:
    result = client.invoke(
        REFERENCE,
        REFERENCE_OPERATION,
        {
            "profile_id": request["profile_id"],
            "operation": "resolve",
            "references": [
                {"kind": request["target_kind"], "id": request["target_id"]}
            ],
        },
    )
    if not isinstance(result, Mapping):
        raise PermissionError("message recipients are unavailable")
    return result


def _assert_candidate_membership(
    request: Mapping[str, Any], snapshot: Mapping[str, Any], invocation: Any
) -> None:
    """Fence a private scheduling admission before native effect decisions."""
    from tobkiri_host.active_chat_source import assert_admission_membership
    from tobkiri_host.finite_chat_dispatch import _current

    token = _current.get()
    if token is None or token.candidate_witness is None:
        return
    invocation.assert_current()
    if not token.active:
        raise PermissionError("finite candidate source has ended")
    if request["target_kind"] != "group":
        return
    assert_admission_membership(request, snapshot, token.candidate_witness)
    invocation.assert_current()


def _target_states(
    request: Mapping[str, Any], recipient_ids: list[str], invocation: Any, client: Any
) -> list[dict[str, Any]]:
    if request["delivery"] != "interrupt":
        return []
    states = []
    for recipient in recipient_ids:
        invocation.assert_current()
        assert_chat_message_request_live(request, invocation.envelope.context)
        state = client.invoke(
            TURN_RESOURCE,
            TURN_RESOURCE_OPERATION,
            {
                "profile_id": request["profile_id"],
                "operation": "delivery_state",
                "conversation_id": recipient,
            },
        )
        if not isinstance(state, Mapping):
            raise PermissionError("interrupt target state is unavailable")
        states.append(dict(state))
    return states


def _bind_prepare(context: Any) -> HostFunction:
    def invoke(payload: Mapping[str, Any], invocation: Any) -> Mapping[str, Any]:
        invocation.assert_current()
        request = validate_request(payload)
        assert_chat_message_request_live(request, invocation.envelope.context)
        if request["profile_id"] != invocation.envelope.context.profile_id:
            raise PermissionError("message profile changed")
        client = invocation.contract_client(
            allowed_contract_ids=frozenset({REFERENCE, TURN_RESOURCE}),
            consumer_pack_id=PACK,
            include_credentials=False,
        )
        snapshot = _snapshot(request, client)
        _assert_candidate_membership(request, snapshot, invocation)
        ids = [
            item
            for item in snapshot["references"][0]["conversation_ids"]
            if item != request["source_conversation_id"]
        ]
        return create_plan(
            request, snapshot, _target_states(request, ids, invocation, client)
        )

    return invoke


def _bind_execute(context: Any) -> HostFunction:
    def invoke(payload: Mapping[str, Any], invocation: Any) -> Mapping[str, Any]:
        invocation.assert_current()
        if set(payload) != {"request", "plan"}:
            raise ValueError("message execute payload is invalid")
        bound = validate_execute_payload(payload["request"], payload["plan"])
        request = bound["request"]
        assert_chat_message_request_live(request, invocation.envelope.context)
        client = invocation.contract_client(
            allowed_contract_ids=frozenset({REFERENCE, TURN_RESOURCE, DELIVERY}),
            consumer_pack_id=PACK,
            include_credentials=False,
        )
        snapshot = _snapshot(request, client)
        _assert_candidate_membership(request, snapshot, invocation)
        assert_current_recipients(request, bound["plan"], snapshot)
        assert_interrupt_targets(
            bound["plan"]["target_states"],
            _target_states(request, bound["plan"]["recipient_ids"], invocation, client),
        )
        bind_chat_message_execute_authority(request, bound["plan"], invocation)
        invocation.assert_current()
        assert_chat_message_request_live(request, invocation.envelope.context)
        receipt = client.invoke(DELIVERY, DELIVERY_OPERATION, bound)
        if (
            not isinstance(receipt, Mapping)
            or receipt.get("version") != "tobkiri.chat.message-delivery.v1"
            or receipt.get("plan_digest") != bound["plan"]["plan_digest"]
            or receipt.get("status")
            not in {"completed", "queued", "partial", "reconciliation_required"}
            or not isinstance(receipt.get("deliveries"), (list, tuple))
            or len(receipt["deliveries"]) != len(bound["plan"]["recipient_ids"])
            or [
                item.get("target_conversation_id")
                for item in receipt["deliveries"]
                if isinstance(item, Mapping)
            ]
            != bound["plan"]["recipient_ids"]
        ):
            raise RuntimeError("message delivery receipt is unavailable")
        record_chat_message_receipt(request, invocation.envelope.context, receipt)
        return dict(receipt)

    return invoke


def _bind_tool(context: Any) -> HostFunction:
    if (
        context.interactive_approval_port is None
        or context.authority_approval_window_port is None
    ):
        raise PermissionError("message native approval is unavailable")
    single_flight = BoundedSemaphore(1)

    def run(payload: Mapping[str, Any], invocation: Any) -> Mapping[str, Any]:
        if (
            set(payload) != {"tool_id", "tool_call_id", "arguments"}
            or payload["tool_id"] != "chat_send_message"
        ):
            raise ValueError("message tool payload is invalid")
        call_id = payload["tool_call_id"]
        if (
            not isinstance(call_id, str)
            or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}", call_id) is None
        ):
            raise ValueError("message tool call identity is invalid")
        arguments = message_arguments(payload["arguments"])
        owner = authenticated_chat_message_tool_owner(invocation)
        request = register_chat_message_tool_request(
            {
                **arguments,
                **authenticated_chat_message_source(invocation),
                "profile_id": owner.profile_id,
                "tool_call_id": call_id,
            },
            invocation,
        )
        client = invocation.contract_client(
            allowed_contract_ids=frozenset({EFFECT}),
            consumer_pack_id=PACK,
            include_credentials=False,
        )
        effect_id = None
        try:
            effect = client.invoke(
                EFFECT,
                EFFECT_OPERATION,
                {
                    "phase": "prepare",
                    "effect_kind": "chat_message_send",
                    "request": request,
                    "correlation_id": str(uuid.uuid4()),
                },
            )
            effect_id = effect.get("effect_id")
            if not isinstance(effect_id, str):
                raise PermissionError("message approval is unavailable")
            open_chat_message_tool_approval(
                invocation,
                effect_status=effect,
                approval_port=context.interactive_approval_port,
                window_port=context.authority_approval_window_port,
            )
            end = time.monotonic() + 90
            resumed = False
            while time.monotonic() < end:
                invocation.assert_current()
                effect = client.invoke(
                    EFFECT,
                    EFFECT_OPERATION,
                    {"phase": "status", "effect_id": effect_id},
                )
                if effect.get("effect_id") != effect_id:
                    raise PermissionError("message approval changed")
                state = effect.get("state")
                if state == "approved" and not resumed:
                    resumed = True
                    effect = client.invoke(
                        EFFECT,
                        EFFECT_OPERATION,
                        {"phase": "resume", "effect_id": effect_id},
                    )
                    if effect.get("effect_id") != effect_id:
                        raise PermissionError("message approval changed")
                    state = effect.get("state")
                if state == "succeeded" and resumed:
                    receipt = take_chat_message_receipt(
                        request, invocation.envelope.context
                    )
                    return {
                        "result": json.dumps(
                            receipt, ensure_ascii=False, sort_keys=True
                        ),
                        "is_error": False,
                        "widget": None,
                    }
                if state not in {
                    "approval_pending",
                    "approved",
                    "claimed",
                    "dispatched",
                }:
                    raise PermissionError("message was not approved and completed")
                time.sleep(0.25)
            raise PermissionError("message approval wait expired")
        except BaseException:
            if effect_id is not None:
                try:
                    client.invoke(
                        EFFECT,
                        EFFECT_OPERATION,
                        {"phase": "cancel", "effect_id": effect_id},
                    )
                except Exception:
                    pass
            raise
        finally:
            close_chat_message_tool_request(request["invocation_key"])

    def invoke(payload: Mapping[str, Any], invocation: Any) -> Mapping[str, Any]:
        if not single_flight.acquire(blocking=False):
            raise PermissionError("message approval is already pending")
        try:
            return run(payload, invocation)
        finally:
            single_flight.release()

    return invoke


HOST_PROVIDER_FACTORY = {
    f"{PACK}.chat-message-prepare.service": SingleOperationHostFactoryV4(
        f"{PACK}.chat-message-prepare.service",
        CONTRACT,
        PREPARE,
        _bind_prepare,
    ),
    f"{PACK}.chat-message-send.service": SingleOperationHostFactoryV4(
        f"{PACK}.chat-message-send.service",
        CONTRACT,
        EXECUTE,
        _bind_execute,
    ),
    LOCAL_FUNCTION: SingleOperationHostFactoryV4(
        LOCAL_FUNCTION,
        LOCAL_CONTRACT,
        LOCAL_OPERATION,
        _bind_tool,
    ),
}
