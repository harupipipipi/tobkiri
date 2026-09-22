"""blocks/chat/channel/delete.py — Delete a channel."""


from blocks._common import ok, error
from domain.chat.channel_manager import ChannelManager
from domain.chat.messaging import MessagingService


def run(input_data, context):
    """Delete a channel and all its messages.

    input_data:
        id : str (required) Channel ID — injected from path param
    """
    if not isinstance(input_data, dict):
        return error("input_data must be a dict")

    channel_id = input_data.get("id") or input_data.get("channel_id")
    if not channel_id:
        return error("channel id is required")

    manager = ChannelManager()
    deleted = manager.delete_channel(channel_id)
    if not deleted:
        return error("channel not found", code="NOT_FOUND")

    messaging = MessagingService()
    messaging.delete_channel_messages(channel_id)

    return ok({"deleted": True, "channel_id": channel_id})
