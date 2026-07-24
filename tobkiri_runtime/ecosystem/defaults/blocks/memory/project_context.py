"""defaults.memory.project_context — プロジェクトコンテキスト操作ハンドラー.

input_data:
    action : str        — "get" または "set" (必須)
    key    : str        — 設定するキー   (action="set" 時に必須)
    value  : Any        — 設定する値     (action="set" 時に必須)

戻り値:
    {"status": "ok", "data": {"context": dict}}
"""

from __future__ import annotations

from typing import Any, Dict

from blocks._common import error, ok
from domain.memory.store import MemoryStore


def run(input_data: Dict[str, Any], context: Any = None) -> Dict[str, Any]:
    """プロジェクトコンテキストを取得または設定する."""
    try:
        action = input_data.get("action")
        if action not in ("get", "set"):
            return error(
                'action is required and must be "get" or "set"',
                "INVALID_INPUT",
            )

        store = MemoryStore()

        if action == "get":
            return ok({"context": dict(store.project_context)})

        # action == "set"
        key = input_data.get("key")
        if not key or not isinstance(key, str):
            return error("key is required and must be a non-empty string for set action", "INVALID_INPUT")

        if "value" not in input_data:
            return error("value is required for set action", "INVALID_INPUT")

        value = input_data["value"]
        store.put_project_context(key, value)

        return ok({"context": dict(store.project_context)})

    except Exception as exc:
        return error(f"Failed to handle project_context: {exc}", "PROJECT_CONTEXT_ERROR")
