from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ._helpers import ordered_unique_strings, payload_source, string_list
from .catalog_runtime import get_template_catalog_snapshot
from .tool_policy_merge import (
    TEMPLATE_POLICY_AUTHORITY_FIELDS,
    blocked_template_tool_policy,
    merge_template_tool_policies,
)


_AI_INPUT_ID_KEYS = ("ai_input_id", "template_ai_input_id", "ai_input")
_AI_INPUT_LIST_KEYS = ("ai_input_ids", "template_ai_input_ids")
_TOOL_POLICY_ID_KEYS = ("template_tool_policy_id", "tool_policy_id")
_TOOL_POLICY_LIST_KEYS = ("template_tool_policy_ids", "tool_policy_ids")
_DISABLED_KEYS = ("disabled_tools",)


def _list_or_empty(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


@dataclass
class TemplateToolPolicyResolution:
    policy: dict[str, Any]
    id_requested: bool = False
    catalog_available: bool = False
    applied: bool = False
    requested_ai_input_ids: list[str] = field(default_factory=list)
    requested_template_tool_policy_ids: list[str] = field(default_factory=list)
    resolved_ai_input_ids: list[str] = field(default_factory=list)
    resolved_template_tool_policy_ids: list[str] = field(default_factory=list)
    resolved_template_tool_policy_projected_ids: list[str] = field(default_factory=list)
    composed_policy_id: str = ""
    blocked: bool = False
    diagnostics: list[dict[str, Any]] = field(default_factory=list)

    @property
    def requested_ai_input_id(self) -> str:
        return self.requested_ai_input_ids[0] if self.requested_ai_input_ids else ""

    @property
    def requested_template_tool_policy_id(self) -> str:
        return (
            self.requested_template_tool_policy_ids[0]
            if self.requested_template_tool_policy_ids
            else ""
        )

    @property
    def resolved_ai_input_id(self) -> str:
        return self.resolved_ai_input_ids[0] if self.resolved_ai_input_ids else ""

    @property
    def resolved_template_tool_policy_id(self) -> str:
        return (
            self.resolved_template_tool_policy_ids[0]
            if self.resolved_template_tool_policy_ids
            else ""
        )

    def to_context(self) -> dict[str, Any]:
        return {
            "id_requested": self.id_requested,
            "catalog_available": self.catalog_available,
            "applied": self.applied,
            "requested_ai_input_id": self.requested_ai_input_id or None,
            "requested_template_tool_policy_id": self.requested_template_tool_policy_id or None,
            "resolved_ai_input_id": self.resolved_ai_input_id or None,
            "resolved_template_tool_policy_id": self.resolved_template_tool_policy_id or None,
            "requested_ai_input_ids": list(self.requested_ai_input_ids),
            "requested_template_tool_policy_ids": list(self.requested_template_tool_policy_ids),
            "resolved_ai_input_ids": list(self.resolved_ai_input_ids),
            "resolved_template_tool_policy_ids": list(self.resolved_template_tool_policy_ids),
            "resolved_template_tool_policy_projected_ids": list(
                self.resolved_template_tool_policy_projected_ids
            ),
            "composed_policy_id": self.composed_policy_id or None,
            "blocked": self.blocked,
            "diagnostics": [dict(item) for item in self.diagnostics],
        }


def resolve_template_tool_policy(
    request_policy: dict[str, Any] | None,
    *,
    metadata: dict[str, Any] | None = None,
    catalog: dict[str, Any] | None = None,
    defaultspack_root: str | Path | None = None,
) -> TemplateToolPolicyResolution:
    """Resolve request template ids to an authoritative backend tool policy."""
    raw_policy = deepcopy(request_policy) if isinstance(request_policy, dict) else {}
    requested_ai_input_ids = _collect_ids(raw_policy, metadata, keys=_AI_INPUT_ID_KEYS)
    requested_ai_input_ids = ordered_unique_strings(
        [
            *requested_ai_input_ids,
            *_collect_ids(raw_policy, metadata, keys=_AI_INPUT_LIST_KEYS, list_only=True),
        ]
    )
    requested_policy_ids = _collect_ids(raw_policy, metadata, keys=_TOOL_POLICY_ID_KEYS)
    requested_policy_ids = ordered_unique_strings(
        [
            *requested_policy_ids,
            *_collect_ids(raw_policy, metadata, keys=_TOOL_POLICY_LIST_KEYS, list_only=True),
        ]
    )
    id_requested = bool(requested_ai_input_ids or requested_policy_ids)
    if not id_requested:
        return TemplateToolPolicyResolution(policy=raw_policy)

    request_disabled_tools = _first_string_list(raw_policy, _DISABLED_KEYS)
    loaded_catalog = (
        catalog if isinstance(catalog, dict) else _load_template_catalog(defaultspack_root)
    )
    if not isinstance(loaded_catalog, dict) or not _catalog_has_template_policy_surface(
        loaded_catalog
    ):
        merged = blocked_template_tool_policy(
            request_disabled_tools=request_disabled_tools,
            diagnostics=[
                _diagnostic(
                    "template_policy_catalog_unavailable",
                    ",".join([*requested_ai_input_ids, *requested_policy_ids]),
                )
            ],
        )
        return TemplateToolPolicyResolution(
            policy={**_strip_template_authority_fields(raw_policy), **merged.policy},
            id_requested=True,
            catalog_available=False,
            applied=True,
            requested_ai_input_ids=requested_ai_input_ids,
            requested_template_tool_policy_ids=requested_policy_ids,
            composed_policy_id=merged.composed_id,
            blocked=True,
            diagnostics=merged.diagnostics,
        )

    diagnostics: list[dict[str, Any]] = []
    resolved_ai_inputs: list[dict[str, Any]] = []
    policy_ids = list(requested_policy_ids)
    for requested_id in requested_ai_input_ids:
        resolved_ai_input = _find_catalog_item(
            _list_or_empty(loaded_catalog.get("ai_inputs")), requested_id
        )
        if resolved_ai_input is None:
            diagnostics.append(_diagnostic("template_ai_input_not_found", requested_id))
            continue
        resolved_ai_inputs.append(resolved_ai_input)
        policy_ids.extend(_candidate_policy_ids(resolved_ai_input))

    policy_ids = ordered_unique_strings(policy_ids)
    resolved_policies: list[dict[str, Any]] = []
    for policy_id in policy_ids:
        resolved_policy = _find_catalog_item(
            _list_or_empty(loaded_catalog.get("tool_policies")), policy_id
        )
        if resolved_policy is None:
            diagnostics.append(_diagnostic("template_tool_policy_not_found", policy_id))
            continue
        resolved_policies.append(resolved_policy)

    if not policy_ids and requested_ai_input_ids:
        diagnostics.append(
            _diagnostic(
                "template_ai_input_missing_tool_policy",
                ",".join(requested_ai_input_ids),
            )
        )

    if diagnostics or not resolved_policies:
        if not resolved_policies and not diagnostics:
            diagnostics.append(
                _diagnostic(
                    "template_tool_policy_not_found",
                    ",".join([*requested_ai_input_ids, *requested_policy_ids]),
                )
            )
        merged = blocked_template_tool_policy(
            request_disabled_tools=request_disabled_tools,
            diagnostics=diagnostics,
        )
        return TemplateToolPolicyResolution(
            policy={**_strip_template_authority_fields(raw_policy), **merged.policy},
            id_requested=True,
            catalog_available=True,
            applied=True,
            requested_ai_input_ids=requested_ai_input_ids,
            requested_template_tool_policy_ids=requested_policy_ids,
            resolved_ai_input_ids=_ids_for_items(
                resolved_ai_inputs, "id", "ai_input_id", "input_id"
            ),
            composed_policy_id=merged.composed_id,
            blocked=True,
            diagnostics=merged.diagnostics,
        )

    merged = merge_template_tool_policies(
        resolved_policies,
        request_disabled_tools=request_disabled_tools,
    )
    policy = _strip_template_authority_fields(raw_policy)
    policy.update(merged.policy)
    if requested_ai_input_ids:
        policy["ai_input_ids"] = list(requested_ai_input_ids)
        policy["ai_input_id"] = requested_ai_input_ids[0]

    return TemplateToolPolicyResolution(
        policy=policy,
        id_requested=True,
        catalog_available=True,
        applied=True,
        requested_ai_input_ids=requested_ai_input_ids,
        requested_template_tool_policy_ids=requested_policy_ids,
        resolved_ai_input_ids=_ids_for_items(resolved_ai_inputs, "id", "ai_input_id", "input_id"),
        resolved_template_tool_policy_ids=merged.source_ids,
        resolved_template_tool_policy_projected_ids=merged.projected_ids,
        composed_policy_id=merged.composed_id,
        blocked=merged.blocked,
        diagnostics=merged.diagnostics,
    )


def _load_template_catalog(defaultspack_root: str | Path | None) -> dict[str, Any] | None:
    root = (
        Path(defaultspack_root)
        if defaultspack_root is not None
        else Path(__file__).resolve().parents[2]
    )
    try:
        return get_template_catalog_snapshot(defaultspack_root=root).catalog
    except Exception:
        return None


def _catalog_has_template_policy_surface(catalog: dict[str, Any] | None) -> bool:
    if not isinstance(catalog, dict):
        return False
    return isinstance(catalog.get("tool_policies"), list)


def _collect_ids(
    *sources: dict[str, Any] | None,
    keys: tuple[str, ...],
    list_only: bool = False,
) -> list[str]:
    values: list[str] = []
    for source in sources:
        if not isinstance(source, dict):
            continue
        for key in keys:
            if key not in source:
                continue
            value = source.get(key)
            if list_only:
                values.extend(string_list(value))
            elif isinstance(value, list):
                values.extend(string_list(value))
            elif isinstance(value, str) and value.strip():
                values.append(value.strip())
            elif value not in (None, "", [], {}) and not isinstance(value, dict):
                values.append(str(value).strip())
    return ordered_unique_strings(values)


def _find_catalog_item(items: Any, requested_id: str) -> dict[str, Any] | None:
    requested = str(requested_id or "").strip()
    if not requested or not isinstance(items, list):
        return None
    for item in items:
        if _catalog_item_matches(item, requested):
            return item
    return None


def _catalog_item_matches(item: Any, requested_id: str) -> bool:
    if not isinstance(item, dict) or item.get("enabled") is False:
        return False
    requested = str(requested_id or "").strip()
    if not requested:
        return False
    keys = {
        str(item.get(key) or "").strip()
        for key in (
            "id",
            "projected_id",
            "piece_id",
            "policy_id",
            "tool_policy_id",
            "ai_input_id",
            "input_id",
        )
        if str(item.get(key) or "").strip()
    }
    template_id = str(item.get("template_id") or "").strip()
    piece_id = str(item.get("piece_id") or "").strip()
    item_id = str(item.get("id") or "").strip()
    if template_id and piece_id:
        keys.add(f"{template_id}:{piece_id}")
    if template_id and item_id:
        keys.add(f"{template_id}:{item_id}")
    return requested in keys


def _candidate_policy_ids(ai_input: dict[str, Any] | None) -> list[str]:
    if not isinstance(ai_input, dict):
        return []
    source = payload_source(ai_input, "input", "ai_input")
    metadata = source.get("metadata") if isinstance(source.get("metadata"), dict) else {}
    return ordered_unique_strings(
        [
            *_collect_ids(source, keys=("tool_policy_id", "tool_policy")),
            *_collect_ids(source, keys=("tool_policy_ids",), list_only=True),
            *_collect_ids(metadata, keys=("tool_policy_ids",), list_only=True),
        ]
    )


def _strip_template_authority_fields(policy: dict[str, Any]) -> dict[str, Any]:
    return {
        key: deepcopy(value)
        for key, value in policy.items()
        if key not in TEMPLATE_POLICY_AUTHORITY_FIELDS
        and key
        not in {
            *_AI_INPUT_ID_KEYS,
            *_AI_INPUT_LIST_KEYS,
            *_TOOL_POLICY_ID_KEYS,
            *_TOOL_POLICY_LIST_KEYS,
        }
    }


def _first_string_list(source: dict[str, Any], keys: tuple[str, ...]) -> list[str]:
    for key in keys:
        if key in source:
            values = string_list(source.get(key))
            if values:
                return values
    return []


def _ids_for_items(items: list[dict[str, Any]], *keys: str) -> list[str]:
    values: list[str] = []
    for item in items:
        for key in keys:
            value = str(item.get(key) or "").strip()
            if value:
                values.append(value)
                break
    return ordered_unique_strings(values)


def _diagnostic(code: str, requested_id: str) -> dict[str, Any]:
    return {
        "level": "error",
        "severity": "error",
        "code": f"template.tool_policy_resolution.{code}",
        "requested_id": requested_id,
    }
