import json
from pathlib import Path
import re


from blocks._common import error, ok


def _pack_root():
    return Path(__file__).resolve().parents[2]


def run(input_data, context=None):
    compact_id = input_data.get("compact_id")
    if not compact_id:
        return error("'compact_id' is required", code="INVALID_INPUT")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", str(compact_id)):
        return error("'compact_id' contains unsafe characters", code="INVALID_INPUT")
    path = _pack_root() / "user_data" / "context" / (compact_id + ".json")
    root = (_pack_root() / "user_data" / "context").resolve()
    try:
        path.resolve().relative_to(root)
    except ValueError:
        return error("'compact_id' resolves outside context store", code="INVALID_INPUT")
    if not path.is_file():
        return error("Compact context not found", code="NOT_FOUND")
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["restored"] = True
    return ok(payload)
