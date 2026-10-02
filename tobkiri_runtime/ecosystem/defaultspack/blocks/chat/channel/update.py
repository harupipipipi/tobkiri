"""blocks/chat/channel/update.py — Update a channel."""


from blocks._common import ok, error
from domain.chat.channel_manager import ChannelManager


def run(input_data, context):
    """Update channel properties.

    input_data:
        id          : str  (required) Channel ID — injected from path param
        name        : str  (optional) New name
        description : str  (optional) New description
        (channel_type, created_by, created_at, id are protected and cannot be changed)
    """
    if not isinstance(input_data, dict):
        return error("input_data must be a dict")

    channel_id = input_data.get("id") or input_data.get("channel_id")
    if not channel_id:
        return error("channel id is required")

    updates = {}
    if "name" in input_data:
        updates["name"] = input_data["name"]
    if "description" in input_data:
        updates["description"] = input_data["description"]
    if "members" in input_data:
        updates["members"] = input_data["members"]

    if not updates:
        return error("no update fields provided")

    manager = ChannelManager()
    channel, err = manager.update_channel(channel_id, updates)
    if err is not None:
        return error(err)

    return ok(channel)
