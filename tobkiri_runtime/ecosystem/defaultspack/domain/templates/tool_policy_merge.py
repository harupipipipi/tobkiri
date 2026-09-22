from __future__ import annotations

import hashlib
from copy import deepcopy
from dataclasses import dataclass
from typing import Any

from ._helpers import canonical_json, payload_source, sorted_unique_strings, string_list


ALLOWLIST_KEYS = (
    "allowedToolIds",
    "allowed_tool_ids",
    "allowed_tools",
    "allowlist",
    "tool_allowlist",
)
DENYLIST_KEYS = (
    "deniedToolIds",
    "denied_tool_ids",
    "denied_tools",
    "denylist",
    "tool_denylist",
    "tool_blocklist",
    "disabled_tools",
    "default_disabled_tools",
    "defaultDisabledTools",
)
DEFAULT_ENABLED_KEYS = ("default_enabled_tools", "defaultEnabledTools")
DEFAULT_DISABLED_KEYS = ("default_disabled_tools", "defaultDisabledTools")
SELECTED_TOOLS_KEYS = ("selected_tools", "selectedTools")
TOOL_CHOICE_VALUES = {"auto", "none", "required"}
TEMPLATE_POLICY_AUTHORITY_FIELDS = frozenset(
    [
        *ALLOWLIST_KEYS,
        *DENYLIST_KEYS,
        *DEFAULT_ENABLED_KEYS,
        *DEFAULT_DISABLED_KEYS,
        *SELECTED_TOOLS_KEYS,
        "tool_choice",
        "parallel_tool_calls",
        "toggleable",
        "params",
    ]
)


@dataclass(frozen=True)
class NormalizedTemplateToolPolicy:
    source_ids: tuple[str, ...]
    projected_ids: tuple[str, ...]
    has_allowlist: bool
    allowlist: tuple[str, ...]
    denylist: tuple[str, ...]
    default_enabled_tools: tuple[str, ...]
    default_disabled_tools: tuple[str, ...]
    selected_tools: tuple[str, ...]
    tool_choice: str | dict[str, Any] | None
    parallel_tool_calls: bool | None
    toggleable: bool | None
    params: dict[str, Any]


@dataclass
class MergedTemplateToolPolicy:
    policy: dict[str, Any]
    source_ids: list[str]
    projected_ids: list[str]
    composed_id: str
    diagnostics: list[dict[str, Any]]
    blocked: bool = False


def normalize_template_tool_policy(item: dict[str, Any]) -> NormalizedTemplateToolPolicy:
    source = payload_source(item, "policy", "tool_policy")
    source_ids = sorted_unique_strings(
        [
            item.get("id"),
            item.get("policy_id"),
            item.get("tool_policy_id"),
            source.get("id"),
            source.get("policy_id"),
            source.get("tool_policy_id"),
        ]
    )
    projected_ids = sorted_unique_strings(
        [
            item.get("projected_id"),
            item.get("template_tool_policy_projected_id"),
            source.get("projected_id"),
        ]
    )
    allowlist, has_allowlist = _merged_string_list_with_presence(source, ALLOWLIST_KEYS)
    denylist = _merged_string_list(source, DENYLIST_KEYS)
    default_enabled = _merged_string_list(source, DEFAULT_ENABLED_KEYS)
    default_disabled = _merged_string_list(source, DEFAULT_DISABLED_KEYS)
    selected_tools = _merged_string_list(source, SELECTED_TOOLS_KEYS)
    raw_params = source.get("params")
    params = raw_params if isinstance(raw_params, dict) else {}
    return NormalizedTemplateToolPolicy(
        source_ids=tuple(source_ids),
        projected_ids=tuple(projected_ids),
        has_allowlist=has_allowlist,
        allowlist=tuple(allowlist),
        denylist=tuple(denylist),
        default_enabled_tools=tuple(default_enabled),
        default_disabled_tools=tuple(default_disabled),
        selected_tools=tuple(selected_tools),
        tool_choice=_valid_tool_choice(source.get("tool_choice")),
        parallel_tool_calls=source.get("parallel_tool_calls")
        if isinstance(source.get("parallel_tool_calls"), bool)
        else None,
        toggleable=source.get("toggleable") if isinstance(source.get("toggleable"), bool) else None,
        params=deepcopy(params),
    )


def merge_template_tool_policies(
    policies: list[dict[str, Any]],
    *,
    request_disabled_tools: list[str] | None = None,
) -> MergedTemplateToolPolicy:
    normalized = [
        normalize_template_tool_policy(item) for item in policies if isinstance(item, dict)
    ]
    diagnostics: list[dict[str, Any]] = []
    source_ids = sorted_unique_strings(
        source_id for policy in normalized for source_id in policy.source_ids
    )
    projected_ids = sorted_unique_strings(
        projected_id for policy in normalized for projected_id in policy.projected_ids
    )

    restrictive = [policy.allowlist for policy in normalized if policy.has_allowlist]
    has_allowlist = bool(restrictive)
    allowlist = list(restrictive[0]) if restrictive else []
    for policy_allowlist in restrictive[1:]:
        allowed = set(policy_allowlist)
        allowlist = [item for item in allowlist if item in allowed]
    allowlist = sorted(set(allowlist))

    denylist = sorted_unique_strings(
        [
            *(tool for policy in normalized for tool in policy.denylist),
            *(request_disabled_tools or []),
        ]
    )
    denied = set(denylist)
    if has_allowlist:
        allowlist = [tool for tool in allowlist if tool not in denied]
    allowed = set(allowlist)

    default_enabled = [
        tool
        for tool in sorted_unique_strings(
            tool for policy in normalized for tool in policy.default_enabled_tools
        )
        if tool not in denied and (not has_allowlist or tool in allowed)
    ]
    default_disabled = sorted_unique_strings(
        [
            *(tool for policy in normalized for tool in policy.default_disabled_tools),
            *denylist,
        ]
    )
    selected_tools = [
        tool
        for tool in sorted_unique_strings(
            tool for policy in normalized for tool in policy.selected_tools
        )
        if tool not in denied and (not has_allowlist or tool in allowed)
    ]
    tool_choice = _merge_tool_choice([policy.tool_choice for policy in normalized], diagnostics)
    parallel_tool_calls = _merge_bool([policy.parallel_tool_calls for policy in normalized])
    toggleable = _merge_bool([policy.toggleable for policy in normalized])
    params = _merge_params([policy.params for policy in normalized], diagnostics)

    policy: dict[str, Any] = {}
    if has_allowlist:
        policy["tool_allowlist"] = allowlist
    if denylist:
        policy["tool_denylist"] = denylist
    if default_enabled:
        policy["default_enabled_tools"] = default_enabled
    if default_disabled:
        policy["default_disabled_tools"] = default_disabled
    if selected_tools:
        policy["selected_tools"] = selected_tools
    if tool_choice is not None:
        policy["tool_choice"] = tool_choice
    if parallel_tool_calls is not None:
        policy["parallel_tool_calls"] = parallel_tool_calls
    if toggleable is not None:
        policy["toggleable"] = toggleable
    if params:
        policy["params"] = params

    composed_id = _composed_id(source_ids, projected_ids, policy)
    policy["template_tool_policy_id"] = source_ids[0] if len(source_ids) == 1 else composed_id
    policy["template_tool_policy_ids"] = source_ids
    policy["template_tool_policy_projected_ids"] = projected_ids
    policy["template_tool_policy_projected_id"] = (
        projected_ids[0] if len(projected_ids) == 1 else ""
    )
    policy["composed_tool_policy_id"] = composed_id
    return MergedTemplateToolPolicy(
        policy=policy,
        source_ids=source_ids,
        projected_ids=projected_ids,
        composed_id=composed_id,
        diagnostics=diagnostics,
    )


def blocked_template_tool_policy(
    *,
    request_disabled_tools: list[str] | None = None,
    diagnostics: list[dict[str, Any]] | None = None,
) -> MergedTemplateToolPolicy:
    denylist = sorted_unique_strings(request_disabled_tools or [])
    policy: dict[str, Any] = {
        "tool_allowlist": [],
        "tool_choice": "none",
        "template_policy_blocked": True,
    }
    if denylist:
        policy["tool_denylist"] = denylist
    composed_id = _composed_id([], [], policy)
    policy["composed_tool_policy_id"] = composed_id
    return MergedTemplateToolPolicy(
        policy=policy,
        source_ids=[],
        projected_ids=[],
        composed_id=composed_id,
        diagnostics=list(diagnostics or []),
        blocked=True,
    )


def _merged_string_list(source: dict[str, Any], keys: tuple[str, ...]) -> list[str]:
    values: list[str] = []
    for key in keys:
        if key in source:
            values.extend(string_list(source.get(key)))
    return sorted_unique_strings(values)


def _merged_string_list_with_presence(
    source: dict[str, Any], keys: tuple[str, ...]
) -> tuple[list[str], bool]:
    present = any(key in source for key in keys)
    return _merged_string_list(source, keys), present


def _valid_tool_choice(value: Any) -> str | dict[str, Any] | None:
    if isinstance(value, str):
        normalized = value.strip().lower()
        return normalized if normalized in TOOL_CHOICE_VALUES else None
    if isinstance(value, dict):
        return deepcopy(value)
    return None


def _same_tool_choice(left: Any, right: Any) -> bool:
    return canonical_json(left) == canonical_json(right)


def _merge_tool_choice(values: list[Any], diagnostics: list[dict[str, Any]]) -> Any:
    defined = [value for value in values if value is not None]
    if not defined:
        return None
    first = defined[0]
    if all(_same_tool_choice(first, value) for value in defined):
        return deepcopy(first)
    if any(value == "none" for value in defined):
        return "none"
    diagnostics.append(
        {
            "level": "warning",
            "severity": "warning",
            "code": "template.tool_policy.conflicting_tool_choice",
            "message": "tool_choice values conflict across template tool policies; using auto",
        }
    )
    return "auto"


def _merge_bool(values: list[bool | None]) -> bool | None:
    defined = [value for value in values if isinstance(value, bool)]
    if not defined:
        return None
    if any(value is False for value in defined):
        return False
    return True


def _merge_params(
    params_values: list[dict[str, Any]],
    diagnostics: list[dict[str, Any]],
) -> dict[str, Any]:
    merged: dict[str, Any] = {}
    conflicts: set[str] = set()
    for params in params_values:
        for key, value in params.items():
            if key in conflicts:
                continue
            if key not in merged:
                merged[key] = deepcopy(value)
                continue
            if canonical_json(merged[key]) == canonical_json(value):
                continue
            conflicts.add(key)
            merged.pop(key, None)
            diagnostics.append(
                {
                    "level": "warning",
                    "severity": "warning",
                    "code": "template.tool_policy.conflicting_param",
                    "message": f"tool policy params.{key} conflicts across templates and was removed",
                    "param": key,
                }
            )
    return merged


def _composed_id(source_ids: list[str], projected_ids: list[str], policy: dict[str, Any]) -> str:
    canonical = canonical_json(
        {
            "source_ids": source_ids,
            "projected_ids": projected_ids,
            "policy": policy,
        }
    )
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]
    return f"composed_tool_policy:{digest}"
