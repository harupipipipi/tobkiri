"""Finite saved-turn callbacks behind authenticated transport and captured Broker.

The supervisor owns signature, binding digest, nonce and continuation ordering.
These callbacks own input scope and dispatch selection, never guest authority.
Route readiness is not a credential/network probe or a promise of AI success.
"""

from __future__ import annotations

from typing import Any, Callable, Mapping

from tobkiri_host.saved_guest_dispatch import TARGETS
from tobkiri_protocol.canonical import canonical_digest, canonical_json
from tobkiri_protocol.saved_conversation import validate_saved_conversation_input

from ..authority.v4 import AuthorityDenied

Target = tuple[str, str]
READINESS: Target = (
    "tobkiri.resource.ai.readiness.v1",
    "rumi_ai_gateway_pack.ai-gateway.preflight",
)
REQUIRED_TARGETS = (*dict.fromkeys(TARGETS), READINESS)
Dispatch = Callable[[object, Target, Mapping[str, Any]], Mapping[str, Any]]
RequireTargets = Callable[[object, tuple[Target, ...]], None]


def _request(outer: object) -> dict[str, Any]:
    payload = getattr(outer, "payload", None)
    if not isinstance(payload, Mapping):
        raise AuthorityDenied("saved bridge initial input is missing")
    return validate_saved_conversation_input(payload)["request"]


def _messages(conversation: Mapping[str, Any]) -> list[dict[str, str]]:
    """Independently constrain the selected owner history to resolved text."""
    if conversation.get("system_prompt_id") or conversation.get("agent_id"):
        raise AuthorityDenied("saved bridge context resolution is required")
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
        if (
            message.get("role") not in {"system", "user", "assistant"}
            or message.get("status") != "complete"
            or not isinstance(message.get("content"), str)
            or message.get("parts")
            or message.get("tool_logs")
            or message.get("widget")
        ):
            raise AuthorityDenied("saved bridge additional content resolution is required")
        result.append({"role": message["role"], "content": message["content"]})
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

    def preflight(self, outer: object) -> None:
        """Check every captured target and owned context before any user append."""
        request = _request(outer)
        self._require_targets(outer, REQUIRED_TARGETS)
        conversation = self._conversation(outer, request)
        revision = conversation.get("conversation_revision")
        if type(revision) is not int or revision != request["conversation_revision"]:
            raise AuthorityDenied("saved bridge conversation revision changed")
        model = conversation.get("model_reference")
        if not isinstance(model, str) or not model.strip():
            raise AuthorityDenied("saved bridge owned model is unavailable")
        payload = {
            "model_profile_id": model,
            "messages": [*_messages(conversation), {"role": "user", "content": request["content"]}],
        }
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

    def __call__(self, outer: object, frame: Mapping[str, Any]) -> Mapping[str, Any]:
        """Dispatch a supervisor-validated frame without trusting its state as scope."""
        request = _request(outer)
        hop = frame.get("hop")
        if (
            frame.get("kind") != "tobkiri.packvm.continuation.request.v2"
            or type(frame.get("version")) is not int
            or frame["version"] != 2
            or frame.get("request_id")
            != getattr(getattr(outer, "context", None), "request_id", None)
            or type(hop) is not int
            or not 0 <= hop < len(TARGETS)
        ):
            raise AuthorityDenied("saved bridge frame identity is invalid")
        target = TARGETS[hop]
        if frame.get("target") != {"contract_id": target[0], "operation_id": target[1]}:
            raise AuthorityDenied("saved bridge stage target is invalid")
        self._require_targets(outer, REQUIRED_TARGETS)
        payload = frame.get("payload")
        if not isinstance(payload, Mapping) or len(canonical_json(dict(payload))) > 60 * 1024:
            raise AuthorityDenied("saved bridge payload is invalid")
        if hop == 0:
            if dict(payload) != {"operation": "get", "conversation_id": request["conversation_id"]}:
                raise AuthorityDenied("saved bridge conversation read is out of scope")
        elif hop in (1, 3):
            self._check_append(request, payload, hop)
        else:
            conversation = self._conversation(outer, request)
            if (
                set(payload) != {"messages", "model_reference", "requirements"}
                or payload["model_reference"] != conversation.get("model_reference")
                or payload["messages"] != _messages(conversation)
                or payload["requirements"] != {"request_surface": "defaultspack.conversation"}
            ):
                raise AuthorityDenied("saved bridge AI input differs from the owner")
        return self._dispatch(outer, target, payload)

    @staticmethod
    def _check_append(request: Mapping[str, Any], payload: Mapping[str, Any], hop: int) -> None:
        message = payload.get("message")
        role = "user" if hop == 1 else "assistant"

        def message_id(kind: str) -> str:
            identity = [request["conversation_id"], request["turn_id"], kind]
            return "message:" + canonical_digest(identity).removeprefix("sha256:")

        if (
            set(payload)
            != {"operation", "conversation_id", "expected_conversation_revision", "message"}
            or payload["operation"] != "append"
            or payload["conversation_id"] != request["conversation_id"]
            or not isinstance(message, Mapping)
            or set(message) != {"id", "role", "content", "parent_id", "metadata", "status"}
            or message["id"] != message_id(role)
            or message["role"] != role
            or message["metadata"] != {"turn_id": request["turn_id"]}
            or message["status"] != "complete"
            or (hop == 1 and message["content"] != request["content"])
            or (hop == 3 and message["parent_id"] != message_id("user"))
        ):
            raise AuthorityDenied("saved bridge append is out of scope")
