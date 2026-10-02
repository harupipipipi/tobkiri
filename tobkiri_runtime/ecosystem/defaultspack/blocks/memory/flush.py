

from blocks._common import ok
from domain.memory.store import MemoryStore


def run(input_data, context=None):
    items = input_data.get("items", [])
    if isinstance(input_data.get("content"), str):
        items = [input_data["content"]]
    if not isinstance(items, list):
        items = [str(items)]
    metadata = input_data.get("metadata", {})
    metadata = metadata if isinstance(metadata, dict) else {}
    scope = input_data.get("scope", "session")
    store = MemoryStore()
    refs = [
        store.store(str(item), {"scope": scope, **metadata})
        for item in items
    ]
    return ok({"memory_flush_refs": refs})
