"""Tool CRUD persistence helpers."""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from ...domain.tool.registry import ToolRegistry as DomainToolRegistry


@dataclass
class ToolEntry:
    tool_id: str = ""
    tool_uuid: str = ""
    display_name: str = ""
    icon: str = ""
    description: str = ""
    enabled: bool = True
    schema: Dict[str, Any] = field(default_factory=dict)
    handler_path: str = ""
    consent_required: bool = False
    tags: List[str] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.tool_uuid:
            self.tool_uuid = str(uuid.uuid5(uuid.NAMESPACE_DNS, f"tool:{self.tool_id}"))

    def to_dict(self) -> Dict[str, Any]:
        return {
            "tool_id": self.tool_id,
            "tool_uuid": self.tool_uuid,
            "display_name": self.display_name,
            "icon": self.icon,
            "description": self.description,
            "enabled": self.enabled,
            "schema": self.schema,
            "handler_path": self.handler_path,
            "consent_required": self.consent_required,
            "tags": self.tags,
            "metadata": self.metadata,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ToolEntry":
        known = {k: v for k, v in data.items() if k in cls.__dataclass_fields__}
        return cls(**known)


ToolDefinition = ToolEntry


class ToolManager:
    def __init__(self, tools_dir: Optional[Path] = None) -> None:
        self._domain = DomainToolRegistry()
        self._tools: Dict[str, ToolEntry] = {}
        self._dir = tools_dir
        if self._dir is not None:
            self._dir.mkdir(parents=True, exist_ok=True)
            self._load_from_disk()

    def _path(self, tool_id: str) -> Optional[Path]:
        if self._dir is None:
            return None
        return self._dir / f"{tool_id}.json"

    def _load_from_disk(self) -> None:
        if self._dir is None:
            return
        for path in sorted(self._dir.glob("*.json")):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                continue
            entry = ToolEntry.from_dict(data)
            self._tools[entry.tool_id or path.stem] = entry

    def get(self, tool_id: str) -> Optional[ToolEntry]:
        return self._tools.get(tool_id)

    def get_tool(self, tool_id: str) -> Optional[ToolEntry]:
        return self.get(tool_id)

    def list_all(self) -> List[ToolEntry]:
        return list(self._tools.values())

    def list_tools(self) -> List[ToolEntry]:
        return self.list_all()

    def list_enabled(self) -> List[ToolEntry]:
        return [tool for tool in self._tools.values() if tool.enabled]

    def create(self, entry: ToolEntry) -> ToolEntry:
        self._tools[entry.tool_id] = entry
        self._domain.register(
            {
                "tool_id": entry.tool_id,
                "name": entry.display_name or entry.tool_id,
                "summary": entry.description,
                "tags": list(entry.tags),
                "schema": entry.schema,
                "execution": {"type": "local", "handler_path": entry.handler_path},
            }
        )
        self._persist(entry)
        return entry

    def register(self, entry: ToolEntry | Dict[str, Any]) -> ToolEntry:
        if isinstance(entry, dict):
            entry = ToolEntry.from_dict(entry)
        return self.create(entry)

    def toggle(self, tool_id: str, enabled: bool) -> bool:
        entry = self._tools.get(tool_id)
        if entry is None:
            return False
        entry.enabled = enabled
        self._persist(entry)
        return True

    def set_enabled(self, tool_id: str, enabled: bool) -> bool:
        return self.toggle(tool_id, enabled)

    def delete(self, tool_id: str) -> bool:
        entry = self._tools.pop(tool_id, None)
        if entry is None:
            return False
        self._domain.unregister(tool_id)
        if self._dir is not None:
            (self._dir / f"{tool_id}.json").unlink(missing_ok=True)
        return True

    def unregister(self, tool_id: str) -> bool:
        return self.delete(tool_id)

    def generate_index(self) -> List[Dict[str, Any]]:
        return [tool.to_dict() for tool in self._tools.values()]

    def _persist(self, entry: ToolEntry) -> None:
        if self._dir is None:
            return
        self._dir.mkdir(parents=True, exist_ok=True)
        (self._dir / f"{entry.tool_id}.json").write_text(
            json.dumps(entry.to_dict(), indent=2, ensure_ascii=False),
            encoding="utf-8",
        )


_TOOL_MANAGER: ToolManager | None = None


def get_tool_manager() -> ToolManager:
    global _TOOL_MANAGER
    if _TOOL_MANAGER is None:
        _TOOL_MANAGER = ToolManager()
    return _TOOL_MANAGER
