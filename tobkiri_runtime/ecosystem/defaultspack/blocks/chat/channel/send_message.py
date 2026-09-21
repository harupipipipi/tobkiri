"""blocks/chat/channel/send_message.py — Send a message to a channel."""

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", ".."))

from blocks._common import ok, error
from domain.chat.channel_manager import ChannelManager
from domain.chat.messaging import MessagingService
from domain.chat.notification import NotificationService


def run(input_data, context):
    """Send a message to a channel. Handles @mentions and AI auto-replies.

    input_data:
        id          : str (required) Channel ID — injected from path param
        sender_id   : str (required) Sender's ID
        sender_name : str (required) Sender's display name
        content     : str (required) Message text (may contain @mentions)
        metadata    : dict (optional) Extra metadata
    """
    if not isinstance(input_data, dict):
        return error("input_data must be a dict")

    channel_id = input_data.get("id") or input_data.get("channel_id")
    if not channel_id:
        return error("channel id is required")

    sender_id = input_data.get("sender_id")
    if not sender_id:
        return error("sender_id is required")

    sender_name = input_data.get("sender_name", sender_id)

    content = input_data.get("content")
    if not content:
        return error("content is required")

    metadata = input_data.get("metadata")

    manager = ChannelManager()
    channel = manager.get_channel(channel_id)
    if channel is None:
        return error("channel not found", code="NOT_FOUND")

    if channel["channel_type"] != "public" and sender_id not in channel["members"]:
        return error("sender is not a member of this channel", code="FORBIDDEN")

    messaging = MessagingService()
    message, mentions = messaging.send_message(
        channel_id=channel_id,
        sender_id=sender_id,
        sender_name=sender_name,
        content=content,
        thread_id=None,
        metadata=metadata,
        mention_values=channel["members"],
    )

    manager.touch_channel(channel_id)

    notification_service = NotificationService()
    members = channel["members"]
    notifications, agents_to_reply = notification_service.create_notifications(
        channel_id, message, members
    )

    agent_replies = []
    if agents_to_reply:
        agent_replies = notification_service.process_agent_replies(
            agents_to_reply=agents_to_reply,
            channel_id=channel_id,
            trigger_message=message,
            messaging_service=messaging,
            channel_manager=manager,
        )

    return ok({
        "message": message,
        "notifications": notifications,
        "agent_replies": agent_replies,
        "unresolved_mentions": message.get("unresolved_mentions", []),
    })
