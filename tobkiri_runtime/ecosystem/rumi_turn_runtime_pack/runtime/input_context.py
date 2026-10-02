"""Optional public input context at the canonical accepted saved-turn boundary."""

from __future__ import annotations

from typing import Any, Callable, Mapping

from tobkiri_protocol.agent_inbox_v1 import CONTEXT_CONTRACT, INBOX_CONTRACT
from tobkiri_protocol.canonical import canonical_digest
from tobkiri_protocol.saved_conversation import validate_saved_conversation_input

CONVERSATION = "tobkiri.resource.conversation.v1"
CONVERSATION_OP = "rumi_conversation_store_pack.conversation-resource"


def _provider(client: Any, contract: str) -> str | None:
    providers = client.session.provider_metadata(contract)
    if not providers:
        return None
    if len(providers) != 1 or not providers[0].get("operation_id"):
        raise RuntimeError("optional input context provider is ambiguous")
    return str(providers[0]["operation_id"])


def execute_with_input_context(
    payload: Mapping[str, Any],
    *,
    client: Any,
    guard: Callable[[], None],
    execute: Callable[[Mapping[str, Any]], Mapping[str, Any]],
    recover_input: Callable[[Mapping[str, Any]], Mapping[str, Any] | None] = lambda _: None,
    bind_input: Callable[
        [Mapping[str, Any], Mapping[str, Any]], Mapping[str, Any]
    ] = lambda _, value: value,
) -> dict[str, Any]:
    """Derive context before immutable capture and ack only owner acceptance."""
    source = validate_saved_conversation_input(payload)
    initial = validate_saved_conversation_input(payload)
    # Accepted context comes only from the selected captured provider. The
    # finite request schema gives caller input no projection provenance.
    initial["request"].pop("task_context", None)
    request = source["request"]
    guard()
    recovered = recover_input(source)
    if recovered is not None:
        initial = validate_saved_conversation_input(recovered)
    operation = _provider(client, CONTEXT_CONTRACT) if recovered is None else None
    projection = None
    prepare = {
        "operation": "prepare_input_for_conversation",
        "profile_id": client.session.profile_id,
        "conversation_id": request["conversation_id"],
        "input_id": request["turn_id"],
        "boundary": "before_turn",
        "operation_id": "input-"
        + canonical_digest(request["turn_id"]).removeprefix("sha256:")[:24],
    }
    if operation is not None:
        guard()
        projection = dict(client.invoke(CONTEXT_CONTRACT, operation, prepare))
        if projection.get("status") == "received":
            context = projection.get("task_context")
            if context is not None:
                initial["request"]["task_context"] = context
            else:
                initial["request"].pop("task_context", None)
            initial = validate_saved_conversation_input(initial)
        elif projection.get("status") not in {"unconfigured", "paused", "cancelled", "unavailable"}:
            raise RuntimeError("optional input context is unavailable")
        else:
            initial["request"].pop("task_context", None)
    if recovered is None:
        guard()
        bound = validate_saved_conversation_input(bind_input(source, initial))
        if bound != initial:
            # A concurrent first capture won. Its immutable input is the only
            # accepted candidate; this invocation cannot ack its losing batch.
            projection = None
        initial = bound
    outcome = dict(execute(initial))
    context_receipt = {
        "source_input_digest": canonical_digest(source),
        "accepted_input_digest": canonical_digest(initial),
        "task_context_digest": canonical_digest(initial["request"].get("task_context")),
        "delivery_status": "pending"
        if initial["request"].get("task_context")
        else "not_applicable",
    }
    if projection is not None and projection.get("status") == "received":
        context_receipt["delivery_status"] = "pending"
        try:
            guard()
            response = client.invoke(
                CONVERSATION,
                CONVERSATION_OP,
                {
                    "operation": "saved_receipt",
                    "profile_id": client.session.profile_id,
                    "turn_id": request["turn_id"],
                },
            )
            receipt = response.get("receipt")
            if receipt is not None:
                user_id = "message:" + canonical_digest(
                    [
                        request["conversation_id"],
                        request["turn_id"],
                        "user",
                    ]
                ).removeprefix("sha256:")
                if (
                    receipt.get("input_digest") != canonical_digest(initial)
                    or receipt.get("conversation_id") != request["conversation_id"]
                    or receipt.get("turn_id") != request["turn_id"]
                    or receipt.get("initial_revision") != request["conversation_revision"]
                    or receipt.get("user_revision") != request["conversation_revision"] + 1
                    or receipt.get("user_message_id") != user_id
                ):
                    raise ValueError("accepted saved input receipt does not match")
                current = client.invoke(CONTEXT_CONTRACT, operation, prepare)
                if current.get("task_context") != projection.get("task_context"):
                    raise ValueError("prepared input context changed after acceptance")
                events = [
                    entry["id"]
                    for entry in projection.get("instructions", [])
                    if entry.get("input_id") == request["turn_id"]
                ]
                inbox_operation = _provider(client, INBOX_CONTRACT)
                if events and inbox_operation is None:
                    raise RuntimeError("accepted input acknowledgement is unavailable")
                if events:
                    guard()
                    client.invoke(
                        INBOX_CONTRACT,
                        inbox_operation,
                        {
                            "operation": "ack",
                            "profile_id": client.session.profile_id,
                            "plan_id": current["plan"]["id"],
                            "expected_revision": current["plan"]["revision"],
                            "input_id": request["turn_id"],
                            "event_ids": events,
                            "accepted_input": initial,
                            "operation_id": "accepted-"
                            + canonical_digest(request["turn_id"]).removeprefix("sha256:")[:24],
                        },
                    )
                context_receipt["delivery_status"] = "applied"
        except Exception:
            context_receipt["delivery_status"] = "ack_failed"
    return {**outcome, "input_context_receipt": context_receipt}
