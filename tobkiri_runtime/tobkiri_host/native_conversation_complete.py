"""One-shot Host exchange for the original signed Conversation operation."""

from __future__ import annotations

import secrets
from collections.abc import Callable, Mapping
from typing import Any

from core_runtime.native_provider_prepare_ipc import (
    CONVERSATION_COMPLETE_MINIMUM_MEMORY,
    ConversationCompletePhase,
)
from tobkiri_protocol.canonical import canonical_digest, canonical_json
from tobkiri_protocol.packvm_data_wire import (
    INVOKE_RESULT_KIND,
    decode_invoke_outcome,
    encode_invoke_payload,
)

from .broker import NestedCancellationProof, RequestEnvelope
from .errors import BackendUnavailableError
from .native_wasm_invocation import NativeWasmInvocationBackend

CONVERSATION_COMPLETE = ("conversation.turn.v1", "complete")
CONVERSATION_GENERATE = (
    "tobkiri.service.ai.generate.v1",
    "rumi_ai_gateway_pack.ai-gateway.generate",
)
_BRIDGE_PROTOCOL = "io.tobkiri.packvm.bridge.v1"
_MAX_BRIDGE_REQUEST_BYTES = 64 * 1024
_MAX_BRIDGE_RESULT_BYTES = 512 * 1024


class NativeConversationCompleteBackend(NativeWasmInvocationBackend):
    """Run one managed Wasm exchange after enrolling an exact Broker reservation."""

    backend_id = "tobkiri.native-conversation-complete-v1"
    memory_reservation_bytes = CONVERSATION_COMPLETE_MINIMUM_MEMORY


def run_conversation_complete(
    envelope: RequestEnvelope,
    payload: dict[str, Any],
    execute: Callable[
        [ConversationCompletePhase, Mapping[str, Any], Callable[[], None]],
        dict[str, Any],
    ],
    dispatch: Callable[
        [RequestEnvelope, Mapping[str, Any], NestedCancellationProof],
        Mapping[str, Any],
    ],
    proof: NestedCancellationProof,
    assert_current: Callable[[], None],
) -> dict[str, Any]:
    """Prepare one source-bound request, dispatch it once, and finish once."""
    def guard() -> None:
        proof.validate_parent(envelope.cancellation_requested)
        assert_current()

    guard()
    # Retain the ordinary Pack request codec limits. The component's strict JSON
    # input may further reject data that requires the extended PackVM codec.
    encode_invoke_payload(payload)
    expected = {
        "messages": payload["messages"],
        "requirements": {"request_surface": "defaultspack.conversation"},
    }
    if "model" in payload:
        expected["model_reference"] = payload["model"]
    expected_digest = canonical_digest(expected)
    request = execute("prepare", payload, guard)
    guard()
    if canonical_digest(request) != expected_digest:
        raise BackendUnavailableError("native Conversation prepared request changed")

    target = {
        "contract_id": CONVERSATION_GENERATE[0],
        "operation_id": CONVERSATION_GENERATE[1],
    }
    nonce = secrets.token_hex(24)
    continuation = {
        "kind": "tobkiri.packvm.continuation.v1",
        "protocol": _BRIDGE_PROTOCOL,
        "version": 1,
        "operation_id": CONVERSATION_COMPLETE[1],
        "nonce": nonce,
        "target": target,
        "request_digest": expected_digest,
    }
    bridge_request = {
        "kind": "tobkiri.packvm.bridge.request.v1",
        "protocol": _BRIDGE_PROTOCOL,
        "version": 1,
        "target": target,
        "request": request,
        "request_digest": expected_digest,
        "continuation": continuation,
    }
    if len(canonical_json(bridge_request)) > _MAX_BRIDGE_REQUEST_BYTES:
        raise ValueError("native Conversation bridge request exceeds the size limit")
    guard()
    # The reservation is already consumed. Nothing from the guest can reenter
    # this step, and any failure after dispatch terminates this invocation.
    response = dict(dispatch(envelope, bridge_request, proof))
    guard()
    result = response.get("result")
    if (
        not isinstance(result, dict)
        or response != {
            **continuation,
            "kind": "tobkiri.packvm.bridge.result.v1",
            "result": result,
            "result_digest": canonical_digest(result),
        }
        or len(canonical_json(response)) > _MAX_BRIDGE_RESULT_BYTES
    ):
        raise BackendUnavailableError("native Conversation bridge result changed")
    terminal = execute("finish", result, guard)
    guard()
    return decode_invoke_outcome({"kind": INVOKE_RESULT_KIND, "outcome": terminal})
