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
    _guidance_depth: int = 0,
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
        result = recovered or {"status": "existing", "turn": claim["turn"]}
        if result["turn"].get("status") == "completed":
            return _drain_guidance(
                store, result, client, guard, track_execution, _guidance_depth,
            )
        return result
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
    except Exception as error:
        terminal_rejection = _terminal_rejection(error)
        if terminal_rejection is not None:
            terminal_code, terminal_details = terminal_rejection
            terminal_message = (
                "More host storage is required before this conversation can start."
                if terminal_code == "PACKVM_HOST_CAPACITY_INSUFFICIENT"
                else "Saved conversation did not complete."
            )
            # The supervisor only marks rejections that provably precede any
            # guest-visible effect (preflight denial or deterministic domain
            # allocation refusal), so no outcome receipt can exist; settle
            # failed with the diagnostic code instead of wedging on waiting.
            details: dict[str, Any] = {
                "phase": "saved_execution_failed",
                "error_code": terminal_code,
                "error": {
                    "code": terminal_code,
                    "message": terminal_message,
                },
                "user_persistence": "not_written",
                "assistant_persistence": "not_written",
            }
            if terminal_details:
                details["capacity"] = terminal_details
            return _settle(
                store,
                record,
                "failed",
                details,
            )
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
    guard()
    try:
        settled = store.settle_saved_from_receipt(
            record["id"],
            input_digest=record["input_digest"],
            result_reference=reference,
        )
    except TurnConflict:
        current = store.get(record["id"])
        if current is None:
            raise ValueError("saved execution lost its durable record")
        return {"status": "reconciliation_required", "turn": current}
    return _drain_guidance(
        store,
        {"status": "completed", "turn": settled},
        client,
        guard,
        track_execution,
        _guidance_depth,
    )


def _drain_guidance(
    store: DurableTurnRuntime,
    result: dict[str, Any],
    client: GlobalContractClient,
    guard: Callable[[], None],
    track_execution: Callable[[str], AbstractContextManager[None]],
    depth: int,
) -> dict[str, Any]:
    """Run bounded, durable guidance children after an acknowledged turn."""

    if depth >= 8:
        return result
    parent_id = str(result["turn"]["id"])
    source_id = parent_id
    parent = store.get(parent_id)
    if parent is None:
        raise ValueError("saved guidance parent is unavailable")
    attempts = 0
    for item in parent.get("guidance", []):
        value = item.get("value")
        if not isinstance(value, Mapping) or value.get("auto_send") is not True:
            continue
        if item.get("status") in {"sent", "failed"}:
            child_id = item.get("followup_turn_id")
            if isinstance(child_id, str):
                child = store.get(child_id)
                if child is not None and child.get("status") == "completed":
                    source_id = _latest_completed_guidance_source(store, child_id)
                    continue
            break
        attempts += 1
        if attempts > 8:
            break
        source = store.get(source_id)
        if source is None or source.get("status") != "completed":
            break
        reference = source.get("result_reference")
        if not isinstance(reference, Mapping):
            break
        guidance_id = str(item.get("id") or "")
        child_id = "steer:" + canonical_digest(
            {"parent_turn_id": parent_id, "guidance_id": guidance_id}
        ).removeprefix("sha256:")
        followup = item.get("followup_input")
        if not isinstance(followup, Mapping):
            followup = {
                "request": {
                    "turn_id": child_id,
                    "conversation_id": parent["conversation_id"],
                    "conversation_revision": reference["conversation_revision"],
                    "content": str(value.get("prompt") or ""),
                }
            }
        guard()
        store.reserve_guidance_followup(
            parent_id, guidance_id, source_id, followup,
        )
        child_result = execute_saved_turn(
            store,
            followup,
            client=client,
            guard=guard,
            track_execution=track_execution,
            _guidance_depth=depth + 1,
        )
        child = child_result.get("turn")
        if not isinstance(child, Mapping):
            break
        store.confirm_guidance_followup(parent_id, guidance_id, child_id)
        if child.get("status") != "completed":
            break
        source_id = _latest_completed_guidance_source(store, child_id)
        refreshed = store.get(parent_id)
        if refreshed is not None:
            result = {"status": "completed", "turn": refreshed}
    return result


def _latest_completed_guidance_source(
    store: DurableTurnRuntime, turn_id: str,
) -> str:
    """Resolve the last receipt-bearing descendant through persisted links."""

    visited: set[str] = set()

    def walk(owner_id: str, source_id: str, depth: int) -> str:
        if depth > 8:
            raise ValueError("saved guidance source depth exceeded")
        if owner_id in visited:
            raise ValueError("saved guidance followup cycle detected")
        visited.add(owner_id)
        owner = store.get(owner_id)
        if owner is None or owner.get("status") != "completed":
            raise ValueError("saved guidance source is not completed")
        for item in owner.get("guidance", []):
            if item.get("status") != "sent":
                continue
            child_id = item.get("followup_turn_id")
            if (
                item.get("followup_source_turn_id") != source_id
                or not isinstance(child_id, str)
            ):
                raise ValueError("saved guidance source chain is invalid")
            child = store.get(child_id)
            if child is None or child.get("status") != "completed":
                raise ValueError("saved guidance source child is not completed")
            source_id = walk(child_id, child_id, depth + 1)
        return source_id

    return walk(turn_id, turn_id, 0)


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
    current = store.get(initial["request"]["turn_id"])
    if (
        not isinstance(current, Mapping)
        or any(
            current.get(key) != record.get(key)
            for key in (
                "id", "request_id", "conversation_id", "conversation_revision",
                "input_digest",
            )
        )
    ):
        raise ValueError("saved lifecycle claim state is unconfirmed")
    if claim["claimed"] and (
        current.get("status") != "running"
        or not any(
            event.get("name") == "turn.running"
            and event.get("details", {}).get("phase")
            == "saved_execution_claimed"
            for event in current.get("events", [])
        )
        or any(
            event.get("name") == "turn.cancellation_requested"
            for event in current.get("events", [])
        )
    ):
        raise ValueError("saved lifecycle claim state is unconfirmed")
    if not claim["claimed"] and current.get("status") not in {
        "queued", "running", "waiting", "completed", "failed", "cancelled",
    }:
        raise ValueError("saved lifecycle claim state is unconfirmed")
    return {"claimed": claim["claimed"], "turn": current}


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


def _terminal_rejection(
    error: BaseException,
) -> tuple[str, dict[str, int]] | None:
    """Return the surfaced code when dispatch provably never reached the guest.

    Only Host-side rejections that precede any guest-visible effect carry a
    ``saved_terminal_error_code`` — the saved preflight denial and a
    deterministic domain-allocation refusal — so no outcome receipt can ever
    exist and the durable record may settle ``failed``.  Unmarked failures
    stay uncertain and keep the fail-closed ``waiting`` state.
    """

    current: BaseException | None = error
    for _ in range(8):
        if current is None:
            return None
        code = getattr(current, "saved_terminal_error_code", None)
        if type(code) is str and 0 < len(code) <= 64:
            raw_details = getattr(current, "saved_terminal_details", None)
            details: dict[str, int] = {}
            if isinstance(raw_details, Mapping):
                required = raw_details.get("required_bytes")
                available = raw_details.get("available_bytes")
                if (
                    set(raw_details) == {"required_bytes", "available_bytes"}
                    and type(required) is int
                    and type(available) is int
                    and required > available >= 0
                ):
                    details = {
                        "required_bytes": required,
                        "available_bytes": available,
                    }
            return code, details
        current = current.__cause__ or current.__context__
    return None


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
    reference = {
        "conversation_id": request["conversation_id"],
        "conversation_revision": revision,
        "user_message_id": user_id,
        "assistant_message_id": assistant_id,
        "outcome_digest": canonical_digest(result),
    }
    return reference


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
        or (
            result.get("reconciliation_required") is False
            and "unknown"
            in {
                result.get("user_persistence"),
                result.get("assistant_persistence"),
            }
        )
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
