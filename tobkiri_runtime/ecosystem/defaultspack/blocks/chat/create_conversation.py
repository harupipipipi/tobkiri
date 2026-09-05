import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
from blocks._common import ok

from domain.chat.store import ChatStore


def run(input_data, context):
    store = ChatStore()
    model = input_data.get("model")
    # Prompt selection is an explicit conversation input.  A mutable
    # profiles/<id>/profile.yaml file is never an execution authority.
    system_prompt_id = input_data.get("system_prompt_id")
    agent_id = input_data.get("agent_id")
    tags = input_data.get("tags")
    parent_conversation_id = input_data.get("parent_conversation_id")
    conversation_kind = input_data.get("conversation_kind")
    metadata = input_data.get("metadata")
    group_id = input_data.get("group_id")
    conv = store.create_conversation(
        model=model,
        system_prompt_id=system_prompt_id,
        agent_id=agent_id,
        tags=tags,
        parent_conversation_id=parent_conversation_id,
        conversation_kind=conversation_kind,
        metadata=metadata,
        group_id=group_id,
    )
    return ok(conv)
