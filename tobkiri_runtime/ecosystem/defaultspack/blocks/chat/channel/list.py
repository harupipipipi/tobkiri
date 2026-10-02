"""blocks/chat/channel/list.py — List channels."""


from blocks._common import ok, error
from domain.chat.channel_manager import ChannelManager


def run(input_data, context):
    """List channels with optional filters.

    input_data:
        channel_type : str (optional) Filter by type
        member_id    : str (optional) Filter by membership
        limit        : int (optional, default 50)
        offset       : int (optional, default 0)
    """
    if not isinstance(input_data, dict):
        input_data = {}

    channel_type = input_data.get("channel_type")
    member_id = input_data.get("member_id")
    limit = input_data.get("limit", 50)
    offset = input_data.get("offset", 0)

    manager = ChannelManager()
    channels, total = manager.list_channels(
        channel_type=channel_type,
        member_id=member_id,
        limit=limit,
        offset=offset,
    )
    return ok({"channels": channels, "total": total})
