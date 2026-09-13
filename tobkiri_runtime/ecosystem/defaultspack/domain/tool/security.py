from __future__ import annotations

from typing import Any

from .normalizers import list_or_empty, mapping_or_empty


TRUSTED_TOOL_PACK_IDS = {"defaultspack", "rumi_default_tools_pack"}
SUPPORTED_AUTHORABLE_EXECUTION_TYPES = {
    "rumi_function",
    "capability",
    "mcp",
    "global_contract",
}
TRUSTED_LEGACY_EXECUTION_TYPES = {"local", "handler", "dynamic"}
VALID_RISKS = {"low", "medium", "high", "critical"}
SANDBOX_CAPABILITY_PREFIX = "sandbox."
SANDBOX_TOOL_IDS = {
    "sandbox_terminal_exec",
    "sandbox_file_read",
    "sandbox_file_write",
    "sandbox_file_patch",
    "sandbox_diff_preview",
    "sandbox_artifact_export",
}
SANDBOX_FUNCTION_IDS = SANDBOX_TOOL_IDS
HOST_CODING_FUNCTION_PREFIXES = (
    "coding_",
    "browser_",
    "computer_",
)
HOST_CAPABILITY_PREFIXES = (
    "terminal.",
    "file.",
    "git.",
    "browser.",
    "computer.",
    "coding.",
)

_UNSAFE_ACTION_TYPES = {
    "create",
    "delete",
    "desktop",
    "execute",
    "file_write",
    "patch",
    "push",
    "shell",
    "update",
    "write",
}
_UNSAFE_CATEGORIES = {
    "computer",
    "desktop",
    "file_write",
    "filesystem_write",
    "shell",
}
_UNSAFE_TEXT_MARKERS = (
    "chmod",
    "commit",
    "create",
    "delete",
    "desktop",
    "execute",
    "file write",
    "filesystem",
    "git push",
    "modify",
    "patch",
    "remove",
    "shell",
    "subprocess",
    "terminal",
    "update",
    "write",
    "writing",
)


def source_pack_id_from_tool(tool_def: dict[str, Any] | None) -> str:
    if not isinstance(tool_def, dict):
        return ""
    metadata = tool_def.get("metadata")
    if isinstance(metadata, dict):
        value = metadata.get("source_pack_id")
        if isinstance(value, str) and value.strip():
            return value.strip()
    value = tool_def.get("source_pack_id")
    if isinstance(value, str) and value.strip():
        return value.strip()
    return ""


def source_pack_id_from_manifest(manifest: dict[str, Any], fallback: str = "") -> str:
    for source in (
        manifest,
        manifest.get("metadata") if isinstance(manifest.get("metadata"), dict) else {},
        manifest.get("config") if isinstance(manifest.get("config"), dict) else {},
    ):
        value = source.get("source_pack_id") if isinstance(source, dict) else None
        if isinstance(value, str) and value.strip():
            return value.strip()
    return str(fallback or "").strip()


def is_trusted_tool(tool_def: dict[str, Any] | None) -> bool:
    return source_pack_id_from_tool(tool_def) in TRUSTED_TOOL_PACK_IDS


def is_trusted_pack_id(pack_id: str) -> bool:
    normalized = str(pack_id or "").strip()
    if normalized in TRUSTED_TOOL_PACK_IDS:
        return True
    if not normalized:
        return False
    try:
        from core_runtime.pack_trust import is_pack_trusted

        return bool(is_pack_trusted(normalized)[0])
    except Exception:
        return False


def execution_type(tool_def: dict[str, Any] | None) -> str:
    if not isinstance(tool_def, dict):
        return ""
    execution = tool_def.get("execution")
    if not isinstance(execution, dict):
        return ""
    return str(execution.get("type") or "").strip().lower()


def legacy_execution_requires_trust(exec_type: str) -> bool:
    return str(exec_type or "").strip().lower() in TRUSTED_LEGACY_EXECUTION_TYPES


def unsupported_execution_reason(tool_def: dict[str, Any] | None) -> str | None:
    if not isinstance(tool_def, dict):
        return "tool definition must be an object"
    exec_type = execution_type(tool_def) or "local"
    execution = mapping_or_empty(tool_def.get("execution"))
    if exec_type in SUPPORTED_AUTHORABLE_EXECUTION_TYPES:
        if exec_type == "rumi_function" and not str(execution.get("qualified_name") or "").strip():
            return "rumi_function tools must declare execution.qualified_name"
        if exec_type == "capability" and not str(execution.get("permission_id") or "").strip():
            return "capability tools must declare execution.permission_id"
        if exec_type == "mcp":
            if not str(execution.get("server_name") or "").strip():
                return "mcp tools must declare execution.server_name"
        if exec_type == "global_contract":
            if not str(execution.get("contract_id") or "").strip():
                return "global_contract tools must declare execution.contract_id"
            if not str(execution.get("operation") or "").strip():
                return "global_contract tools must declare execution.operation"
        return None
    if legacy_execution_requires_trust(exec_type) and is_trusted_tool(tool_def):
        return None
    return "execution type '{}' is only allowed for trusted first-party tools".format(exec_type)


def untrusted_tool_security_rejection(tool_def: dict[str, Any]) -> str | None:
    """Reject untrusted tools that try to borrow host coding/browser/computer power."""
    if not isinstance(tool_def, dict) or not is_explicitly_untrusted_tool(tool_def):
        return None
    if is_sandbox_capability_tool(tool_def):
        return None

    execution = mapping_or_empty(tool_def.get("execution"))
    exec_type = str(execution.get("type") or "").strip().lower()
    grants = capability_grants(tool_def)
    if any(_is_host_capability_grant(grant) for grant in grants):
        return "untrusted tools may not request host capabilities; use sandbox.* capabilities"

    if exec_type == "rumi_function":
        qualified_name = str(execution.get("qualified_name") or "").strip()
        pack_id, _, function_id = qualified_name.partition(":")
        if pack_id in TRUSTED_TOOL_PACK_IDS and _is_host_coding_function(function_id):
            return "untrusted tools may not borrow trusted host coding/browser/computer functions"
    if exec_type == "capability":
        permission_id = str(execution.get("permission_id") or "").strip()
        if _is_host_capability_grant(permission_id):
            return "untrusted tools may not invoke host capabilities"
    if _looks_like_host_coding_tool(tool_def):
        return "untrusted tools may not expose host coding/browser/computer actions"
    return None


def is_explicitly_untrusted_tool(tool_def: dict[str, Any]) -> bool:
    if not isinstance(tool_def, dict):
        return False
    metadata = tool_def.get("metadata")
    if isinstance(metadata, dict):
        if metadata.get("trusted") is False:
            return True
        source_pack_id = str(metadata.get("source_pack_id") or "").strip()
        if source_pack_id:
            return not is_trusted_pack_id(source_pack_id)
    source_pack_id = str(tool_def.get("source_pack_id") or "").strip()
    if source_pack_id:
        return not is_trusted_pack_id(source_pack_id)
    return tool_def.get("trusted") is False


def capability_grants(tool_def: dict[str, Any]) -> list[str]:
    values: list[str] = []
    for source in (
        tool_def,
        tool_def.get("metadata") if isinstance(tool_def.get("metadata"), dict) else {},
        tool_def.get("config") if isinstance(tool_def.get("config"), dict) else {},
    ):
        raw = source.get("capability_grants") if isinstance(source, dict) else None
        if isinstance(raw, list):
            values.extend(str(item).strip() for item in raw if str(item).strip())
        elif isinstance(raw, str) and raw.strip():
            values.append(raw.strip())
    return list(dict.fromkeys(values))


def is_sandbox_capability_tool(tool_def: dict[str, Any]) -> bool:
    if not isinstance(tool_def, dict):
        return False
    grants = capability_grants(tool_def)
    if not grants or any(not grant.startswith(SANDBOX_CAPABILITY_PREFIX) for grant in grants):
        return False
    execution = mapping_or_empty(tool_def.get("execution"))
    exec_type = str(execution.get("type") or "").strip().lower()
    if exec_type == "rumi_function":
        qualified_name = str(execution.get("qualified_name") or "").strip()
        pack_id, _, function_id = qualified_name.partition(":")
        return pack_id == "defaultspack" and function_id in SANDBOX_FUNCTION_IDS
    if exec_type == "capability":
        permission_id = str(execution.get("permission_id") or "").strip()
        return permission_id.startswith(SANDBOX_CAPABILITY_PREFIX)
    return False


def _is_host_capability_grant(value: str) -> bool:
    grant = str(value or "").strip()
    return bool(grant) and not grant.startswith(SANDBOX_CAPABILITY_PREFIX) and grant.startswith(HOST_CAPABILITY_PREFIXES)


def _is_host_coding_function(function_id: str) -> bool:
    name = str(function_id or "").strip()
    return name.startswith(HOST_CODING_FUNCTION_PREFIXES) and name not in SANDBOX_FUNCTION_IDS


def _looks_like_host_coding_tool(tool_def: dict[str, Any]) -> bool:
    if is_sandbox_capability_tool(tool_def):
        return False
    name = " ".join(
        str(value or "").strip().lower()
        for value in (
            tool_def.get("tool_id"),
            tool_def.get("name"),
            tool_def.get("summary"),
            tool_def.get("description"),
            _tool_value(tool_def, "category"),
            _tool_value(tool_def, "action_type"),
        )
    )
    return any(marker in name for marker in ("coding_", "terminal", "shell", "git", "browser", "computer"))


def normalize_risk(raw_risk: Any, tool_def: dict[str, Any], trusted: bool) -> tuple[str, bool]:
    risk = str(raw_risk or "").strip().lower()
    known = risk in VALID_RISKS
    if known:
        return risk, False
    if trusted and not appears_write_or_execute_capable(tool_def):
        return "low", True
    return "high", True


def requires_approval_for_security(tool_def: dict[str, Any] | None) -> bool:
    if not isinstance(tool_def, dict):
        return True
    if is_safe_first_party_memo_tool(tool_def):
        return False
    risk = str(_tool_value(tool_def, "risk") or "").strip().lower()
    return (
        bool(_tool_value(tool_def, "requires_approval"))
        or risk in {"high", "critical"}
        or appears_write_or_execute_capable(tool_def)
    )


def is_safe_first_party_memo_tool(tool_def: dict[str, Any]) -> bool:
    """Allow Rumi's built-in memo tools to write local memory without a dead-end approval."""
    if not is_trusted_tool(tool_def):
        return False
    if _tool_value(tool_def, "requires_approval") is not False:
        return False
    execution = tool_def.get("execution")
    if not isinstance(execution, dict) or str(execution.get("type") or "").strip().lower() != "handler":
        return False
    category = str(_tool_value(tool_def, "category") or "").strip().lower()
    name = " ".join(
        str(value or "").strip().lower()
        for value in (
            tool_def.get("tool_id"),
            tool_def.get("name"),
            tool_def.get("summary"),
        )
    )
    tags = list_or_empty(tool_def.get("tags"))
    tag_text = " ".join(str(tag or "").strip().lower() for tag in tags)
    return category == "memory" and ("memo" in name or "memo" in tag_text)


def appears_write_or_execute_capable(tool_def: dict[str, Any] | None) -> bool:
    if not isinstance(tool_def, dict):
        return True
    if bool(_tool_value(tool_def, "write_action")):
        return True
    action_type = str(_tool_value(tool_def, "action_type") or "").strip().lower()
    if action_type in _UNSAFE_ACTION_TYPES:
        return True
    category = str(_tool_value(tool_def, "category") or "").strip().lower()
    if category in _UNSAFE_CATEGORIES:
        return True
    tags = tool_def.get("tags")
    if isinstance(tags, list):
        for tag in tags:
            if str(tag or "").strip().lower() in _UNSAFE_ACTION_TYPES | _UNSAFE_CATEGORIES:
                return True
    return _unsafe_text_seen(
        tool_def.get("tool_id"),
        tool_def.get("name"),
        tool_def.get("summary"),
        tool_def.get("description"),
        _schema_text(tool_def.get("schema")),
        _schema_text(tool_def.get("execution")),
    )


def _tool_value(tool_def: dict[str, Any] | None, key: str) -> Any:
    if not isinstance(tool_def, dict):
        return None
    if key in tool_def:
        return tool_def.get(key)
    metadata = tool_def.get("metadata")
    if isinstance(metadata, dict) and key in metadata:
        return metadata.get(key)
    execution = tool_def.get("execution")
    if isinstance(execution, dict) and key in execution:
        return execution.get(key)
    return None


def _schema_text(value: Any) -> str:
    if isinstance(value, dict):
        parts: list[str] = []
        for key, item in value.items():
            parts.append(str(key))
            parts.append(_schema_text(item))
        return " ".join(parts)
    if isinstance(value, list):
        return " ".join(_schema_text(item) for item in value)
    if isinstance(value, str):
        return value
    return ""


def _unsafe_text_seen(*values: Any) -> bool:
    text = " ".join(str(value or "").replace("_", " ").replace("-", " ").lower() for value in values)
    return any(marker in text for marker in _UNSAFE_TEXT_MARKERS)
