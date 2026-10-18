"""Strict request and sealed recipient plan for approved inter-chat messages."""

from __future__ import annotations

from typing import Any, Mapping
import time

from tobkiri_protocol.canonical import canonical_digest

PLAN_VERSION = "tobkiri.chat.message-plan.v1"
MAX_RECIPIENTS = 16
MAX_CONTENT_BYTES = 16 * 1024


def message_arguments(value: Any) -> dict[str, str]:
    """Validate the only fields an AI may supply to the message tool."""
    if (
        not isinstance(value, Mapping)
        or set(value)
        != {
            "target_kind",
            "target_id",
            "content",
        }
        and set(value) != {"target_kind", "target_id", "content", "delivery"}
    ):
        raise ValueError("message arguments are invalid")
    if value["target_kind"] not in {"chat", "group"}:
        raise ValueError("message target kind is invalid")
    for key in ("target_id", "content"):
        if not isinstance(value[key], str) or not value[key].strip():
            raise ValueError("message text is invalid")
    if len(value["target_id"]) > 256:
        raise ValueError("message target is too long")
    if len(value["content"].encode("utf-8")) > MAX_CONTENT_BYTES:
        raise ValueError("message content is too long")
    mode = value.get("delivery", "steer")
    if mode not in {"steer", "interrupt"}:
        raise ValueError("message delivery mode is invalid")
    return {**dict(value), "delivery": mode}


def validate_request(value: Any) -> dict[str, Any]:
    """Validate an internally authenticated source-bound request."""
    keys = {
        "target_kind",
        "target_id",
        "content",
        "source_conversation_id",
        "source_turn_id",
        "profile_id",
        "tool_call_id",
        "invocation_key",
        "delivery",
    }
    if not isinstance(value, Mapping) or set(value) != keys:
        raise ValueError("message request is invalid")
    message_arguments(
        {key: value[key] for key in ("target_kind", "target_id", "content", "delivery")}
    )
    for key in keys - {"target_kind", "target_id", "content", "delivery"}:
        if not isinstance(value[key], str) or not value[key] or len(value[key]) > 256:
            raise ValueError("message source identity is invalid")
    return dict(value)


def create_plan(
    request: Mapping[str, Any], snapshot: Mapping[str, Any], target_states: Any = None
) -> dict[str, Any]:
    """Seal the entire canonical recipient snapshot and exact source/content."""
    request = validate_request(request)
    if (
        snapshot.get("kind") != "tobkiri.chat.reference.snapshot.v1"
        or snapshot.get("profile_id") != request["profile_id"]
    ):
        raise PermissionError("message recipient profile changed")
    references = snapshot.get("references")
    if not isinstance(references, (list, tuple)) or len(references) != 1:
        raise ValueError("message reference snapshot is invalid")
    reference = references[0]
    if (
        not isinstance(reference, Mapping)
        or reference.get("kind") != request["target_kind"]
        or reference.get("id") != request["target_id"]
    ):
        raise PermissionError("message reference changed")
    recipients = reference.get("conversation_ids")
    if (
        not isinstance(recipients, (list, tuple))
        or not 1 <= len(recipients) <= MAX_RECIPIENTS + 1
    ):
        raise ValueError("message recipient snapshot is invalid")
    if any(
        not isinstance(item, str) or not item or len(item) > 256 for item in recipients
    ):
        raise ValueError("message recipient identity is invalid")
    if list(recipients) != sorted(set(recipients)):
        raise ValueError("message recipient set is invalid")
    if (
        reference.get("membership_complete") is not True
        or type(reference.get("member_count")) is not int
        or reference["member_count"] != len(recipients)
    ):
        raise ValueError("message requires a complete recipient snapshot")
    if (
        request["target_kind"] == "chat"
        and request["source_conversation_id"] in recipients
    ):
        raise PermissionError("message cannot target its source chat")
    effective = [
        item for item in recipients if item != request["source_conversation_id"]
    ]
    if not 1 <= len(effective) <= MAX_RECIPIENTS:
        raise ValueError("message effective recipient count is invalid")
    for key in ("snapshot_time", "expires_at"):
        if type(snapshot.get(key)) is not int:
            raise ValueError("message snapshot time is invalid")
    if (
        snapshot["expires_at"] <= snapshot["snapshot_time"]
        or type(snapshot.get("truncated")) is not bool
        or snapshot["truncated"]
        or snapshot.get("next_cursor") is not None
    ):
        raise ValueError("message snapshot is incomplete or expired")
    if (
        not isinstance(reference.get("label"), str)
        or len(reference["label"]) > 256
        or len(reference["label"].encode("utf-8")) > 1024
    ):
        raise ValueError("message recipient label is invalid")
    states = [] if target_states is None else target_states
    if request["delivery"] == "interrupt":
        if not isinstance(states, (list, tuple)) or len(states) != len(effective):
            raise ValueError("interrupt target states are unavailable")
        for recipient, state in zip(effective, states):
            if (
                not isinstance(state, Mapping)
                or set(state)
                != {
                    "conversation_id",
                    "conversation_revision",
                    "turn_id",
                    "request_id",
                    "revision",
                    "status",
                }
                or state["conversation_id"] != recipient
            ):
                raise ValueError("interrupt target state identity is invalid")
            if (
                type(state.get("conversation_revision")) is not int
                or state["conversation_revision"] < 0
            ):
                raise ValueError("interrupt conversation revision is invalid")
            if state["status"] == "idle":
                if any(
                    state[key] is not None
                    for key in ("turn_id", "request_id", "revision")
                ):
                    raise ValueError("idle target state is invalid")
            elif state["status"] in {"queued", "running", "waiting"}:
                if (
                    any(
                        not isinstance(state[key], str)
                        or not state[key]
                        or len(state[key]) > 256
                        for key in ("turn_id", "request_id")
                    )
                    or type(state["revision"]) is not int
                    or state["revision"] < 1
                ):
                    raise ValueError("active target state is invalid")
            else:
                raise ValueError("interrupt target state is invalid")
    elif states:
        raise ValueError("steer does not accept interruption states")
    plan = {
        "version": PLAN_VERSION,
        "request_digest": canonical_digest(request),
        "snapshot": dict(snapshot),
        "snapshot_digest": canonical_digest(dict(snapshot)),
        "recipient_ids": effective,
        "target_states": [dict(state) for state in states],
        "source_conversation_id": request["source_conversation_id"],
        "source_turn_id": request["source_turn_id"],
        "profile_id": request["profile_id"],
        "content_digest": canonical_digest({"content": request["content"]}),
    }
    return {**plan, "plan_digest": canonical_digest(plan)}


def validate_execute_payload(request: Any, plan: Any) -> dict[str, Any]:
    """Reject changed content, source, recipient plan or digest."""
    request = validate_request(request)
    if not isinstance(plan, Mapping) or not isinstance(plan.get("snapshot"), Mapping):
        raise ValueError("message plan is invalid")
    if dict(plan) != create_plan(request, plan["snapshot"], plan.get("target_states")):
        raise PermissionError("message plan changed")
    return {"request": request, "plan": dict(plan)}


def approval_metadata(request: Any, plan: Any) -> dict[str, str]:
    """Present every approved character within existing per-value/key bounds."""
    bound = validate_execute_payload(request, plan)
    request, plan = bound["request"], bound["plan"]
    reference = plan["snapshot"]["references"][0]
    metadata = {
        "action": "chat.message.send",
        "summary": "Send message to Tobkiri chats",
        "confirmation_phrase": "EXECUTE",
        "delivery": request["delivery"],
        "source": f"Profile: {request['profile_id']}\nSource chat: {request['source_conversation_id']}\nSource turn: {request['source_turn_id']}",
        "target": f"{reference['label']} ({request['target_kind']}:{request['target_id']})",
    }
    if request["source_conversation_id"] in reference["conversation_ids"]:
        metadata["source_excluded"] = (
            "The authenticated source chat is excluded from group delivery."
        )
    if request["delivery"] == "interrupt":
        interrupt_runs = "\n".join(
            f"{state['conversation_id']}: {state['turn_id'] or 'idle'} (request {state['request_id'] or 'none'}, revision {state['revision']})"
            for state in plan["target_states"]
        )
    texts = {
        "message": request["content"],
        "recipients": "\n".join(plan["recipient_ids"]),
    }
    if request["delivery"] == "interrupt":
        texts["interrupt_runs"] = interrupt_runs
    for prefix, text in texts.items():
        for index, offset in enumerate(range(0, len(text), 1800), start=1):
            metadata[f"{prefix}_{index:02d}"] = text[offset : offset + 1800]
    if len(metadata) > 32 or any(len(value) > 2048 for value in metadata.values()):
        raise ValueError("message approval presentation exceeds its finite bounds")
    return metadata


def assert_current_recipients(
    request: Mapping[str, Any],
    plan: Mapping[str, Any],
    current: Mapping[str, Any],
    *,
    now: float | None = None,
) -> None:
    """Compare target semantics while allowing unrelated revisions and clocks."""
    validate_execute_payload(request, plan)
    create_plan(request, current, plan.get("target_states"))
    clock = int(time.time() * 1000) if now is None else now
    approved = plan["snapshot"]
    if (
        not approved["snapshot_time"] <= clock < approved["expires_at"]
        or not current["snapshot_time"] <= clock < current["expires_at"]
    ):
        raise PermissionError("message recipient snapshot expired")
    keys = (
        "kind",
        "id",
        "label",
        "conversation_ids",
        "snapshot_digest",
        "member_count",
        "membership_complete",
    )
    old = approved["references"][0]
    new = current["references"][0]
    if any(old.get(key) != new.get(key) for key in keys):
        raise PermissionError("message recipient membership changed")


def assert_interrupt_targets(approved: Any, current: Any) -> None:
    """Permit monotonic progress of the exact run, rejecting replacement or rollback."""
    if not isinstance(current, (list, tuple)) or len(approved) != len(current):
        raise PermissionError("interrupt target execution changed")
    for old, new in zip(approved, current):
        if any(
            old.get(key) != new.get(key)
            for key in (
                "conversation_id",
                "conversation_revision",
                "turn_id",
                "request_id",
                "status",
            )
        ):
            raise PermissionError("interrupt target execution changed")
        if old["status"] == "idle":
            if new.get("revision") is not None:
                raise PermissionError("interrupt idle state changed")
        elif type(new.get("revision")) is not int or new["revision"] < old["revision"]:
            raise PermissionError("interrupt target revision rolled back")
