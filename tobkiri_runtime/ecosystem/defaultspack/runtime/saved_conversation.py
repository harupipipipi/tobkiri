"""Pure saved-conversation steps for the bounded v2 PackVM continuation path.

This module performs no storage or network I/O. The root guest runner must seal
each intent, retain its state, and supply only its authenticated Host result.
Intents are not authority; registration and the captured Broker remain required.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

TARGETS = (
    ("tobkiri.resource.conversation.v1", "rumi_conversation_store_pack.conversation-resource"),
    ("tobkiri.action.message.manage.v1", "rumi_conversation_store_pack.message-manage"),
    ("tobkiri.service.ai.generate.v1", "rumi_ai_gateway_pack.ai-gateway.generate"),
    ("tobkiri.action.message.manage.v1", "rumi_conversation_store_pack.message-manage"),
)
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,255}\Z")
_REQUEST_FIELDS = {"turn_id", "conversation_id", "conversation_revision", "content"}
_STATE_FIELDS = {
    "hop",
    "request",
    "history",
    "model_reference",
    "revision",
    "parent_id",
    "assistant",
}


def _json(value: Any, *, limit: int = 60 * 1024) -> bytes:
    def validate(item: Any, depth: int = 0) -> None:
        if depth > 12:
            raise ValueError("saved conversation nesting exceeds limit")
        if item is None or type(item) in (str, bool):
            return
        if type(item) is int and abs(item) <= 2**53 - 1:
            return
        if type(item) is list:
            for child in item:
                validate(child, depth + 1)
            return
        if type(item) is dict and all(type(key) is str for key in item):
            for child in item.values():
                validate(child, depth + 1)
            return
        raise ValueError("saved conversation value is not strict JSON")

    validate(value)
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )
    if len(encoded) > limit:
        raise ValueError("saved conversation exceeds continuation budget")
    return encoded


def _identifier(value: Any) -> str:
    if not isinstance(value, str) or _ID.fullmatch(value) is None:
        raise ValueError("saved conversation identity is invalid")
    return value


def _revision(value: Any) -> int:
    if type(value) is not int or value < 1:
        raise ValueError("saved conversation revision is invalid")
    return value


def _request(value: Any) -> dict[str, Any]:
    if type(value) is not dict or set(value) != _REQUEST_FIELDS:
        raise ValueError("saved conversation request fields are invalid")
    _identifier(value["turn_id"])
    _identifier(value["conversation_id"])
    _revision(value["conversation_revision"])
    if not isinstance(value["content"], str) or not value["content"].strip():
        raise ValueError("saved conversation requires nonempty user text")
    _json(value)
    return value


def _message_id(request: dict[str, Any], role: str) -> str:
    identity = [request["conversation_id"], request["turn_id"], role]
    return "message:" + hashlib.sha256(_json(identity)).hexdigest()


def _message(state: dict[str, Any], role: str) -> dict[str, Any]:
    request = state["request"]
    return {
        "id": _message_id(request, role),
        "role": role,
        "content": request["content"] if role == "user" else state["assistant"],
        "parent_id": state["parent_id"] if role == "user" else _message_id(request, "user"),
        "metadata": {"turn_id": request["turn_id"]},
        "status": "complete",
    }


def _intent(state: dict[str, Any]) -> dict[str, Any]:
    hop, request = state["hop"], state["request"]
    if hop == 0:
        payload = {"operation": "get", "conversation_id": request["conversation_id"]}
    elif hop in (1, 3):
        payload = {
            "operation": "append",
            "conversation_id": request["conversation_id"],
            "expected_conversation_revision": state["revision"],
            "message": _message(state, "user" if hop == 1 else "assistant"),
        }
    else:
        payload = {
            "messages": [*state["history"], {"role": "user", "content": request["content"]}],
            "model_reference": state["model_reference"],
            "requirements": {"request_surface": "defaultspack.conversation"},
        }
    value = {
        "kind": "tobkiri.packvm.continuation.intent.v2",
        "hop": hop,
        "target": dict(zip(("contract_id", "operation_id"), TARGETS[hop])),
        "payload": payload,
        "state": state,
    }
    # Leave room for root-owned identity, nonce and predecessor framing.
    _json(value)
    return value


def _failure(state: dict[str, Any], code: str) -> dict[str, Any]:
    hop, request = state["hop"], state["request"]
    user = ("not_written", "unknown", "saved", "saved")[hop]
    assistant = "unknown" if hop == 3 else "not_written"
    reconcile = hop in (1, 3)
    if code == "TURN_RECONCILIATION_REQUIRED":
        user, assistant, reconcile = "unknown", "unknown", True
    return {
        "status": "error",
        "error": {"code": code, "message": "Saved conversation did not complete."},
        "turn_id": request["turn_id"],
        "conversation_id": request["conversation_id"],
        "user_message_id": _message_id(request, "user"),
        "assistant_message_id": _message_id(request, "assistant"),
        "user_persistence": user,
        "assistant_persistence": assistant,
        "reconciliation_required": reconcile,
    }


def _history(conversation: dict[str, Any]) -> tuple[list[dict[str, Any]], str | None]:
    messages = conversation.get("messages")
    if not isinstance(messages, list) or len(messages) > 200:
        raise ValueError("conversation history is invalid or too large")
    by_id = {_identifier(item["id"]): item for item in messages}
    if len(by_id) != len(messages):
        raise ValueError("conversation history contains duplicate IDs")
    current = conversation.get("current_node_id")
    if not messages:
        if current is not None:
            raise ValueError("empty conversation has a current node")
        return [], None
    current = _identifier(current)
    if current not in by_id:
        raise ValueError("conversation current node is unavailable")
    if all(item.get("parent_id") is None for item in messages):
        # Legacy linear records predate explicit parent links.
        selected = messages[: messages.index(by_id[current]) + 1]
    else:
        selected = []
        seen: set[str] = set()
        node: str | None = current
        while node is not None:
            if node in seen or node not in by_id:
                raise ValueError("conversation branch is cyclic or incomplete")
            seen.add(node)
            selected.append(by_id[node])
            node = by_id[node].get("parent_id")
        selected.reverse()
    history = []
    for item in selected:
        if (
            item.get("role") not in {"user", "assistant", "system"}
            or item.get("status") != "complete"
        ):
            raise ValueError(
                "conversation history requires explicit tool or incomplete-turn handling"
            )
        if item.get("parts") or item.get("tool_logs") or item.get("widget"):
            raise ValueError("conversation history requires additional content resolution")
        history.append({"role": item["role"], "content": item["content"]})
    return history, current


def start(payload: dict[str, Any]) -> dict[str, Any]:
    """Request the exact owned conversation before attempting any write."""
    request = json.loads(_json(_request(payload)))
    return _intent(
        {
            "hop": 0,
            "request": request,
            "history": [],
            "model_reference": None,
            "revision": request["conversation_revision"],
            "parent_id": None,
            "assistant": None,
        }
    )


def resume(state: dict[str, Any], outcome: dict[str, Any]) -> dict[str, Any]:
    """Advance only after an exact owner result; errors never produce a retry."""
    _json(state)
    if type(state) is not dict or set(state) != _STATE_FIELDS:
        raise ValueError("saved continuation fields are invalid")
    state = json.loads(_json(state))
    request = _request(state["request"])
    hop = state["hop"]
    if type(hop) is not int or not 0 <= hop < 4:
        raise ValueError("saved continuation hop is invalid")
    _revision(state["revision"])
    if (
        type(outcome) is not dict
        or outcome.get("status") != "ok"
        or set(outcome) != {"status", "value"}
    ):
        return _failure(state, "HOST_ACTION_FAILED")
    value = outcome["value"]
    acknowledged = False
    try:
        _json(value, limit=512 * 1024)
        if not isinstance(value, dict):
            raise ValueError("owner response must be an object")
        if hop == 0:
            conversation = value["conversation"]
            if conversation["id"] != request["conversation_id"]:
                raise ValueError("conversation identity mismatch")
            if any(
                item["id"] in {_message_id(request, "user"), _message_id(request, "assistant")}
                for item in conversation["messages"]
            ):
                return _failure(state, "TURN_RECONCILIATION_REQUIRED")
            if _revision(conversation["conversation_revision"]) != request["conversation_revision"]:
                return _failure(state, "CONVERSATION_REVISION_CONFLICT")
            # These references require extra captured owner calls, not silently
            # discarded prompts/agent configuration or an ambient Host lookup.
            if conversation.get("system_prompt_id") or conversation.get("agent_id"):
                return _failure(state, "CONTEXT_RESOLUTION_REQUIRED")
            model = conversation.get("model_reference")
            if not isinstance(model, str) or not model.strip():
                return _failure(state, "MODEL_REFERENCE_REQUIRED")
            state["history"], state["parent_id"] = _history(conversation)
            state["model_reference"] = model
            # Size preflight only, not proof of provider readiness. The larger
            # AI intent must fit before the user message is persisted.
            _intent({**state, "hop": 2})
        elif hop in (1, 3):
            expected = _message(state, "user" if hop == 1 else "assistant")
            message = value["message"]
            if (
                not isinstance(message, dict)
                or value.get("action") != "message_appended"
                or any(message.get(key) != item for key, item in expected.items())
            ):
                raise ValueError("message owner acknowledgement mismatch")
            revision = _revision(value["conversation_revision"])
            if revision <= state["revision"]:
                raise ValueError("message owner revision did not advance")
            state["revision"] = revision
            acknowledged = True
            if hop == 3:
                return {
                    "status": "ok",
                    "turn_id": request["turn_id"],
                    "conversation_id": request["conversation_id"],
                    "conversation_revision": revision,
                    "user_message_id": _message_id(request, "user"),
                    "message": message,
                }
        else:
            if value.get("status") != "ok" or value.get("tool_intents"):
                return _failure(state, "AI_COMPLETION_UNAVAILABLE")
            output = value.get("output")
            if not isinstance(output, (str, list)) or not output:
                raise ValueError("AI output is invalid")
            state["assistant"] = output
            state["history"] = []
        return _intent({**state, "hop": hop + 1})
    except (KeyError, TypeError, ValueError, UnicodeError):
        # If the next intent cannot be represented, preserve which write has
        # actually been acknowledged, rather than claiming a clean rollback.
        return _failure(
            {**state, "hop": 2} if hop == 1 and acknowledged else state, "OWNER_RESPONSE_INVALID"
        )


def tobkiri_packvm_invoke(operation_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Expose the isolated computation ABI, pending root-owned v2 integration.

    Only the guest root may create resume input after consuming a sealed result.
    The external saved-send route must never accept caller-supplied state.
    """
    if operation_id != "saved_complete" or type(payload) is not dict:
        raise ValueError("unsupported saved conversation operation")
    if set(payload) == {"request"}:
        return start(payload["request"])
    if set(payload) == {"state", "outcome"}:
        return resume(payload["state"], payload["outcome"])
    raise ValueError("invalid saved conversation ABI payload")
