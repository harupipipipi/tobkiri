"""blocks/chat/channel/join.py — Add a member to a channel."""


from blocks._common import ok, error
from domain.chat.channel_manager import ChannelManager


def run(input_data, context):
    """Add a member to a channel.

    input_data:
        id        : str (required) Channel ID — injected from path param
        member_id : str (required) The member to add
    """
    if not isinstance(input_data, dict):
        return error("input_data must be a dict")

    channel_id = input_data.get("id") or input_data.get("channel_id")
    if not channel_id:
        return error("channel id is required")

    member_id = input_data.get("member_id")
    if not member_id:
        return error("member_id is required")

    manager = ChannelManager()

    channel = manager.get_channel(channel_id)
    if channel is None:
        return error("channel not found", code="NOT_FOUND")

    if channel["channel_type"] == "direct":
        return error("cannot add members to a direct channel")

    channel, err = manager.add_member(channel_id, member_id)
    if err is not None:
        return error(err)

    return ok(channel)
