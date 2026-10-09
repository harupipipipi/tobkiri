"""Real captured ancestry authorizes only an executed native effect's target."""

from concurrent.futures import Future
from dataclasses import replace
from types import SimpleNamespace
import time

import pytest

from core_runtime.invocation_scope_v4 import CapturedInvocationScopeV4
from core_runtime import owned_chat_message_approval_v4 as owned
from ecosystem.rumi_turn_runtime_pack.runtime.durable import DurableTurnRuntime
from ecosystem.rumi_turn_runtime_pack.runtime.delivery import (
    begin_interrupt, request_interrupt, confirm_interrupt,
)
from tests.test_operation_cancellation import _envelope, _child_envelope
from tobkiri_host.approved_interrupt import ApprovedInterruptBinding
from tobkiri_host.models import OpaqueAuthorityRef
from tobkiri_host.operation_cancellation import (
    OwnedCancellationHandles, nested_cancellation_proof_for,
)
from tobkiri_protocol.chat_message_v1 import create_plan


def test_captured_native_execute_authority_port_owner_cas_and_ack(tmp_path):
    store = DurableTurnRuntime("defaults", user_data_root=tmp_path)
    source = store.begin({
        "turn_id": "source-turn", "request_id": "source-request",
        "conversation_id": "source", "conversation_revision": 1,
    })
    store.mutate("transition", source["id"], expected_revision=source["revision"], status="running")
    initial = {"request": {
        "turn_id": "target-run", "conversation_id": "target",
        "conversation_revision": 1, "content": "Original target",
    }}
    store.claim_saved(initial)
    state = store.delivery_state("target", conversation_revision=1)
    envelope = _envelope()
    ctx = replace(envelope.context, profile_id="defaults", caller_principal=OpaqueAuthorityRef("owner"), caller_session_id="source-session")

    def captured(operation, payload, parent=None):
        current = replace(
            envelope, context=ctx, contract_id=operation[0], operation_id=operation[1],
            payload=payload,
        )
        return CapturedInvocationScopeV4(current, lambda: None, parent)

    root = captured((owned.SAVED_CANONICAL, "rumi_turn_runtime_pack.turn-saved"), {
        "request": {"conversation_id": "source", "turn_id": "source-turn"},
    })
    broker = captured(owned.BROKER, {}, root)
    executor = captured(owned.EXECUTOR, {}, broker)
    local = captured(owned.LOCAL, {}, executor)
    invocation = SimpleNamespace(
        envelope=local.envelope, parent_invocation=executor,
        assert_current=local.assert_current,
        presentation_owner_principal_id="owner", presentation_owner_session_id="source-session",
    )
    request = owned.register_chat_message_tool_request({
        "target_kind": "chat", "target_id": "target", "content": "Hello",
        "delivery": "interrupt", "source_conversation_id": "source",
        "source_turn_id": "source-turn", "profile_id": "defaults", "tool_call_id": "tool-one",
    }, invocation)
    now = int(time.time() * 1000)
    snapshot = {
        "kind": "tobkiri.chat.reference.snapshot.v1", "profile_id": "defaults",
        "store_revision": 1, "project_revision": 1, "snapshot_time": now,
        "expires_at": now + 90000, "next_cursor": None, "truncated": False,
        "references": [{"kind": "chat", "id": "target", "label": "Target",
                        "conversation_ids": ["target"], "snapshot_digest": "digest",
                        "member_count": 1, "membership_complete": True}],
    }
    plan = create_plan(request, snapshot, [state])
    bound = {"request": request, "plan": plan}
    execute = captured(("tobkiri.service.chat.message.send.v1", "rumi_default_tools_pack.chat-message-send"), bound, local)
    delivery_scope = captured(("tobkiri.action.chat.message.delivery.v1", "rumi_turn_runtime_pack.chat-message-deliver"), bound, execute)
    delivery_invocation = SimpleNamespace(
        envelope=delivery_scope.envelope, parent_invocation=execute,
        assert_current=delivery_scope.assert_current,
        presentation_owner_principal_id="owner", presentation_owner_session_id="source-session",
    )
    registry = OwnedCancellationHandles()
    target_envelope = replace(_envelope(), context=ctx,
                              contract_id="tobkiri.action.turn.saved.v1",
                              operation_id="rumi_turn_runtime_pack.turn-saved", payload=initial)
    target_binding = registry.bind(
        group=("rumi_turn_runtime_pack", "saved-turn"), role="execute",
        envelope=target_envelope, owner_principal="owner", owner_session="target-session",
        guard=lambda: None,
    )
    delivery = {
        "delivery_id": "mode-one", "source": {
            "profile_id": "defaults", "conversation_id": "source",
            "turn_id": "source-turn", "tool_call_id": "tool-one",
        }, "target_conversation_id": "target", "conversation_revision": 1,
        "content": "Hello", "recipient_snapshot": ["target"],
        "delivery": "interrupt", "target_state": state,
    }
    port = ApprovedInterruptBinding(
        registry=registry, envelope=delivery_scope.envelope,
        presentation_owner_principal_id="owner",
        authorize=lambda observed: owned.assert_chat_message_interrupt_authority(delivery_invocation, observed),
        guard=delivery_scope.assert_current,
    )
    try:
        owned.bind_chat_message_effect("captured-effect", request, ctx, lambda: None, plan=plan)
        with target_binding.track("target-run"):
            proof = nested_cancellation_proof_for(target_envelope, "owner", "target-session")
            child_id = proof.reserve_child(replace(
                _child_envelope(target_envelope), context=ctx,
            ))
            future = Future()
            proof.bind_child(child_id, future)
            with pytest.raises(PermissionError, match="execution proof"):
                port.request(state, before_signal=lambda: pytest.fail("pending cannot CAS"))
            assert store.get("target-run")["status"] == "running"
            owned.bind_chat_message_execute_authority(request, plan, SimpleNamespace(
                envelope=execute.envelope, assert_current=execute.assert_current,
            ))
            begin_interrupt(store, delivery)
            observation = port.request(state, before_signal=lambda: request_interrupt(store, delivery))
            assert target_envelope.cancellation_requested.is_set()
            assert not delivery_scope.envelope.cancellation_requested.is_set()
            assert future.cancel()
            proof.record_queued_cancellation(child_id, future)
            proof.record_resource_drain(future)
        assert observation.wait_for_verified_drain(time.monotonic() + 0.1)
        acknowledged = confirm_interrupt(store, delivery)
        assert acknowledged["status"] == "stopped"
        assert store.get("target-run")["status"] == "cancelled"
    finally:
        owned.close_chat_message_tool_request(request["invocation_key"])
