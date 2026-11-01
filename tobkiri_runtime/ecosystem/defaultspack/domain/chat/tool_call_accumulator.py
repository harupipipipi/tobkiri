from __future__ import annotations

import json
from typing import Any


class ToolCallAccumulator:
    """Collect provider stream tool call events into tool_use-like blocks."""

    def __init__(self) -> None:
        self._calls: dict[str, dict[str, Any]] = {}
        self._order: list[str] = []

    def ingest(self, chunk: dict[str, Any] | None) -> None:
        if not isinstance(chunk, dict):
            return
        chunk_type = str(chunk.get("type") or "").strip()
        if chunk_type == "tool_use":
            self._ingest_complete_call(chunk)
            return
        if chunk_type == "tool_call_start":
            call_id = self._call_id(chunk)
            current = self._call(call_id)
            current["name"] = str(chunk.get("name") or current.get("name") or "")
            current["started"] = True
            return
        if chunk_type == "tool_call_delta":
            call_id = self._call_id(chunk)
            current = self._call(call_id)
            current["name"] = str(chunk.get("name") or current.get("name") or "")
            arguments_chunk = chunk.get("arguments_chunk")
            if arguments_chunk not in (None, ""):
                current["arguments_parts"].append(str(arguments_chunk))
            current["started"] = True
            return
        if chunk_type == "tool_call_end":
            call_id = self._call_id(chunk)
            current = self._call(call_id)
            current["name"] = str(chunk.get("name") or current.get("name") or "")
            current["started"] = True
            current["ended"] = True

    def has_calls(self) -> bool:
        return bool(self._order)

    def tool_uses(self) -> list[dict[str, Any]]:
        blocks: list[dict[str, Any]] = []
        for call_id in self._order:
            current = self._calls.get(call_id) or {}
            arguments = self._parsed_arguments(current)
            if (
                not current.get("started")
                or not current.get("ended")
                or not str(current.get("name") or "").strip()
                or arguments is None
            ):
                continue
            blocks.append(
                {
                    "type": "tool_use",
                    "id": call_id,
                    "name": str(current.get("name") or ""),
                    "input": arguments,
                }
            )
        return blocks

    def arguments_for(self, call_id: str) -> dict[str, Any]:
        current = self._calls.get(str(call_id or "").strip()) or {}
        return self._parsed_arguments(current) or {}

    def _ingest_complete_call(self, block: dict[str, Any]) -> None:
        call_id = self._call_id(block)
        current = self._call(call_id)
        current["name"] = str(block.get("name") or block.get("tool_name") or current.get("name") or "")
        tool_input = block.get("input", {})
        current["arguments_parts"] = [
            tool_input
            if isinstance(tool_input, str)
            else json.dumps(tool_input, ensure_ascii=False)
        ]
        current["started"] = True
        current["ended"] = True

    def _call(self, call_id: str) -> dict[str, Any]:
        call_id = str(call_id or "").strip() or "tool_call_1"
        if call_id not in self._calls:
            self._calls[call_id] = {
                "id": call_id,
                "name": "",
                "arguments_parts": [],
                "started": False,
                "ended": False,
            }
            self._order.append(call_id)
        return self._calls[call_id]

    @staticmethod
    def _call_id(value: dict[str, Any]) -> str:
        return str(
            value.get("id")
            or value.get("tool_call_id")
            or value.get("call_id")
            or "tool_call_1"
        ).strip() or "tool_call_1"

    @staticmethod
    def _parsed_arguments(current: dict[str, Any]) -> dict[str, Any] | None:
        raw = "".join(str(part) for part in current.get("arguments_parts") or [])
        if not raw.strip():
            return {}
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return None
        return parsed if isinstance(parsed, dict) else None
