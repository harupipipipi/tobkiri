"""Finite saved-turn callbacks behind authenticated transport and captured Broker.

The supervisor owns signature, binding digest, nonce and continuation ordering.
These callbacks own input scope and dispatch selection, never guest authority.
Route readiness is not a credential/network probe or a promise of AI success.
"""

from __future__ import annotations

import json
import re
from typing import Any, Callable, Mapping

from tobkiri_host.saved_turn_plan import TARGETS, TOOL, SavedToolFrame
from tobkiri_protocol.canonical import canonical_digest, canonical_json, strict_loads
from tobkiri_protocol.saved_conversation import (
    validate_saved_conversation_context,
    validate_saved_conversation_input,
)

from tobkiri_protocol.saved_tools import MAX_SAVED_TOOL_HOPS, saved_tool_messages, saved_tool_logs

from ..authority.v4 import AuthorityDenied

Target = tuple[str, str]
def project_saved_ai_result(value: Mapping[str, Any]) -> dict[str, Any]:
    """Cross the strict guest ABI with reply data, not floating-point telemetry."""
    return {
        "status": value.get("status"),
        "output": value.get("output"),
        "tool_intents": value.get("tool_intents", []),
    }


READINESS: Target = (
    "tobkiri.resource.ai.readiness.v1",
    "rumi_ai_gateway_pack.ai-gateway.preflight",
)
REQUIRED_TARGETS = (*dict.fromkeys(TARGETS), READINESS)
DEFINITION: Target = ("tobkiri.resource.tool.definition.v1", "rumi_tool_registry_pack.tool-definition-resource")
TOOL_TARGETS = (DEFINITION, TOOL)
ALLOWED_TARGETS = (*REQUIRED_TARGETS, *TOOL_TARGETS)
Dispatch = Callable[[object, Target, Mapping[str, Any]], Mapping[str, Any]]
RequireTargets = Callable[[object, tuple[Target, ...]], None]


def _request(outer: object) -> dict[str, Any]:
    payload = getattr(outer, "payload", None)
    if not isinstance(payload, Mapping):
        raise AuthorityDenied("saved bridge initial input is missing")
    return validate_saved_conversation_input(payload)["request"]


def _require_resolved_context(conversation: Mapping[str, Any]) -> None:
    """Reject owned context that the text-only saved path cannot resolve."""
    try:
        validate_saved_conversation_context(conversation)
    except ValueError as error:
        raise AuthorityDenied(str(error)) from error


def _messages(
    conversation: Mapping[str, Any], *, flatten_text_blocks: bool = False,
) -> list[dict[str, Any]]:
    """Independently constrain the selected owner history to resolved text."""
    _require_resolved_context(conversation)
    messages = conversation.get("messages")
    if not isinstance(messages, list) or len(messages) > 200:
        raise AuthorityDenied("saved bridge owner history is invalid")
    by_id: dict[str, Mapping[str, Any]] = {}
    for message in messages:
        if not isinstance(message, Mapping):
            raise AuthorityDenied("saved bridge owner message is invalid")
        identifier = message.get("id")
        if not isinstance(identifier, str) or not identifier or identifier in by_id:
            raise AuthorityDenied("saved bridge owner message identity is invalid")
        by_id[identifier] = message
    current = conversation.get("current_node_id")
    if not messages:
        if current is not None:
            raise AuthorityDenied("saved bridge empty history has a current node")
        return []
    if not isinstance(current, str) or current not in by_id:
        raise AuthorityDenied("saved bridge current message is unavailable")
    if all(message.get("parent_id") is None for message in messages):
        selected = messages[: messages.index(by_id[current]) + 1]
    else:
        selected = []
        seen: set[str] = set()
        node: object = current
        while node is not None:
            if not isinstance(node, str) or node not in by_id or node in seen:
                raise AuthorityDenied("saved bridge history is cyclic or incomplete")
            seen.add(node)
            selected.append(by_id[node])
            node = by_id[node].get("parent_id")
        selected.reverse()
    result = []
    for message in selected:
        content = message.get("content")
        text_only = isinstance(content, str) or (
            isinstance(content, list) and bool(content)
            and all(
                isinstance(part, Mapping) and set(part) == {"type", "text"}
                and part["type"] == "text" and isinstance(part["text"], str)
                for part in content
            )
        )
        if (
            message.get("role") not in {"system", "user", "assistant"}
            or message.get("status") != "complete"
            or not text_only
            or message.get("parts")
                        or message.get("widget")
        ):
            raise AuthorityDenied("saved bridge additional content resolution is required")
        metadata = message.get("metadata") or {}
        trace = saved_tool_messages(metadata.get("saved_tool_messages", []))
        if ((trace and message["role"] != "assistant")
                or canonical_json(message.get("tool_logs") or []) != canonical_json(saved_tool_logs(trace))):
            raise AuthorityDenied("saved bridge owned tool transcript is invalid")
        if flatten_text_blocks:
            # Readiness accepts text-only messages. Preserve every tool argument
            # and result as text there; generation receives the exact structures.
            result.extend({"role": "assistant", "content": canonical_json(item).decode()} for item in trace)
        else:
            result.extend(trace)
        # Retain the owner's exact text blocks for guest equality checks and
        # generation. Only the text-only readiness API receives joined text.
        if flatten_text_blocks and isinstance(content, list):
            content = "".join(part["text"] for part in content)
        result.append({"role": message["role"], "content": content})
    return result


class SavedBridgeCallbacks:
    """Bind the four guest stages to finite Host-owned dispatch callbacks."""

    def __init__(self, dispatch: Dispatch, require_targets: RequireTargets) -> None:
        self._dispatch = dispatch
        self._require_targets = require_targets

    def _conversation(self, outer: object, request: Mapping[str, Any]) -> Mapping[str, Any]:
        outcome = self._dispatch(
            outer,
            TARGETS[0],
            {
                "operation": "get",
                "conversation_id": request["conversation_id"],
            },
        )
        value = outcome.get("value")
        conversation = value.get("conversation") if isinstance(value, Mapping) else None
        if (
            outcome.get("status") != "ok"
            or not isinstance(conversation, Mapping)
            or conversation.get("id") != request["conversation_id"]
        ):
            raise AuthorityDenied("saved bridge conversation is unavailable")
        return conversation

    def _tools(self, outer: object, request: Mapping[str, Any]) -> Mapping[str, Any]:
        if request.get("tool_selection", {}).get("mode", "none") == "none":
            return {"tools": [], "definitions": {}}
        self._require_targets(outer, TOOL_TARGETS)
        outcome = self._dispatch(outer, DEFINITION, {
            "operation": "select", "selection": request["tool_selection"],
        })
        value = outcome.get("value")
        if (outcome.get("status") != "ok" or not isinstance(value, Mapping)
                or set(value) != {"tools", "definitions"}
                or not isinstance(value["tools"], list) or not isinstance(value["definitions"], dict)
                or len(value["tools"]) != len(value["definitions"])
                or len(canonical_json(dict(value))) > 512 * 1024):
            raise AuthorityDenied("saved bridge tool selection is unavailable")
        names = []
        for tool in value["tools"]:
            function = tool.get("function") if isinstance(tool, dict) else None
            if (not isinstance(function, dict) or tool.get("type") != "function"
                    or not isinstance(function.get("name"), str)
                    or not isinstance(function.get("parameters"), dict)):
                raise AuthorityDenied("saved bridge selected tool schema is invalid")
            name = function["name"]
            digest = value["definitions"].get(name)
            if (name in names or not isinstance(digest, str)
                    or not re.fullmatch(r"[0-9a-f]{64}", digest)):
                raise AuthorityDenied("saved bridge selected tool identity is invalid")
            names.append(name)
        return value

    def preflight(self, outer: object) -> None:
        """Check every captured target and owned context before any user append."""
        request = _request(outer)
        self._require_targets(outer, REQUIRED_TARGETS)
        tools = self._tools(outer, request)
        conversation = self._conversation(outer, request)
        revision = conversation.get("conversation_revision")
        if type(revision) is not int or revision != request["conversation_revision"]:
            raise AuthorityDenied("saved bridge conversation revision changed")
        model = conversation.get("model_reference")
        if not isinstance(model, str) or not model.strip():
            raise AuthorityDenied("saved bridge owned model is unavailable")
        payload = {
            "model_profile_id": model,
            "messages": [
                *_messages(conversation, flatten_text_blocks=True),
                {"role": "user", "content": request["content"]},
            ],
        }
        if tools["tools"]:
            payload["tool_calling"] = True
        if len(canonical_json(payload)) > 60 * 1024:
            raise AuthorityDenied("saved bridge readiness input exceeds budget")
        outcome = self._dispatch(outer, READINESS, payload)
        value = outcome.get("value")
        if (
            outcome.get("status") != "ok"
            or not isinstance(value, Mapping)
            or value.get("ready") is not True
            or value.get("model_profile_id") != model
        ):
            raise AuthorityDenied("saved bridge AI route is unavailable")

    def __call__(self, outer: object, frame: Mapping[str, Any] | SavedToolFrame) -> Mapping[str, Any]:
        """Dispatch authenticated continuation scope through captured targets."""
        request = _request(outer)
        enabled = request.get("tool_selection", {}).get("mode", "none") != "none"
        trace: list[dict[str, Any]] = []
        if enabled:
            if (not isinstance(frame, SavedToolFrame)
                    or frame.initial_digest != canonical_digest({"request": request})):
                raise AuthorityDenied("saved bridge requires Host-checked tool scope")
            stage = frame.stage
            # Tool-stage scope may contain assistant calls awaiting their results.
            trace = strict_loads(frame.tool_messages, max_bytes=40 * 1024, max_depth=12)
            if stage != "tool":
                trace = saved_tool_messages(trace)
            frame = frame.frame
        elif isinstance(frame, SavedToolFrame):
            raise AuthorityDenied("saved bridge unexpected tool scope")
        else:
            hop = frame.get("hop")
            if type(hop) is not int or not 0 <= hop < len(TARGETS):
                raise AuthorityDenied("saved bridge frame identity is invalid")
            stage = ("read", "user", "ai", "assistant")[hop]
        hop = frame.get("hop")
        if (
            frame.get("kind") != "tobkiri.packvm.continuation.request.v2"
            or type(frame.get("version")) is not int or frame["version"] != 2
            or frame.get("request_id") != getattr(getattr(outer, "context", None), "request_id", None)
            or type(hop) is not int or not 0 <= hop < (MAX_SAVED_TOOL_HOPS if enabled else len(TARGETS))
        ):
            raise AuthorityDenied("saved bridge frame identity is invalid")
        target = {"read": TARGETS[0], "user": TARGETS[1], "ai": TARGETS[2],
                  "assistant": TARGETS[3], "tool": TOOL}.get(stage)
        if target is None or frame.get("target") != {"contract_id": target[0], "operation_id": target[1]}:
            raise AuthorityDenied("saved bridge stage target is invalid")
        self._require_targets(outer, REQUIRED_TARGETS)
        payload = frame.get("payload")
        if not isinstance(payload, Mapping) or len(canonical_json(dict(payload))) > 60 * 1024:
            raise AuthorityDenied("saved bridge payload is invalid")
        if stage == "read":
            if dict(payload) != {"operation": "get", "conversation_id": request["conversation_id"]}:
                raise AuthorityDenied("saved bridge conversation read is out of scope")
        elif stage in {"user", "assistant"}:
            self._check_append(request, payload, 1 if stage == "user" else 3, trace if stage == "assistant" else [])
            if stage == "user":
                self._tools(outer, request)
                conversation = self._conversation(outer, request)
                _require_resolved_context(conversation)
                if (
                    payload["expected_conversation_revision"] != request["conversation_revision"]
                    or conversation.get("conversation_revision") != request["conversation_revision"]
                    or payload["message"]["parent_id"] != conversation.get("current_node_id")
                ):
                    raise AuthorityDenied("saved bridge selected branch changed")
        elif stage == "ai":
            conversation = self._conversation(outer, request)
            if (
                set(payload) != {"messages", "model_reference", "requirements"}
                or payload["model_reference"] != conversation.get("model_reference")
                or canonical_json(payload["messages"]) != canonical_json([*_messages(conversation), *trace])
                or payload["requirements"] != {"request_surface": "conversation.saved"}
            ):
                raise AuthorityDenied("saved bridge AI input differs from the owner")
            selected = self._tools(outer, request)
            arguments = dict(payload)
            if selected["tools"]:
                arguments.update({
                    "tools": selected["tools"],
                    "requirements": {**payload["requirements"], "tool_calling": True},
                    "parameters": {"tool_choice": "required" if request["tool_selection"].get("must_use") and not trace else "auto"},
                })
            outcome = self._dispatch(outer, target, arguments)
            if enabled and outcome.get("status") == "ok" and isinstance(outcome.get("value"), Mapping):
                return {**outcome, "value": {**outcome["value"], "tool_definitions": selected["definitions"]}}
            return outcome
        else:
            if not enabled or set(payload) != {"tool_id", "tool_call_id", "arguments", "expected_definition_hash"}:
                raise AuthorityDenied("saved bridge tool invocation is out of scope")
            self._require_targets(outer, TOOL_TARGETS)
        return self._dispatch(outer, target, payload)

    @staticmethod
    def _check_append(
        request: Mapping[str, Any], payload: Mapping[str, Any], hop: int,
        trace: list[dict[str, Any]] | None = None,
    ) -> None:
        message = payload.get("message")
        role = "user" if hop == 1 else "assistant"
        trace = trace or []
        metadata = {"turn_id": request["turn_id"]}
        fields = {"id", "role", "content", "parent_id", "metadata", "status"}
        if trace:
            metadata["saved_tool_messages"] = trace
            fields.add("tool_logs")

        def message_id(kind: str) -> str:
            identity = [request["conversation_id"], request["turn_id"], kind]
            return "message:" + canonical_digest(identity).removeprefix("sha256:")

        if (
            set(payload)
            != {"operation", "conversation_id", "expected_conversation_revision", "message"}
            or payload["operation"] != "append"
            or payload["conversation_id"] != request["conversation_id"]
            or not isinstance(message, Mapping)
            or set(message) != fields
            or message["id"] != message_id(role)
            or message["role"] != role
            or canonical_json(message["metadata"]) != canonical_json(metadata)
            or canonical_json(message.get("tool_logs", [])) != canonical_json(saved_tool_logs(trace))
            or message["status"] != "complete"
            or (hop == 1 and message["content"] != request["content"])
            or (hop == 3 and message["parent_id"] != message_id("user"))
        ):
            raise AuthorityDenied("saved bridge append is out of scope")


def project_saved_tool_result(value: Mapping[str, Any]) -> dict[str, Any]:
    """Carry normalized tool content as text without loosening the guest JSON ABI."""
    if (value.get("status") not in {"success", "error"}
            or type(value.get("is_error")) is not bool
            or value["is_error"] != (value["status"] == "error")):
        raise ValueError("saved tool result is not normalized")
    content = json.dumps({key: value.get(key) for key in ("status", "result", "error")},
                         ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    if len(content.encode()) > 16 * 1024:
        raise ValueError("saved tool result exceeds its bound")
    return {"tool_id": value.get("tool_id"), "tool_call_id": value.get("tool_call_id"), "content": content}
