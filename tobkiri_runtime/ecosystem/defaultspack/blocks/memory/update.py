import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from blocks._common import error, ok
from domain.memory.store import MemoryStore


def run(input_data, context=None):
    memory_id = input_data.get("id") or input_data.get("memory_id")
    if not memory_id:
        return error("memory_id is required", "INVALID_INPUT")
    updates = input_data.get("updates", {})
    if not isinstance(updates, dict):
        updates = {key: value for key, value in input_data.items() if key not in {"id", "memory_id"}}
    store = MemoryStore()
    current = next(
        (item for item in store.long_term if item.get("id") == memory_id),
        None,
    )
    if current is None:
        return error("memory not found", "NOT_FOUND")
    entry = store.update(memory_id, updates)
    return ok(entry)
