import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from blocks._common import ok
from domain.memory.store import MemoryStore


def run(input_data, context=None):
    items = MemoryStore().long_term
    return ok({
        "enabled": True,
        "backend": "rumi.resource.memory.v1",
        "entry_count": len(items),
        "memo_folder_count": 0,
        "memo_note_count": 0,
        "files": [],
    })
