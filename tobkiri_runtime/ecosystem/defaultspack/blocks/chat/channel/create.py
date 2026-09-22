"""blocks/chat/channel/create.py — Create a channel."""


from blocks._common import ok, error
from domain.chat.channel_manager import ChannelManager


def run(input_data, context):
    """Create a new channel.

    input_data:
        name         : str  (required) Channel name
        channel_type : str  (optional) "public" | "private" | "direct" (default: "public")
        description  : str  (optional) Channel description
        created_by   : str  (optional) Creator's ID
        members      : list (optional) Initial member IDs
    """
    if not isinstance(input_data, dict):
        return error("input_data must be a dict")

    name = input_data.get("name")
    if not name:
        return error("name is required")

    channel_type = input_data.get("channel_type", "public")
    description = input_data.get("description", "")
    created_by = input_data.get("created_by")
    members = input_data.get("members")

    manager = ChannelManager()
    channel, err = manager.create_channel(
        name=name,
        channel_type=channel_type,
        description=description,
        created_by=created_by,
        members=members,
    )
    if err is not None:
        return error(err)

    return ok(channel)
