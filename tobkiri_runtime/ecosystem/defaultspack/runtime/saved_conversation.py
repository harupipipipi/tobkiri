"""Pure saved-conversation steps for the bounded v2 PackVM continuation path.

This module performs no storage or network I/O. The root guest runner must seal
each intent, retain its state, and supply only its authenticated Host result.
Intents are not authority; registration and the captured Broker remain required.
"""

from __future__ import annotations

import base64
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
_SAVED_IMAGE_URL = re.compile(
    r"data:(image/(?:png|jpeg|webp|gif));base64,([A-Za-z0-9+/]+={0,2})\Z"
)
_REQUEST_FIELDS = {"turn_id", "conversation_id", "conversation_revision", "content"}
_TOOL_TARGET = ("tobkiri.service.tool.invoke.v1", "rumi_tool_broker_pack.tool-invoke")
_MAX_TOOL_CALLS = 8
_MAX_SAVED_TEXT_BYTES = 60 * 1024
_MAX_SAVED_IMAGE_COUNT = 2
_MAX_SAVED_IMAGE_BYTES = 1024 * 1024
_MAX_SAVED_IMAGE_BASE64_CHARS = ((_MAX_SAVED_IMAGE_BYTES + 2) // 3) * 4
_MAX_SAVED_FRAME_BYTES = 4 * 1024 * 1024
_STAGE_TARGETS = {
    "read": TARGETS[0],
    "user": TARGETS[1],
    "ai": TARGETS[2],
    "assistant": TARGETS[3],
    "tool": _TOOL_TARGET,
}
_STATE_FIELDS = {
    "hop",
    "request",
    "history",
    "model_reference",
    "revision",
    "parent_id",
    "assistant",
    "stage",
    "pending_tools",
    "tool_messages",
    "tool_count",
    "seen_tools",
    "system_prompt_digest",
    "user_content",
    "user_content_digest",
}


def _json(value: Any, *, limit: int = _MAX_SAVED_FRAME_BYTES) -> bytes:
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


def _saved_image_url(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    match = _SAVED_IMAGE_URL.fullmatch(value)
    if (
        match is None
        or len(match.group(2)) % 4
        or len(match.group(2)) > _MAX_SAVED_IMAGE_BASE64_CHARS
    ):
        return False
    try:
        decoded = base64.b64decode(match.group(2), validate=True)
    except (TypeError, ValueError):
        return False
    return 0 < len(decoded) <= _MAX_SAVED_IMAGE_BYTES


def _saved_user_content(value: Any) -> bool:
    if (
        isinstance(value, str)
        and bool(value.strip())
        and len(value.encode("utf-8")) <= _MAX_SAVED_TEXT_BYTES
    ):
        return True
    if not isinstance(value, list) or not 1 <= len(value) <= 1 + _MAX_SAVED_IMAGE_COUNT:
        return False
    text = value[0]
    if (
        not isinstance(text, dict)
        or set(text) != {"type", "text"}
        or text.get("type") != "text"
        or not isinstance(text.get("text"), str)
        or not text["text"].strip()
        or len(text["text"].encode("utf-8")) > _MAX_SAVED_TEXT_BYTES
    ):
        return False
    return all(
        isinstance(part, dict)
        and set(part) == {"type", "image_url"}
        and part.get("type") == "image_url"
        and isinstance(part.get("image_url"), dict)
        and set(part["image_url"]) == {"url"}
        and _saved_image_url(part["image_url"].get("url"))
        for part in value[1:]
    )


def _request(value: Any) -> dict[str, Any]:
    if type(value) is not dict or set(value) - {"tool_selection"} != _REQUEST_FIELDS:
        raise ValueError("saved conversation request fields are invalid")
    _identifier(value["turn_id"])
    _identifier(value["conversation_id"])
    _revision(value["conversation_revision"])
    if not _saved_user_content(value["content"]):
        raise ValueError("saved conversation user content is invalid")
    selection = value.get("tool_selection", {})
    if "tool_selection" in value:
        if (
            not isinstance(selection, dict)
            or set(selection) - {"mode", "include", "exclude", "scope", "must_use"}
            or selection.get("mode") not in {"auto", "manual", "none"}
            or selection.get("scope", "turn") not in {"turn", "conversation"}
            or type(selection.get("must_use", False)) is not bool
        ):
            raise ValueError("saved tool selection is invalid")
        for key in ("include", "exclude"):
            values = selection.get(key, [])
            if not isinstance(values, list) or len(values) > 256:
                raise ValueError("saved tool selection is invalid")
            for item in values:
                if (
                    isinstance(item, dict)
                    and set(item) == {"kind", "id"}
                    and item["kind"] in {"tool", "service"}
                ):
                    item = item["id"]
                if (
                    not isinstance(item, str)
                    or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}", item) is None
                ):
                    raise ValueError("saved tool target is invalid")
        if selection["mode"] == "none" and (selection.get("include") or selection.get("must_use")):
            raise ValueError("disabled saved tools cannot be required")
    _json(value, limit=3 * 1024 * 1024)
    return value


def _state_request(value: Any) -> dict[str, Any]:
    """Validate the compact request identity retained after the user append."""
    if type(value) is not dict or set(value) - {"tool_selection"} != {
        "turn_id", "conversation_id", "conversation_revision"
    }:
        raise ValueError("saved continuation request fields are invalid")
    _identifier(value["turn_id"])
    _identifier(value["conversation_id"])
    _revision(value["conversation_revision"])
    selection = value.get("tool_selection", {})
    if "tool_selection" in value:
        _request({**value, "content": "saved content"})
    return value


def _message_id(request: dict[str, Any], role: str) -> str:
    identity = [request["conversation_id"], request["turn_id"], role]
    return "message:" + hashlib.sha256(_json(identity)).hexdigest()


def _tool_logs(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    calls = {}
    logs = []
    for message in messages:
        if message["role"] == "assistant":
            calls.update({item["id"]: item["function"] for item in message["tool_calls"]})
        else:
            call = calls[message["tool_call_id"]]
            logs.append(
                {
                    "tool_name": call["name"],
                    "tool_call_id": message["tool_call_id"],
                    "arguments": json.loads(call["arguments"]),
                    "result": message["content"],
                }
            )
    return logs


def _message(
    state: dict[str, Any], role: str, *, user_content: Any | None = None,
) -> dict[str, Any]:
    request = state["request"]
    content = state["assistant"]
    if role == "user":
        content = state["user_content"] if user_content is None else user_content
        if not _saved_user_content(content):
            raise ValueError("saved conversation user content is unavailable")
    message = {
        "id": _message_id(request, role),
        "role": role,
        "content": content,
        "parent_id": state["parent_id"] if role == "user" else _message_id(request, "user"),
        "metadata": {"turn_id": request["turn_id"]},
        "status": "complete",
    }
    if role == "assistant" and state["tool_messages"]:
        message["metadata"]["saved_tool_messages"] = state["tool_messages"]
        message["tool_logs"] = _tool_logs(state["tool_messages"])
    return message


def _intent(state: dict[str, Any], *, user_content: Any | None = None) -> dict[str, Any]:
    stage, request = state["stage"], state["request"]
    prompt_digest = state["system_prompt_digest"]
    if prompt_digest is not None and (
        not isinstance(prompt_digest, str)
        or re.fullmatch(r"sha256:[0-9a-f]{64}", prompt_digest) is None
    ):
        raise ValueError("saved system prompt binding is invalid")
    if stage == "read":
        payload = {"operation": "get", "conversation_id": request["conversation_id"]}
    elif stage in {"user", "assistant"}:
        payload = {
            "operation": "append",
            "conversation_id": request["conversation_id"],
            "expected_conversation_revision": state["revision"],
            "message": _message(state, stage, user_content=user_content),
        }
    elif stage == "tool":
        payload = state["pending_tools"][0]
    else:
        payload = {
            "model_reference": state["model_reference"],
            "requirements": {"request_surface": "conversation.saved"},
        }
    if stage in {"user", "ai"} and prompt_digest is not None:
        payload["system_prompt_digest"] = prompt_digest
    value = {
        "kind": "tobkiri.packvm.continuation.intent.v2",
        "hop": state["hop"],
        "target": dict(zip(("contract_id", "operation_id"), _STAGE_TARGETS[stage])),
        "payload": payload,
        "state": state,
    }
    _json(value)
    return value


def _failure(state: dict[str, Any], code: str) -> dict[str, Any]:
    stage, request = state["stage"], state["request"]
    user = "not_written" if stage == "read" else "unknown" if stage == "user" else "saved"
    assistant = "unknown" if stage == "assistant" else "not_written"
    reconcile = stage in {"user", "assistant", "tool"} or state["tool_count"] > 0
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


def start(payload: dict[str, Any]) -> dict[str, Any]:
    """Request the exact owned conversation before attempting any write."""
    request = json.loads(_json(_request(payload), limit=3 * 1024 * 1024))
    user_content = request.pop("content")
    return _intent(
        {
            "hop": 0,
            "request": request,
            "history": [],
            "model_reference": None,
            "revision": request["conversation_revision"],
            "parent_id": None,
            "assistant": None,
            "stage": "read",
            "pending_tools": [],
            "tool_messages": [],
            "tool_count": 0,
            "seen_tools": [],
            "system_prompt_digest": None,
            "user_content": user_content,
            "user_content_digest": None,
        }
    )


def resume(state: dict[str, Any], outcome: dict[str, Any]) -> dict[str, Any]:
    """Advance from one acknowledged result, without replaying an uncertain effect."""
    _json(state)
    if type(state) is not dict or set(state) != _STATE_FIELDS:
        raise ValueError("saved continuation fields are invalid")
    state = json.loads(_json(state))
    request = _state_request(state["request"])
    stage, hop = state["stage"], state["hop"]
    if stage not in _STAGE_TARGETS or type(hop) is not int or not 0 <= hop < 20:
        raise ValueError("saved continuation hop is invalid")
    _revision(state["revision"])
    if (
        type(outcome) is not dict
        or outcome.get("status") != "ok"
        or set(outcome) != {"status", "value"}
    ):
        return _failure(state, "HOST_ACTION_FAILED")
    acknowledged = False
    try:
        value = outcome["value"]
        _json(value, limit=3 * 1024 * 1024 if stage == "user" else 512 * 1024)
        if not isinstance(value, dict):
            raise ValueError("owner response must be an object")
        if stage == "read":
            conversation = value["conversation"]
            if conversation["id"] != request["conversation_id"]:
                raise ValueError("conversation identity mismatch")
            if not isinstance(conversation.get("messages"), list) or any(
                not isinstance(item, dict) or item.get("id") in {
                    _message_id(request, "user"), _message_id(request, "assistant")
                }
                for item in conversation["messages"]
            ):
                return _failure(state, "TURN_RECONCILIATION_REQUIRED")
            if _revision(conversation["conversation_revision"]) != request["conversation_revision"]:
                return _failure(state, "CONVERSATION_REVISION_CONFLICT")
            if conversation.get("agent_id"):
                return _failure(state, "CONTEXT_RESOLUTION_REQUIRED")
            model = conversation.get("model_reference")
            if not isinstance(model, str) or not model.strip():
                return _failure(state, "MODEL_REFERENCE_REQUIRED")
            state["parent_id"] = conversation.get("current_node_id")
            if state["parent_id"] is not None:
                _identifier(state["parent_id"])
            prompt_id = conversation.get("system_prompt_id")
            if prompt_id:
                prompt_digest = value.get("system_prompt_digest")
                if (
                    not isinstance(prompt_digest, str)
                    or re.fullmatch(r"sha256:[0-9a-f]{64}", prompt_digest) is None
                ):
                    return _failure(state, "CONTEXT_RESOLUTION_REQUIRED")
                state["system_prompt_digest"] = prompt_digest
            elif "system_prompt_digest" in value:
                raise ValueError("unexpected saved system prompt")
            state["model_reference"] = model
            user_content = state["user_content"]
            if not _saved_user_content(user_content) or state["user_content_digest"] is not None:
                raise ValueError("saved conversation user content is unavailable")
            state["user_content"] = None
            state["user_content_digest"] = "sha256:" + hashlib.sha256(
                _json(user_content, limit=3 * 1024 * 1024)
            ).hexdigest()
            state["stage"] = "user"
            return _intent({**state, "hop": hop + 1}, user_content=user_content)
        elif stage in {"user", "assistant"}:
            message = value["message"]
            expected = _message(state, stage) if stage == "assistant" else None
            if (
                not isinstance(message, dict)
                or value.get("action") != "message_appended"
                or (
                    stage == "user"
                    and (
                        message.get("id") != _message_id(request, "user")
                        or message.get("role") != "user"
                        or message.get("parent_id") != state["parent_id"]
                        or message.get("metadata") != {"turn_id": request["turn_id"]}
                        or message.get("status") != "complete"
                        or not _saved_user_content(message.get("content"))
                        or "sha256:" + hashlib.sha256(
                            _json(message.get("content"), limit=3 * 1024 * 1024)
                        ).hexdigest()
                        != state["user_content_digest"]
                    )
                )
                or (stage == "assistant" and any(message.get(key) != item for key, item in expected.items()))
            ):
                raise ValueError("message owner acknowledgement mismatch")
            revision = _revision(value["conversation_revision"])
            if revision <= state["revision"]:
                raise ValueError("message owner revision did not advance")
            state["revision"] = revision
            acknowledged = True
            if stage == "assistant":
                return {
                    "status": "ok",
                    "turn_id": request["turn_id"],
                    "conversation_id": request["conversation_id"],
                    "conversation_revision": revision,
                    "user_message_id": _message_id(request, "user"),
                    "message": message,
                }
            state["stage"] = "ai"
        elif stage == "tool":
            tool = state["pending_tools"][0]
            if (
                value.get("tool_id") != tool["tool_id"]
                or value.get("tool_call_id") != tool["tool_call_id"]
                or not isinstance(value.get("content"), str)
            ):
                raise ValueError("tool owner acknowledgement mismatch")
            state["tool_messages"].append(
                {
                    "role": "tool",
                    "tool_call_id": tool["tool_call_id"],
                    "content": value["content"],
                }
            )
            state["pending_tools"].pop(0)
            state["tool_count"] += 1
            state["stage"] = "tool" if state["pending_tools"] else "ai"
        else:
            if value.get("status") != "ok":
                return _failure(state, "AI_COMPLETION_UNAVAILABLE")
            intents = value.get("tool_intents", [])
            if not isinstance(intents, list):
                raise ValueError("tool intents are invalid")
            selection = request.get("tool_selection", {})
            output = value.get("output")
            if isinstance(output, (str, list)):
                _json(output, limit=_MAX_SAVED_TEXT_BYTES)
            if intents:
                definitions = value.get("tool_definitions")
                if (
                    selection.get("mode", "none") == "none"
                    or not isinstance(definitions, dict)
                    or len(state["seen_tools"]) + len(intents) > _MAX_TOOL_CALLS
                ):
                    return _failure(state, "AI_COMPLETION_UNAVAILABLE")
                calls = []
                for intent in intents:
                    name, identifier = intent["operation"], _identifier(intent["intent_id"])
                    arguments = intent["arguments"]
                    digest = definitions[name]
                    if (
                        identifier in state["seen_tools"]
                        or not isinstance(arguments, dict)
                        or not isinstance(digest, str)
                        or not re.fullmatch(r"[0-9a-f]{64}", digest)
                    ):
                        raise ValueError("tool intent binding is invalid")
                    state["seen_tools"].append(identifier)
                    state["pending_tools"].append(
                        {
                            "tool_id": name,
                            "tool_call_id": identifier,
                            "arguments": arguments,
                            "expected_definition_hash": digest,
                        }
                    )
                    calls.append(
                        {
                            "id": identifier,
                            "type": "function",
                            "function": {
                                "name": name,
                                "arguments": _json(arguments).decode(),
                            },
                        }
                    )
                state["tool_messages"].append(
                    {"role": "assistant", "content": output or "", "tool_calls": calls}
                )
                state["stage"] = "tool"
            else:
                if selection.get("must_use") and not state["tool_count"]:
                    return _failure(state, "REQUIRED_TOOL_NOT_USED")
                if not isinstance(output, (str, list)) or not output:
                    raise ValueError("AI output is invalid")
                state["assistant"], state["history"] = output, []
                state["stage"] = "assistant"
        return _intent({**state, "hop": hop + 1})
    except (KeyError, TypeError, ValueError, UnicodeError):
        return _failure(
            # A next intent that failed serialization was never dispatched.
            # Report persistence from the acknowledged stage, not that intent.
            {**state, "stage": "ai" if stage == "user" and acknowledged else stage},
            "OWNER_RESPONSE_INVALID",
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
