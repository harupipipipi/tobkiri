"""blocks/chat/channel/get_messages.py — Get messages from a channel."""


from blocks._common import ok, error
from domain.chat.channel_manager import ChannelManager
from domain.chat.messaging import MessagingService


def run(input_data, context):
    """Get messages for a channel.

    input_data:
        id        : str (required) Channel ID — injected from path param
        thread_id : str (optional) If provided, get messages in a specific thread
        limit     : int (optional, default 50)
        offset    : int (optional, default 0)
    """
    if not isinstance(input_data, dict):
        input_data = {}

    channel_id = input_data.get("id") or input_data.get("channel_id")
    if not channel_id:
        return error("channel id is required")

    manager = ChannelManager()
    channel = manager.get_channel(channel_id)
    if channel is None:
        return error("channel not found", code="NOT_FOUND")

    thread_id = input_data.get("thread_id")
    limit = input_data.get("limit", 50)
    offset = input_data.get("offset", 0)

    messaging = MessagingService()
    messages, total = messaging.get_messages(
        channel_id=channel_id,
        limit=limit,
        offset=offset,
        thread_id=thread_id,
    )

    return ok({"messages": messages, "total": total, "channel_id": channel_id})
