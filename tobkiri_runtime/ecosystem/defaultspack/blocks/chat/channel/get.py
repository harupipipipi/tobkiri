"""blocks/chat/channel/get.py — Get channel details."""


from blocks._common import ok, error
from domain.chat.channel_manager import ChannelManager


def run(input_data, context):
    """Get a channel by ID.

    input_data:
        id : str (required) Channel ID — injected from path param
    """
    if not isinstance(input_data, dict):
        return error("input_data must be a dict")

    channel_id = input_data.get("id") or input_data.get("channel_id")
    if not channel_id:
        return error("channel id is required")

    manager = ChannelManager()
    channel = manager.get_channel(channel_id)
    if channel is None:
        return error("channel not found", code="NOT_FOUND")

    return ok(channel)
