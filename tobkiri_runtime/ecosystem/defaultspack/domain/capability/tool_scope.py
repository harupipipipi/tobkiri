"""Explicit three-state Runtime Profile Tool scope."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable


TOOL_SCOPE_MODES = {"inherit", "none", "allowlist"}


@dataclass(frozen=True, slots=True)
class ToolScope:
    """A non-ambiguous Runtime Profile Tool restriction."""

    mode: str = "inherit"
    ids: tuple[str, ...] = ()

    def allows(self, tool_id: str) -> bool:
        """Return whether the Tool ID is allowed by this scope."""

        if self.mode == "inherit":
            return True
        if self.mode == "none":
            return False
        return str(tool_id or "").strip() in self.ids

    def to_dict(self) -> dict[str, Any]:
        """Return the runtime profile wire representation."""

        return {"mode": self.mode, "ids": list(self.ids)}


def normalize_tool_scope(value: Any) -> ToolScope:
    """Normalize v2 scope while preserving legacy profile compatibility."""

    if isinstance(value, ToolScope):
        return value
    if value is None:
        return ToolScope()
    if isinstance(value, list):
        ids = _unique_ids(value)
        return (
            ToolScope(mode="allowlist", ids=ids)
            if ids
            else ToolScope(mode="none")
        )
    if not isinstance(value, dict):
        return ToolScope()
    nested = value.get("tool_scope")
    if isinstance(nested, dict):
        value = nested
    mode = str(value.get("mode") or "").strip().lower()
    ids = _unique_ids(value.get("ids"))
    if mode not in TOOL_SCOPE_MODES:
        legacy = value.get("tools")
        if isinstance(legacy, list):
            legacy_ids = _unique_ids(legacy)
            return (
                ToolScope(mode="allowlist", ids=legacy_ids)
                if legacy_ids
                else ToolScope(mode="none")
            )
        return ToolScope()
    if mode == "allowlist" and not ids:
        raise ValueError("tool_scope mode=allowlist requires at least one id")
    return ToolScope(mode=mode, ids=ids if mode == "allowlist" else ())


def filter_tool_ids(tool_ids: Iterable[str], scope: ToolScope) -> list[str]:
    """Filter Tool IDs while preserving source order."""

    return [tool_id for tool_id in tool_ids if scope.allows(tool_id)]


def _unique_ids(value: Any) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple, set)):
        return ()
    ids: list[str] = []
    for item in value:
        tool_id = str(item or "").strip()
        if tool_id and tool_id not in ids:
            ids.append(tool_id)
    return tuple(ids)
