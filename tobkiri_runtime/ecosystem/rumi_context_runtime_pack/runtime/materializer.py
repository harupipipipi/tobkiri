"""Deterministic context materialization without canonical storage."""

from __future__ import annotations

import hashlib
import json
from typing import Any, Callable, Mapping

CONVERSATION = "rumi.resource.conversation.v1"
MEMORY = "rumi.resource.memory.v1"
KNOWLEDGE = "rumi.resource.knowledge.v1"


class ContextBudgetExceeded(RuntimeError):
    """Raised when required content cannot fit the requested context budget."""


class ContextMaterializer:
    """Compose a context snapshot from authoritative contract responses."""

    def __init__(self, client: Any) -> None:
        self.client = client

    def materialize(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        """Return a deterministic, revision-bound context projection."""
        profile_id = str(payload.get("profile_id") or "default")
        conversation_id = str(payload.get("conversation_id") or "")
        conversation = self.client.invoke(
            CONVERSATION,
            "get",
            {"profile_id": profile_id, "conversation_id": conversation_id},
        )
        if not isinstance(conversation, Mapping):
            raise KeyError("conversation is unknown")
        expected = int(payload.get("conversation_revision") or 0)
        actual = int(conversation.get("conversation_revision") or 0)
        if expected <= 0 or actual != expected:
            raise ValueError("conversation revision does not match context request")

        query = str(payload.get("query") or "")
        limit = max(0, min(100, int(payload.get("recall_limit") or 8)))
        common = {
            "profile_id": profile_id,
            "query": query,
            "limit": limit,
        }
        memories = self.client.invoke(MEMORY, "search", common)
        knowledge = self.client.invoke(KNOWLEDGE, "search", common)
        sections = [
            {
                "kind": "system",
                "items": _items(payload.get("system_items")),
                "required": True,
            },
            {
                "kind": "conversation",
                "items": _items(conversation.get("messages")),
                "required": True,
            },
            {"kind": "memory", "items": _items(memories), "required": False},
            {
                "kind": "knowledge",
                "items": _items(knowledge),
                "required": False,
            },
        ]
        budget = max(1, int(payload.get("token_budget") or 8192))
        projected = _fit(sections, budget)
        source_revisions = {
            "conversation": actual,
            "memory": _revision(memories),
            "knowledge": _revision(knowledge),
        }
        digest_input = {
            "profile_id": profile_id,
            "conversation_id": conversation_id,
            "source_revisions": source_revisions,
            "sections": projected,
        }
        return {
            "schema": "rumi.context.materialized.v1",
            **digest_input,
            "estimated_tokens": _tokens(projected),
            "token_budget": budget,
            "digest": _digest(digest_input),
            "authoritative": False,
            "persisted": False,
        }


def create_context_operation(client: Any) -> Callable[[str, Mapping[str, Any]], Any]:
    """Create the derived context materialization operation."""
    materializer = ContextMaterializer(client)

    def operation(name: str, payload: Mapping[str, Any]) -> Any:
        if name == "materialize":
            return materializer.materialize(payload)
        if name == "estimate":
            return {"estimated_tokens": _tokens(_items(payload.get("items")))}
        raise ValueError(f"unknown context operation: {name}")

    return operation


def _fit(sections: list[dict[str, Any]], budget: int) -> list[dict[str, Any]]:
    required = [section for section in sections if section["required"]]
    if _tokens(required) > budget:
        raise ContextBudgetExceeded("required context exceeds token budget")
    result = json.loads(json.dumps(required, ensure_ascii=False, default=str))
    used = _tokens(result)
    for section in sections:
        if section["required"]:
            continue
        accepted: list[Any] = []
        for item in section["items"]:
            candidate = {**section, "items": [*accepted, item]}
            cost = _tokens(candidate)
            if used + cost > budget:
                break
            accepted.append(item)
        result.append({**section, "items": accepted})
        used = _tokens(result)
    return result


def _items(value: Any) -> list[Any]:
    if isinstance(value, Mapping):
        for key in ("items", "results", "memories", "records"):
            items = value.get(key)
            if isinstance(items, list):
                return _copy(items)
        return [_copy(value)]
    if isinstance(value, list):
        return _copy(value)
    if value is None:
        return []
    raise ValueError("context source must be an object or list")


def _revision(value: Any) -> int | str | None:
    if not isinstance(value, Mapping):
        return None
    return value.get("revision") or value.get("index_revision")


def _tokens(value: Any) -> int:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    return max(1, (len(encoded.encode("utf-8")) + 3) // 4)


def _digest(value: Mapping[str, Any]) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _copy(value: Any) -> Any:
    return json.loads(json.dumps(value, ensure_ascii=False, default=str))

