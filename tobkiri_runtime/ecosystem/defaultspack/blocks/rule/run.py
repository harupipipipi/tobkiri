"""Conversation rule slash command.

`/rule <text>` creates a persistent rule for the current conversation. Rules are
stored outside chat message history and injected on every turn by
blocks.chat._context_helpers, so context compaction cannot delete them.
"""

from __future__ import annotations

from typing import Any


from blocks._common import error, ok
from domain.chat.rules import ConversationRuleStore


def run(input_data: Any = None, context: Any = None) -> dict[str, Any]:
    data = input_data if isinstance(input_data, dict) else {}
    action = str(data.get("action") or "").strip().lower()
    rule_text = _rule_text(data)
    conversation_id = str(data.get("conversation_id") or "").strip()

    if not action:
        action = "add" if rule_text else "list"

    mutating_actions = {"add", "create", "set", "disable", "delete", "remove"}
    if action in mutating_actions and _is_untrusted_external(context):
        return error(
            "external or P2P requests cannot mutate persistent conversation rules",
            "FORBIDDEN",
        )

    store = ConversationRuleStore()

    try:
        if action in {"add", "create", "set"}:
            if not conversation_id:
                return error("conversation_id is required", "INVALID_INPUT")
            if not rule_text:
                return error("rule text is required", "MISSING_PARAM")
            rule = store.create_rule(
                conversation_id=conversation_id,
                text=rule_text,
                scope=str(data.get("scope") or "conversation"),
                source=str(data.get("source") or "user_command"),
                priority=str(data.get("priority") or "normal"),
                metadata=_metadata(data, context),
            )
            return ok(
                {
                    "created": True,
                    "rule": rule,
                    "rules": store.list_rules(conversation_id),
                    "message": "rule pinned for this conversation",
                }
            )

        if action in {"list", "get", "status"}:
            rules = store.list_rules(
                conversation_id or None,
                active_only=not _truthy(data.get("include_disabled")),
            )
            return ok(
                {
                    "conversation_id": conversation_id,
                    "rules": rules,
                    "total": len(rules),
                }
            )

        if action in {"disable", "delete", "remove"}:
            rule_id = str(data.get("rule_id") or data.get("id") or "").strip()
            if not rule_id:
                return error("rule_id is required", "MISSING_PARAM")
            disabled = store.disable_rule(rule_id, conversation_id=conversation_id or None)
            if disabled is None:
                return error("rule not found", "NOT_FOUND")
            return ok(
                {
                    "disabled": True,
                    "rule": disabled,
                    "rules": store.list_rules(conversation_id or None),
                }
            )

        return error("unsupported rule action: " + action, "INVALID_INPUT")
    except ValueError as exc:
        return error(str(exc), "INVALID_INPUT")
    except Exception as exc:
        return error("rule command failed: " + str(exc), "RULE_ERROR")


def _rule_text(data: dict[str, Any]) -> str:
    for key in ("rule", "content", "text", "message"):
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _metadata(data: dict[str, Any], context: Any) -> dict[str, Any]:
    metadata = data.get("metadata") if isinstance(data.get("metadata"), dict) else {}
    result = dict(metadata)
    for key in ("message_id", "run_id", "request_id"):
        value = data.get(key)
        if value not in (None, ""):
            result[key] = value
    if isinstance(context, dict):
        for key in ("run_source", "actor_id", "trusted_actor_id"):
            value = context.get(key)
            if value not in (None, ""):
                result[key] = value
    return result


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    return str(value or "").strip().lower() in {"1", "true", "yes", "on", "enabled"}


def _is_untrusted_external(context: Any) -> bool:
    if not isinstance(context, dict):
        return False
    if str(context.get("trusted_actor_id") or "").strip():
        return False
    source = " ".join(
        str(context.get(key) or "").strip().lower()
        for key in ("run_source", "source", "origin", "transport")
    )
    return any(token in source for token in ("p2p", "peer", "external", "remote", "webhook"))
