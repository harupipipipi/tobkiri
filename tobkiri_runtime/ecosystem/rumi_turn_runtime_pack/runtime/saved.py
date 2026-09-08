"""One-shot saved execution coordination inside the durable turn owner.

This source helper requires a captured client and an invocation guard. It is
not a public Host Function or an HTTP route. Capture wiring, stage persistence,
read-only reconciliation and authenticated stop remain separate requirements.
"""

from __future__ import annotations

from typing import Any, Callable, Mapping

from core_runtime.global_contract_dispatch import GlobalContractClient
from ecosystem.rumi_turn_runtime_pack.runtime.durable import DurableTurnRuntime
from ecosystem.rumi_turn_runtime_pack.runtime.turns import TurnConflict
from tobkiri_protocol.canonical import canonical_digest, canonical_json, strict_loads
from tobkiri_protocol.saved_conversation import (
    SAVED_CONVERSATION_CONTRACT,
    SAVED_CONVERSATION_OPERATION,
    validate_saved_conversation_input,
)

RECEIPT_CONTRACT = "tobkiri.resource.conversation.v1"
RECEIPT_OPERATION = "rumi_conversation_store_pack.conversation-resource"
SAVED_CONTRACTS = frozenset({SAVED_CONVERSATION_CONTRACT, RECEIPT_CONTRACT})


def execute_saved_turn(
    store: DurableTurnRuntime,
    payload: Mapping[str, Any],
    *,
    client: GlobalContractClient,
    guard: Callable[[], None],
) -> dict[str, Any]:
    """Dispatch only a claim winner through a captured, restricted client.

    The Host supplies ``guard`` to check the original deadline, cancellation
    and capture validity. Nested dispatch must inherit that same invocation.
    Neither dependency comes from request data. A claim is scheduling state,
    not permission to bypass the client's Authority/Broker checks.
    """
    initial = validate_saved_conversation_input(payload)
    if (
        client.consumer_pack_id != "rumi_turn_runtime_pack"
        or client.session.profile_id != store.profile_id
        or client.allowed_contract_ids != SAVED_CONTRACTS
        or client.host_credential_transport is not None
    ):
        raise PermissionError("saved execution client does not match the owner")
    guard()
    claim = store.claim_saved(initial)
    if not claim["claimed"]:
        # A running snapshot may still have a live executor. Do not rewrite it
        # as terminal or treat a repeat request as a restart/recovery signal.
        recovered = _reconcile(store, claim["turn"], client, guard)
        return recovered or {"status": "existing", "turn": claim["turn"]}
    record = claim["turn"]
    try:
        guard()
        outcome = client.invoke(
            SAVED_CONVERSATION_CONTRACT, SAVED_CONVERSATION_OPERATION, initial
        )
        guard()
        reference = _completed_reference(initial["request"], outcome)
    except Exception:
        # Dispatch may have committed effects before raising or losing its
        # reply. Never retry it or expose provider/parser exception contents.
        return _settle(store, record, "waiting", {
            "phase": "reconciliation_required",
            "reason": "saved_execution_outcome_unconfirmed",
        })
    return _settle(store, record, "completed", {"result_reference": reference})


def _reconcile(
    store: DurableTurnRuntime, record: Mapping[str, Any],
    client: GlobalContractClient, guard: Callable[[], None],
) -> dict[str, Any] | None:
    if record["status"] not in {"running", "waiting"}:
        return None
    guard()
    response = client.invoke(RECEIPT_CONTRACT, RECEIPT_OPERATION, {
        "profile_id": store.profile_id, "operation": "saved_receipt",
        "turn_id": record["id"],
    })
    guard()
    encoded = canonical_json(response)
    if len(encoded) > 8192:
        raise ValueError("saved receipt exceeds byte limit")
    receipt = strict_loads(encoded).get("receipt")
    if receipt is None or isinstance(receipt, dict) and "result_reference" not in receipt:
        return None
    if not isinstance(receipt, dict) or set(receipt) != {
        "turn_id", "conversation_id", "input_digest", "initial_revision",
        "user_message_id", "assistant_message_id", "user_revision", "result_reference",
    } or any(receipt[key] != value for key, value in {
        "turn_id": record["id"], "conversation_id": record["conversation_id"],
        "input_digest": record["input_digest"],
        "initial_revision": record["conversation_revision"],
    }.items()):
        raise ValueError("saved receipt input identity is invalid")
    reference = receipt["result_reference"]
    user_id, assistant_id = (
        "message:" + canonical_digest([
            record["conversation_id"], record["id"], role,
        ]).removeprefix("sha256:")
        for role in ("user", "assistant")
    )
    if (
        not isinstance(reference, dict)
        or set(reference) != {
            "conversation_id", "conversation_revision", "user_message_id",
            "assistant_message_id", "outcome_digest",
        }
        or reference["conversation_id"] != record["conversation_id"]
        or reference["user_message_id"] != user_id or receipt["user_message_id"] != user_id
        or reference["assistant_message_id"] != assistant_id
        or receipt["assistant_message_id"] != assistant_id
        or type(receipt["user_revision"]) is not int
        or receipt["user_revision"] != record["conversation_revision"] + 1
        or type(reference["conversation_revision"]) is not int
        or reference["conversation_revision"] != receipt["user_revision"] + 1
        or not isinstance(reference["outcome_digest"], str)
        or len(reference["outcome_digest"]) != 71
        or not reference["outcome_digest"].startswith("sha256:")
        or any(char not in "0123456789abcdef" for char in reference["outcome_digest"][7:])
    ):
        raise ValueError("saved receipt result identity is invalid")
    try:
        guard()
        result = store.reconcile_saved(
            record["id"], expected_revision=record["revision"],
            input_digest=record["input_digest"], result_reference=reference,
        )
    except TurnConflict:
        return {"status": "reconciliation_required", "turn": store.get(record["id"])}
    return {"status": "completed", "turn": result}


def _settle(
    store: DurableTurnRuntime,
    claimed: Mapping[str, Any],
    status: str,
    details: Mapping[str, Any],
) -> dict[str, Any]:
    try:
        record = store.mutate(
            "transition", claimed["id"], expected_revision=claimed["revision"],
            status=status, details=details,
        )
    except TurnConflict:
        # Guidance/cancellation can change the revision while dispatch runs.
        # Do not overwrite the newer state with a stale completion or retry CAS.
        current = store.get(claimed["id"])
        if current is None:
            raise ValueError("saved execution lost its durable record")
        if (
            status == "completed" and current["status"] == "completed"
            and current.get("result_reference") == details.get("result_reference")
        ):
            return {"status": "completed", "turn": current}
        return {"status": "reconciliation_required", "turn": current}
    return {
        "status": "completed" if status == "completed" else "reconciliation_required",
        "turn": record,
    }


def _completed_reference(request: Mapping[str, Any], value: Any) -> dict[str, Any]:
    encoded = canonical_json(value)
    if len(encoded) > 512 * 1024:
        raise ValueError("saved result exceeds byte limit")
    result = strict_loads(encoded)
    user_id, assistant_id = (
        "message:" + canonical_digest([
            request["conversation_id"], request["turn_id"], role,
        ]).removeprefix("sha256:")
        for role in ("user", "assistant")
    )
    if not isinstance(result, dict) or set(result) != {
        "status", "turn_id", "conversation_id", "conversation_revision",
        "user_message_id", "message",
    }:
        raise ValueError("saved result is not a complete acknowledgement")
    message = result["message"]
    revision = result["conversation_revision"]
    if (
        result["status"] != "ok"
        or result["turn_id"] != request["turn_id"]
        or result["conversation_id"] != request["conversation_id"]
        or result["user_message_id"] != user_id
        or type(revision) is not int
        or revision <= request["conversation_revision"]
        or not isinstance(message, dict)
        or message.get("id") != assistant_id
        or message.get("parent_id") != user_id
        or message.get("role") != "assistant"
        or message.get("status") != "complete"
        or message.get("metadata") != {"turn_id": request["turn_id"]}
        or not isinstance(message.get("content"), (str, list))
        or not message["content"]
    ):
        raise ValueError("saved acknowledgement identity is invalid")
    # Conversation content remains in its owner. Retain identity and a digest
    # of the acknowledged outcome, not another copy of the private transcript.
    return {
        "conversation_id": request["conversation_id"],
        "conversation_revision": revision,
        "user_message_id": user_id,
        "assistant_message_id": assistant_id,
        "outcome_digest": canonical_digest(result),
    }
