from __future__ import annotations

from collections import Counter
from typing import Any

from domain.tool.service_catalog import infer_service_id
from domain.tool.schema_adapter import mapping_or_empty


CLOUDFLARE_BRIDGE_TOOL_CAPABILITIES: dict[str, tuple[str, ...]] = {
    "sandbox_exec": ("sandbox.exec", "sandbox.container", "sandbox.resource_limits"),
    "python_exec": ("sandbox.exec", "sandbox.container", "sandbox.resource_limits"),
    "node_exec": ("sandbox.exec", "sandbox.container", "sandbox.resource_limits"),
    "sandbox_files_read": ("sandbox.files", "sandbox.container"),
    "sandbox_files_apply_patch": ("sandbox.files", "sandbox.container"),
}

CLOUDFLARE_BRIDGE_TOOL_IDS = frozenset(CLOUDFLARE_BRIDGE_TOOL_CAPABILITIES)

CLOUDFLARE_SANDBOX_CANDIDATE_TOOL_IDS = frozenset(
    {
        "package_install_plan",
        "sandbox_port_expose",
    }
)

LOCAL_SANDBOX_HANDLER_TOOL_IDS = frozenset(
    {
        "sandbox_artifact_export",
        "sandbox_diff_preview",
        "sandbox_file_patch",
        "sandbox_file_read",
        "sandbox_file_write",
        "sandbox_terminal_exec",
    }
)

PC_LOCAL_SERVICES = frozenset({"browser", "computer"})
PC_LOCAL_TAGS = frozenset(
    {
        "browser",
        "computer",
        "computer_use",
        "desktop",
        "keyboard",
        "linux",
        "mac-swift",
        "mouse",
        "screen",
        "vision",
        "windows",
        "x11-virtual",
    }
)
CONNECTOR_SERVICES = frozenset({"calendar", "gmail", "github", "google_drive", "mcp", "notion", "slack"})
CONNECTOR_TAGS = frozenset({"connector", "integration", "oauth"})
HOST_WORKSPACE_SERVICES = frozenset({"artifacts", "coding", "files", "terminal"})
HOST_WORKSPACE_TAGS = frozenset({"agent_os", "artifact_workspace", "workspace", "git"})


def cloudflare_tool_record(tool: dict[str, Any], *, record: dict[str, Any] | None = None) -> dict[str, Any]:
    tool_id = _tool_id(tool, record)
    service_id = str((record or {}).get("service_id") or infer_service_id(tool)).strip().lower()
    tags = _tags(tool, record)

    if tool_id in CLOUDFLARE_BRIDGE_TOOL_CAPABILITIES:
        return _coverage(
            compatible=True,
            route="cloudflare_sandbox_bridge",
            reason="sandbox_bridge_supported",
            runtime="cloudflare_sandbox_bridge",
            capabilities=CLOUDFLARE_BRIDGE_TOOL_CAPABILITIES[tool_id],
            limitations=_bridge_tool_limitations(tool_id),
        )

    if tool_id == "sandbox_port_expose":
        return _coverage(
            compatible=False,
            route="cloudflare_preview_not_enabled",
            reason="preview_url_domain_required",
            runtime="cloudflare_sandbox_bridge",
            capabilities=("sandbox.port_expose",),
            limitations=("sandbox_preview_urls_require_custom_domain_for_production",),
        )

    if tool_id in LOCAL_SANDBOX_HANDLER_TOOL_IDS:
        return _coverage(
            compatible=False,
            route="pc_bridge_required",
            reason="local_sandbox_workspace_required",
            runtime="pc_bridge",
            capabilities=(),
            limitations=("legacy_sandbox_handler_uses_pc_workspace_manager",),
        )

    if service_id in PC_LOCAL_SERVICES or tags & PC_LOCAL_TAGS:
        return _coverage(
            compatible=False,
            route="pc_bridge_required",
            reason="pc_local_surface",
            runtime="pc_bridge",
            capabilities=(),
            limitations=("screen_browser_desktop_stay_on_pc",),
        )

    if service_id in CONNECTOR_SERVICES or tags & CONNECTOR_TAGS:
        return _coverage(
            compatible=False,
            route="external_connector_or_pc_bridge",
            reason="external_connector_required",
            runtime="pc_bridge",
            capabilities=(),
            limitations=("connector_oauth_and_audit_stay_in_defaultspack",),
        )

    if tool_id in CLOUDFLARE_SANDBOX_CANDIDATE_TOOL_IDS or "sandbox" in tags:
        return _coverage(
            compatible=False,
            route="cloudflare_sandbox_candidate",
            reason="sandbox_bridge_adapter_missing_surface",
            runtime="cloudflare_sandbox_bridge",
            capabilities=_candidate_capabilities(tool_id, tags),
            limitations=("adapter_surface_not_implemented",),
        )

    if service_id in HOST_WORKSPACE_SERVICES or tags & HOST_WORKSPACE_TAGS:
        return _coverage(
            compatible=False,
            route="pc_bridge_required",
            reason="host_workspace_required",
            runtime="pc_bridge",
            capabilities=(),
            limitations=("host_workspace_and_git_state_stay_on_pc",),
        )

    return _coverage(
        compatible=False,
        route="defaultspack_runtime_required",
        reason="defaultspack_runtime_required",
        runtime="defaultspack_runtime",
        capabilities=(),
        limitations=("no_cloudflare_adapter_registered",),
    )


def cloudflare_tool_records(tools: list[dict[str, Any]], records: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    if records is None:
        compact_records: list[dict[str, Any] | None] = [None] * len(tools)
    else:
        compact_records = list(records)
    return [
        cloudflare_tool_record(tool, record=record if isinstance(record, dict) else None)
        for tool, record in zip(tools, compact_records)
    ]


def cloudflare_tool_summary(records: list[dict[str, Any]]) -> dict[str, Any]:
    by_route = Counter(str(record.get("route") or "unknown") for record in records)
    by_reason = Counter(str(record.get("reason") or "unknown") for record in records)
    supported_count = sum(1 for record in records if record.get("compatible") is True)
    candidate_count = sum(1 for record in records if str(record.get("route") or "") == "cloudflare_sandbox_candidate")
    return {
        "schema": "rumi.cloudflare.tool_coverage.v1",
        "count": len(records),
        "supported_count": supported_count,
        "unsupported_count": max(0, len(records) - supported_count),
        "candidate_count": candidate_count,
        "by_route": dict(sorted(by_route.items())),
        "by_reason": dict(sorted(by_reason.items())),
        "supported_runtime": "cloudflare_sandbox_bridge",
        "all_tools_cloudflare_native": False,
        "pc_bridge_required": any(str(record.get("route") or "") == "pc_bridge_required" for record in records),
    }


def _coverage(
    *,
    compatible: bool,
    route: str,
    reason: str,
    runtime: str,
    capabilities: tuple[str, ...],
    limitations: tuple[str, ...],
) -> dict[str, Any]:
    return {
        "compatible": compatible,
        "route": route,
        "reason": reason,
        "runtime": runtime,
        "required_capabilities": list(capabilities),
        "limitations": list(limitations),
    }


def _candidate_capabilities(tool_id: str, tags: set[str]) -> tuple[str, ...]:
    if "file" in tags:
        return ("sandbox.files",)
    if tool_id == "package_install_plan":
        return ("sandbox.exec", "sandbox.files")
    return ("sandbox.container",)


def _bridge_tool_limitations(tool_id: str) -> tuple[str, ...]:
    if tool_id in {"sandbox_exec", "python_exec", "node_exec"}:
        return ("per_call_env_unsupported", "stdin_unsupported")
    return ()


def _tool_id(tool: dict[str, Any], record: dict[str, Any] | None) -> str:
    if isinstance(record, dict):
        value = str(record.get("tool_id") or "").strip()
        if value:
            return value
    return str(tool.get("tool_id") or tool.get("name") or "").strip()


def _tags(tool: dict[str, Any], record: dict[str, Any] | None) -> set[str]:
    values: list[Any] = []
    if isinstance(record, dict):
        values.extend(record.get("tags") or [])
    values.extend(tool.get("tags") or [])
    metadata = mapping_or_empty(tool.get("metadata"))
    values.extend(metadata.get("tags") or [])
    category = str(tool.get("category") or metadata.get("category") or "").strip()
    if category:
        values.append(category)
    return {str(value).strip().lower() for value in values if str(value or "").strip()}
