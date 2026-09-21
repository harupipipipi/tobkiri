"""Regression coverage for exact defaultspack channel mentions."""

from __future__ import annotations

from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parent.parent
DEFAULTSPACK_ROOT = ROOT / "ecosystem" / "defaultspack"
sys.path.insert(0, str(DEFAULTSPACK_ROOT))

from domain.chat.messaging import MessagingService  # noqa: E402
from domain.chat.notification import NotificationService  # noqa: E402


def _notification_service() -> NotificationService:
    service = NotificationService()
    service._agent_registry.clear()
    service._notifications.clear()
    return service


def test_channel_parser_uses_shared_boundaries_and_keeps_hyphenated_ids() -> None:
    text = (
        "お願い@agent-qa、@agent_2 mail@example.com "
        "https://example.com/@agent-qa \\@agent-qa @@agent-qa"
    )

    assert MessagingService.parse_mentions(text) == ["agent-qa", "agent_2"]
    assert MessagingService.parse_mentions("お願い@agent.qa", ["agent.qa"]) == [
        "agent.qa"
    ]


def test_channel_mentions_resolve_exact_ids_without_substring_fanout() -> None:
    service = _notification_service()
    message = {
        "id": "message-1",
        "sender_id": "sender",
        "sender_name": "Sender",
        "content": "@ann",
        "mentions": ["ann"],
    }

    notifications, agents_to_reply = service.create_notifications(
        "channel-1", message, ["sender", "joanna", "annette"]
    )

    assert notifications == []
    assert agents_to_reply == []
    assert message["unresolved_mentions"] == ["ann"]


def test_hyphenated_agent_and_case_insensitive_exact_alias_notify_once() -> None:
    service = _notification_service()
    service.register_agent("agent-qa", "Agent-QA")
    message = {
        "id": "message-2",
        "sender_id": "sender",
        "sender_name": "Sender",
        "content": "@agent-qa @AGENT-QA",
        "mentions": ["agent-qa", "AGENT-QA"],
    }

    notifications, agents_to_reply = service.create_notifications(
        "channel-1", message, ["sender", "agent-qa"]
    )

    assert [item["target_id"] for item in notifications] == ["agent-qa"]
    assert agents_to_reply == ["agent-qa"]
    assert message["unresolved_mentions"] == []


def test_all_keeps_broadcast_semantics_and_unknown_mentions_are_explicit() -> None:
    service = _notification_service()
    message = {
        "id": "message-3",
        "sender_id": "sender",
        "sender_name": "Sender",
        "content": "@ALL @missing",
        "mentions": ["ALL", "missing"],
    }

    notifications, agents_to_reply = service.create_notifications(
        "channel-1", message, ["sender", "member-1", "member-2"]
    )

    assert [item["target_id"] for item in notifications] == ["member-1", "member-2"]
    assert agents_to_reply == []
    assert message["unresolved_mentions"] == ["missing"]


def test_case_fold_collision_is_unresolved_instead_of_picking_a_member() -> None:
    service = _notification_service()
    message = {
        "id": "message-4",
        "sender_id": "sender",
        "sender_name": "Sender",
        "content": "@agent",
        "mentions": ["agent"],
    }

    notifications, agents_to_reply = service.create_notifications(
        "channel-1", message, ["sender", "Agent", "agent"]
    )

    assert notifications == []
    assert agents_to_reply == []
    assert message["unresolved_mentions"] == ["agent"]
