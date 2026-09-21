import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from blocks._common import error, ok
from domain.memory.store import MemoryStore


def run(input_data, context=None):
    memory_id = input_data.get("id") or input_data.get("memory_id")
    if not memory_id:
        return error("memory_id is required", "INVALID_INPUT")
    return ok({"deleted": MemoryStore().delete(memory_id)})
