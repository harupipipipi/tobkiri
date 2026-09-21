"""blocks/chat/channel/reply.py — Reply to a message in a thread."""

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", ".."))

from blocks._common import ok, error
from domain.chat.channel_manager import ChannelManager
from domain.chat.messaging import MessagingService
from domain.chat.notification import NotificationService


def run(input_data, context):
    """Reply to a message, creating or continuing a thread.

    input_data:
        id          : str (required) Channel ID — injected from path param
        msg_id      : str (required) Parent message ID — injected from path param
        sender_id   : str (required) Sender's ID
        sender_name : str (required) Sender's display name
        content     : str (required) Reply text (may contain @mentions)
        metadata    : dict (optional) Extra metadata
    """
    if not isinstance(input_data, dict):
        return error("input_data must be a dict")

    channel_id = input_data.get("id") or input_data.get("channel_id")
    if not channel_id:
        return error("channel id is required")

    parent_msg_id = input_data.get("msg_id") or input_data.get("message_id")
    if not parent_msg_id:
        return error("parent message id (msg_id) is required")

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

    parent_msg = messaging.get_message(channel_id, parent_msg_id)
    if parent_msg is None:
        return error("parent message not found", code="NOT_FOUND")

    actual_thread_id = parent_msg.get("thread_id") or parent_msg_id

    reply_msg, mentions = messaging.send_message(
        channel_id=channel_id,
        sender_id=sender_id,
        sender_name=sender_name,
        content=content,
        thread_id=actual_thread_id,
        metadata=metadata,
        mention_values=channel["members"],
    )

    manager.touch_channel(channel_id)

    notification_service = NotificationService()

    thread_root = messaging.get_message(channel_id, actual_thread_id)
    thread_notif = None
    if thread_root is not None:
        thread_notif = notification_service.create_thread_notification(
            channel_id, reply_msg, thread_root.get("sender_id", "")
        )

    members = channel["members"]
    mention_notifications, agents_to_reply = notification_service.create_notifications(
        channel_id, reply_msg, members
    )

    all_notifications = []
    if thread_notif is not None:
        all_notifications.append(thread_notif)
    all_notifications.extend(mention_notifications)

    agent_replies = []
    if agents_to_reply:
        agent_replies = notification_service.process_agent_replies(
            agents_to_reply=agents_to_reply,
            channel_id=channel_id,
            trigger_message=reply_msg,
            messaging_service=messaging,
            channel_manager=manager,
        )

    thread_messages, thread_total = messaging.get_thread(
        channel_id, actual_thread_id
    )

    return ok({
        "reply": reply_msg,
        "thread_id": actual_thread_id,
        "thread_messages": thread_messages,
        "thread_total": thread_total,
        "notifications": all_notifications,
        "agent_replies": agent_replies,
        "unresolved_mentions": reply_msg.get("unresolved_mentions", []),
    })
