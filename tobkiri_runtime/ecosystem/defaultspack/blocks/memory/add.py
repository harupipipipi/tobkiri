import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from blocks._common import error, ok
from domain.memory.store import MemoryStore


def run(input_data, context=None):
    content = input_data.get("content") if isinstance(input_data, dict) else None
    if not content:
        return error("content is required", "INVALID_INPUT")
    metadata = input_data.get("metadata", {}) if isinstance(input_data.get("metadata", {}), dict) else {}
    scope = input_data.get("scope", "user")
    details = {
        **metadata,
        "scope": scope,
        "agent_id": input_data.get("agent_id"),
        "project_id": input_data.get("project_id"),
        "source": input_data.get("source", "manual"),
    }
    entry = MemoryStore().store(str(content), details)
    return ok(entry)
