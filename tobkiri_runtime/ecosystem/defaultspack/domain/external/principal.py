from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class ExternalPrincipal:
    type: str
    id: str
    display_name: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "type": str(self.type or "unknown"),
            "id": str(self.id or ""),
        }
        if self.display_name:
            data["display_name"] = self.display_name
        if self.metadata:
            data["metadata"] = dict(self.metadata)
        return data


def principal_from(value: Any, *, default_type: str = "unknown") -> ExternalPrincipal:
    if isinstance(value, ExternalPrincipal):
        return value
    if not isinstance(value, dict):
        return ExternalPrincipal(default_type, str(value or ""))
    metadata_value = value.get("metadata")
    metadata: dict[str, object] = (
        {str(key): item for key, item in metadata_value.items()}
        if isinstance(metadata_value, dict)
        else {}
    )
    return ExternalPrincipal(
        type=str(value.get("type") or default_type or "unknown"),
        id=str(value.get("id") or ""),
        display_name=str(value.get("display_name") or ""),
        metadata=dict(metadata),
    )
