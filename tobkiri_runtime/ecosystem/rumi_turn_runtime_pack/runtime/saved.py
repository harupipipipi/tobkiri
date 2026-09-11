"""One-shot saved execution coordination inside the durable turn owner.

This source helper requires a captured client and an invocation guard. It is
not a public Host Function or an HTTP route. Capture wiring, stage persistence,
read-only reconciliation and authenticated stop remain separate requirements.
"""

from __future__ import annotations

from contextlib import AbstractContextManager, nullcontext
from typing import Any, Callable, Mapping

from core_runtime.global_contract_dispatch import GlobalContractClient
from ecosystem.rumi_turn_runtime_pack.runtime.durable import DurableTurnRuntime
from ecosystem.rumi_turn_runtime_pack.runtime.turns import TurnConflict
from tobkiri_protocol.canonical import canonical_digest, canonical_json, strict_loads
from tobkiri_protocol.saved_conversation import (
    SAVED_CONVERSATION_CONTRACT,
    SAVED_CONVERSATION_OPERATION,
    is_saved_text_content,
    validate_saved_conversation_context,
    validate_saved_conversation_input,
)
from tobkiri_protocol.saved_tools import saved_tool_logs, saved_tool_messages
from tobkiri_protocol.saved_context import (
    PROMPT_TARGET,
    resolved_saved_prompt,
    saved_prompt_reference,
)

RECEIPT_CONTRACT = "tobkiri.resource.conversation.v1"
RECEIPT_OPERATION = "rumi_conversation_store_pack.conversation-resource"
LIFECYCLE_CONTRACT = "tobkiri.action.turn.lifecycle.v1"
LIFECYCLE_OPERATION = "rumi_turn_runtime_pack.turn-lifecycle"
SAVED_CONTRACTS = frozenset(
    {
        SAVED_CONVERSATION_CONTRACT,
        RECEIPT_CONTRACT,
        PROMPT_TARGET[0],
        LIFECYCLE_CONTRACT,
    }
)


def reconcile_saved_turn(
    store: DurableTurnRuntime,
    turn_id: str,
    *,
    client: GlobalContractClient,
    guard: Callable[[], None],
) -> dict[str, Any]:
    """Reconcile existing state using an owner-reader-only captured client.

    No initial input, claim, AI call or message append is available here.
    An absent turn remains absent even when a client repeats this operation.
    """
    if (
        client.consumer_pack_id != "rumi_turn_runtime_pack"
        or client.session.profile_id != store.profile_id
        or client.allowed_contract_ids != frozenset({RECEIPT_CONTRACT})
        or client.host_credential_transport is not None
    ):
        raise PermissionError("saved reconciliation requires an owner-reader-only client")
    guard()
    record = store.get(turn_id)
    if record is None:
        raise KeyError("saved turn is unavailable")
    if not record.get("input_digest") or not record["request_id"].startswith("saved-turn."):
        raise PermissionError("only saved turns can be reconciled")
    recovered = _reconcile(store, record, client, guard)
    return recovered or {"status": "existing", "turn": record}


def execute_saved_turn(
    store: DurableTurnRuntime,
    payload: Mapping[str, Any],
    *,
    client: GlobalContractClient,
    guard: Callable[[], None],
    track_execution: Callable[[str], AbstractContextManager[None]] = lambda _: nullcontext(),
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
    if store.get(initial["request"]["turn_id"]) is None:
        # Existing turns must remain reconcilable after the conversation changes.
        # Only new execution reads context before claiming any durable work.
        response = client.invoke(
            RECEIPT_CONTRACT,
            RECEIPT_OPERATION,
            {
                "profile_id": store.profile_id,
                "operation": "get",
                "conversation_id": initial["request"]["conversation_id"],
            },
        )
        guard()
        conversation = response.get("conversation")
        if (
            not isinstance(conversation, Mapping)
            or conversation.get("id") != initial["request"]["conversation_id"]
        ):
            raise ValueError("saved conversation owner response is invalid")
        validate_saved_conversation_context(conversation)
        prompt_id = saved_prompt_reference(conversation)
        if prompt_id is not None:
            prompt = client.invoke(*PROMPT_TARGET, {"operation": "get", "prompt_id": prompt_id})
            guard()
            resolved_saved_prompt(prompt, prompt_id=prompt_id, profile_id=store.profile_id)
        revision = conversation.get("conversation_revision")
        if type(revision) is not int or revision != initial["request"]["conversation_revision"]:
            raise ValueError("saved conversation revision changed before execution")
        model = conversation.get("model_reference")
        if not isinstance(model, str) or not model.strip():
            raise ValueError("saved conversation model reference is required")
    guard()
    begun = client.invoke(
        LIFECYCLE_CONTRACT,
        LIFECYCLE_OPERATION,
        {
            "profile_id": store.profile_id,
            "operation": "begin_saved",
            **initial,
        },
    )
    guard()
    _validate_begun_turn(store.profile_id, initial, begun)
    guard()
    claim = client.invoke(
        LIFECYCLE_CONTRACT,
        LIFECYCLE_OPERATION,
        {
            "profile_id": store.profile_id,
            "operation": "claim_saved",
            **initial,
        },
    )
    guard()
    claim = _validate_saved_claim(store, initial, claim)
    if not claim["claimed"]:
        # A running snapshot may still have a live executor. Do not rewrite it
        # as terminal or treat a repeat request as a restart/recovery signal.
        recovered = _reconcile(store, claim["turn"], client, guard)
        return recovered or {"status": "existing", "turn": claim["turn"]}
    record = claim["turn"]
    try:
        with track_execution(record["id"]):
            guard()
            outcome = client.invoke(
                SAVED_CONVERSATION_CONTRACT, SAVED_CONVERSATION_OPERATION, initial
            )
            guard()
            failure = _failure_settlement(initial["request"], outcome)
            if failure is None:
                reference = _completed_reference(initial["request"], outcome)
    except Exception:
        # Dispatch may have committed effects before raising or losing its
        # reply. Never retry it or expose provider/parser exception contents.
        return _settle(
            store,
            record,
            "waiting",
            {
                "phase": "reconciliation_required",
                "reason": "saved_execution_outcome_unconfirmed",
            },
        )
    if failure is not None:
        return _settle(store, record, *failure)
    return _settle(store, record, "completed", {"result_reference": reference})


def _validate_begun_turn(
    profile_id: str,
    initial: Mapping[str, Any],
    record: object,
) -> None:
    """Require the lifecycle owner to bind the exact saved-send identity."""
    request = initial["request"]
    request_id = "saved-turn." + canonical_digest(
        {"profile_id": profile_id, "turn_id": request["turn_id"]}
    ).removeprefix("sha256:")
    expected = {
        "id": request["turn_id"],
        "request_id": request_id,
        "conversation_id": request["conversation_id"],
        "conversation_revision": request["conversation_revision"],
        "input_digest": canonical_digest(initial),
    }
    if not isinstance(record, Mapping) or any(
        record.get(key) != value for key, value in expected.items()
    ):
        raise ValueError("saved lifecycle owner response is invalid")


def _validate_saved_claim(
    store: DurableTurnRuntime,
    initial: Mapping[str, Any],
    claim: object,
) -> Mapping[str, Any]:
    """Accept only the exact claim state committed by the lifecycle owner."""
    if (
        not isinstance(claim, Mapping)
        or set(claim) != {"claimed", "turn"}
        or type(claim["claimed"]) is not bool
        or not isinstance(claim["turn"], Mapping)
    ):
        raise ValueError("saved lifecycle claim response is invalid")
    record = claim["turn"]
    _validate_begun_turn(store.profile_id, initial, record)
    if claim["claimed"] and record.get("status") != "running":
        raise ValueError("saved lifecycle claim response is invalid")
    if store.get(initial["request"]["turn_id"]) != record:
        raise ValueError("saved lifecycle claim state is unconfirmed")
    return claim


def _reconcile(
    store: DurableTurnRuntime,
    record: Mapping[str, Any],
    client: GlobalContractClient,
    guard: Callable[[], None],
) -> dict[str, Any] | None:
    if record["status"] not in {"running", "waiting"}:
        return None
    guard()
    response = client.invoke(
        RECEIPT_CONTRACT,
        RECEIPT_OPERATION,
        {
            "profile_id": store.profile_id,
            "operation": "saved_receipt",
            "turn_id": record["id"],
        },
    )
    guard()
    encoded = canonical_json(response)
    if len(encoded) > 8192:
        raise ValueError("saved receipt exceeds byte limit")
    decoded = strict_loads(encoded)
    if not isinstance(decoded, dict) or set(decoded) != {"receipt"}:
        raise ValueError("saved receipt response is invalid")
    receipt = decoded["receipt"]
    if receipt is None or isinstance(receipt, dict) and "result_reference" not in receipt:
        return None
    if (
        not isinstance(receipt, dict)
        or set(receipt)
        != {
            "turn_id",
            "conversation_id",
            "input_digest",
            "initial_revision",
            "user_message_id",
            "assistant_message_id",
            "user_revision",
            "result_reference",
        }
        or any(
            receipt[key] != value
            for key, value in {
                "turn_id": record["id"],
                "conversation_id": record["conversation_id"],
                "input_digest": record["input_digest"],
                "initial_revision": record["conversation_revision"],
            }.items()
        )
    ):
        raise ValueError("saved receipt input identity is invalid")
    reference = receipt["result_reference"]
    user_id, assistant_id = (
        "message:"
        + canonical_digest(
            [
                record["conversation_id"],
                record["id"],
                role,
            ]
        ).removeprefix("sha256:")
        for role in ("user", "assistant")
    )
    if (
        not isinstance(reference, dict)
        or set(reference)
        != {
            "conversation_id",
            "conversation_revision",
            "user_message_id",
            "assistant_message_id",
            "outcome_digest",
        }
        or reference["conversation_id"] != record["conversation_id"]
        or reference["user_message_id"] != user_id
        or receipt["user_message_id"] != user_id
        or reference["assistant_message_id"] != assistant_id
        or receipt["assistant_message_id"] != assistant_id
        or type(receipt["initial_revision"]) is not int
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
            record["id"],
            expected_revision=record["revision"],
            input_digest=record["input_digest"],
            result_reference=reference,
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
            "transition",
            claimed["id"],
            expected_revision=claimed["revision"],
            status=status,
            details=details,
        )
    except TurnConflict:
        # Guidance/cancellation can change the revision while dispatch runs.
        # Do not overwrite the newer state with a stale completion or retry CAS.
        current = store.get(claimed["id"])
        if current is None:
            raise ValueError("saved execution lost its durable record")
        if (
            status == "completed"
            and current["status"] == "completed"
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
        "message:"
        + canonical_digest(
            [
                request["conversation_id"],
                request["turn_id"],
                role,
            ]
        ).removeprefix("sha256:")
        for role in ("user", "assistant")
    )
    if not isinstance(result, dict) or set(result) != {
        "status",
        "turn_id",
        "conversation_id",
        "conversation_revision",
        "user_message_id",
        "message",
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
        or revision != request["conversation_revision"] + 2
        or not isinstance(message, dict)
        or message.get("id") != assistant_id
        or message.get("parent_id") != user_id
        or message.get("role") != "assistant"
        or message.get("status") != "complete"
        or not is_saved_text_content(message.get("content"))
    ):
        raise ValueError("saved acknowledgement identity is invalid")
    metadata = message.get("metadata")
    if not isinstance(metadata, dict):
        raise ValueError("saved acknowledgement metadata is invalid")
    trace = saved_tool_messages(metadata.get("saved_tool_messages", []))
    expected_metadata = {"turn_id": request["turn_id"]}
    selection = request.get("tool_selection", {})
    if trace:
        if selection.get("mode", "none") == "none":
            raise ValueError("saved acknowledgement contains unselected tools")
        expected_metadata["saved_tool_messages"] = trace
    logs = message.get("tool_logs")
    if (
        metadata != expected_metadata
        or (selection.get("must_use") and not trace)
        or canonical_json([] if logs is None else logs) != canonical_json(saved_tool_logs(trace))
    ):
        raise ValueError("saved acknowledgement tool transcript is invalid")
    # Conversation content remains in its owner. Retain identity and a digest
    # of the acknowledged outcome, not another copy of the private transcript.
    return {
        "conversation_id": request["conversation_id"],
        "conversation_revision": revision,
        "user_message_id": user_id,
        "assistant_message_id": assistant_id,
        "outcome_digest": canonical_digest(result),
    }


def _failure_settlement(
    request: Mapping[str, Any], value: Any,
) -> tuple[str, dict[str, Any]] | None:
    """Validate a finite saved failure without treating it as a lost reply."""
    encoded = canonical_json(value)
    if len(encoded) > 512 * 1024:
        raise ValueError("saved result exceeds byte limit")
    result = strict_loads(encoded)
    if not isinstance(result, dict) or result.get("status") != "error":
        return None
    expected_ids = {
        role: "message:"
        + canonical_digest(
            [request["conversation_id"], request["turn_id"], role]
        ).removeprefix("sha256:")
        for role in ("user", "assistant")
    }
    error = result.get("error")
    allowed_codes = {
        "AI_COMPLETION_UNAVAILABLE",
        "CONTEXT_RESOLUTION_REQUIRED",
        "CONVERSATION_REVISION_CONFLICT",
        "HOST_ACTION_FAILED",
        "MODEL_REFERENCE_REQUIRED",
        "OWNER_RESPONSE_INVALID",
        "REQUIRED_TOOL_NOT_USED",
        "TURN_RECONCILIATION_REQUIRED",
    }
    if (
        set(result)
        != {
            "status",
            "error",
            "turn_id",
            "conversation_id",
            "user_message_id",
            "assistant_message_id",
            "user_persistence",
            "assistant_persistence",
            "reconciliation_required",
        }
        or not isinstance(error, dict)
        or set(error) != {"code", "message"}
        or error.get("code") not in allowed_codes
        or error.get("message") != "Saved conversation did not complete."
        or result.get("turn_id") != request["turn_id"]
        or result.get("conversation_id") != request["conversation_id"]
        or result.get("user_message_id") != expected_ids["user"]
        or result.get("assistant_message_id") != expected_ids["assistant"]
        or result.get("user_persistence")
        not in {"not_written", "unknown", "saved"}
        or result.get("assistant_persistence")
        not in {"not_written", "unknown"}
        or type(result.get("reconciliation_required")) is not bool
    ):
        raise ValueError("saved failure acknowledgement is invalid")
    uncertain = result["reconciliation_required"]
    return (
        "waiting" if uncertain else "failed",
        {
            "phase": (
                "reconciliation_required" if uncertain else "saved_execution_failed"
            ),
            "error_code": error["code"],
            "user_persistence": result["user_persistence"],
            "assistant_persistence": result["assistant_persistence"],
        },
    )
