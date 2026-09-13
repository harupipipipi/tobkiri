import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from blocks._common import ok
from domain.memory.store import MemoryStore


def run(input_data, context=None):
    input_data = input_data if isinstance(input_data, dict) else {}
    query = str(input_data.get("query", input_data.get("q", ""))) if isinstance(input_data, dict) else ""
    limit = int(input_data.get("limit", 5) if isinstance(input_data, dict) else 5)
    filters = input_data.get("filters", {}) if isinstance(input_data.get("filters", {}), dict) else {}
    for key in ("scope", "agent_id", "project_id"):
        if key in input_data:
            filters[key] = input_data[key]
    results = MemoryStore().search(query, limit=limit)
    if filters:
        results = [
            item
            for item in results
            if all(
                item.get(key) == value
                or (
                    isinstance(item.get("metadata"), dict)
                    and item["metadata"].get(key) == value
                )
                for key, value in filters.items()
            )
        ]
    return ok({"results": results})
