"""A bounded saved-turn transition checker over authenticated Host results."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Mapping

from tobkiri_protocol.canonical import canonical_digest, canonical_json, strict_loads
from tobkiri_protocol.saved_tools import MAX_SAVED_TOOL_CALLS, saved_tool_logs, saved_tool_messages

READ = ("tobkiri.resource.conversation.v1", "rumi_conversation_store_pack.conversation-resource")
APPEND = ("tobkiri.action.message.manage.v1", "rumi_conversation_store_pack.message-manage")
AI = ("tobkiri.service.ai.generate.v1", "rumi_ai_gateway_pack.ai-gateway.generate")
TOOL = ("tobkiri.service.tool.invoke.v1", "rumi_tool_broker_pack.tool-invoke")
TARGETS = (READ, APPEND, AI, APPEND)


@dataclass(frozen=True)
class SavedToolFrame:
    """Host-local checked scope, never accepted from a serialized guest frame."""

    frame: Mapping[str, Any]
    initial_digest: str
    tool_messages: bytes
    stage: str


class SavedTurnPlan:
    """Retain only the current exchange's scope; no durable scheduling or grants."""

    def __init__(self, request: Mapping[str, Any]) -> None:
        self.initial_digest = canonical_digest({"request": dict(request)})
        selection = request.get("tool_selection", {})
        self.enabled = selection.get("mode", "none") != "none"
        self.must_use = selection.get("must_use", False)
        self.stage = "read"
        self.messages: list[dict[str, Any]] = []
        self.pending: list[dict[str, Any]] = []
        self.seen: set[str] = set()
        self.failed = False

    @property
    def target(self) -> tuple[str, str]:
        """Return the single next permitted target from acknowledged outcomes."""
        return {"read": READ, "user": APPEND, "ai": AI, "tool": TOOL, "assistant": APPEND}[self.stage]

    def check(self, payload: Mapping[str, Any]) -> None:
        """Match an effect or saved transcript before any nested dispatch."""
        if self.failed or self.stage == "complete":
            raise ValueError("saved turn cannot dispatch another action")
        if self.stage == "tool" and canonical_json(dict(payload)) != canonical_json(self.pending[0]):
            raise ValueError("saved tool differs from the acknowledged AI intent")
        if self.stage == "assistant":
            message = payload.get("message", {})
            metadata = message.get("metadata", {})
            if (
                canonical_json(metadata.get("saved_tool_messages", [])) != canonical_json(self.messages)
                or canonical_json(message.get("tool_logs", [])) != canonical_json(saved_tool_logs(self.messages))
            ):
                raise ValueError("saved assistant tool transcript differs from Host results")

    def receive(self, outcome: Mapping[str, Any]) -> None:
        """Advance once after a Host result; errors never select a retry target."""
        if outcome.get("status") != "ok" or not isinstance(outcome.get("value"), Mapping):
            self.failed = True
            return
        value = outcome["value"]
        if self.stage == "read":
            self.stage = "user"
        elif self.stage == "user":
            self.stage = "ai"
        elif self.stage == "tool":
            expected = self.pending[0]
            if (
                value.get("tool_id") != expected["tool_id"]
                or value.get("tool_call_id") != expected["tool_call_id"]
                or not isinstance(value.get("content"), str)
            ):
                self.failed = True
                return
            self.messages.append({
                "role": "tool", "tool_call_id": expected["tool_call_id"], "content": value["content"],
            })
            self.pending.pop(0)
            self.stage = "tool" if self.pending else "ai"
        elif self.stage == "ai":
            self._ai_result(value)
        else:
            self.stage = "complete"

    def _ai_result(self, value: Mapping[str, Any]) -> None:
        intents = value.get("tool_intents", [])
        if value.get("status") != "ok" or not isinstance(intents, list):
            self.failed = True
            return
        if not intents:
            self.failed = self.must_use and not self.seen
            self.stage = "assistant"
            return
        definitions = value.get("tool_definitions")
        if not self.enabled or not isinstance(definitions, dict) or len(self.seen) + len(intents) > MAX_SAVED_TOOL_CALLS:
            self.failed = True
            return
        calls = []
        for intent in intents:
            if not isinstance(intent, dict):
                self.failed = True
                return
            name, identifier, arguments = intent.get("operation"), intent.get("intent_id"), intent.get("arguments")
            digest = definitions.get(name) if isinstance(name, str) else None
            if (
                not isinstance(identifier, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,255}", identifier) or identifier in self.seen
                or not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}", name)
                or not isinstance(arguments, dict) or not isinstance(digest, str)
                or re.fullmatch(r"[0-9a-f]{64}", digest) is None
            ):
                self.failed = True
                return
            self.seen.add(identifier)
            self.pending.append({
                "tool_id": name, "tool_call_id": identifier, "arguments": arguments,
                "expected_definition_hash": digest,
            })
            calls.append({
                "id": identifier, "type": "function",
                "function": {"name": name, "arguments": canonical_json(arguments).decode()},
            })
        self.messages.append({"role": "assistant", "content": value.get("output") or "", "tool_calls": calls})
        # Copy/bound before sending any of the proposed tool effects.
        self.messages = strict_loads(canonical_json(self.messages), max_bytes=40 * 1024, max_depth=12)
        saved_tool_messages([*self.messages, *[
            {"role": "tool", "tool_call_id": item["tool_call_id"], "content": ""}
            for item in self.pending
        ]])
        self.stage = "tool"
