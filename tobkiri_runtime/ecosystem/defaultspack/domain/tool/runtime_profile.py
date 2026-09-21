from __future__ import annotations

from collections import Counter
from typing import Any

from domain.tool.service_catalog import infer_service_id


RUNTIME_PROFILE_SCHEMA = "rumi.tool.runtime_profile.v1"

SANDBOX_EXEC_TOOL_IDS = frozenset({"sandbox_exec", "python_exec", "node_exec"})
BROWSER_SERVICES = frozenset({"browser"})
BROWSER_TAGS = frozenset({"browser", "webview"})
COMPUTER_SERVICES = frozenset({"computer"})
COMPUTER_TAGS = frozenset({"computer", "computer_use", "desktop", "screen", "mouse", "keyboard"})
CONNECTOR_SERVICES = frozenset({"calendar", "gmail", "github", "google_drive", "mcp", "notion", "slack"})
CONNECTOR_TAGS = frozenset({"connector", "integration", "oauth"})
HOST_WORKSPACE_SERVICES = frozenset({"artifacts", "coding", "files", "terminal"})
HOST_WORKSPACE_TAGS = frozenset({"agent_os", "artifact_workspace", "workspace", "git"})
NETWORK_SERVICES = frozenset({"web"})
NETWORK_TOOL_IDS = frozenset({"web_search", "tool_web_search", "reddit_search", "tool_reddit_search"})


def tool_runtime_profile(tool: dict[str, Any], *, record: dict[str, Any] | None = None) -> dict[str, Any]:
    """Return a compact, conservative execution-material profile for a tool.

    This profile is intentionally separate from Cloudflare compatibility. It
    describes the materials a tool is likely to need, such as Python, network,
    a browser session, a sandbox, or the PC workspace.
    """

    tool_id = _tool_id(tool, record)
    service_id = str((record or {}).get("service_id") or infer_service_id(tool)).strip().lower()
    tags = _tags(tool, record)
    execution = tool.get("execution") if isinstance(tool.get("execution"), dict) else {}
    metadata = tool.get("metadata") if isinstance(tool.get("metadata"), dict) else {}
    capability_grants = _string_set(tool.get("capability_grants")) | _string_set(metadata.get("capability_grants"))
    calling_convention = str(tool.get("calling_convention") or metadata.get("calling_convention") or "").strip().lower()
    entrypoint = str(tool.get("entrypoint") or metadata.get("entrypoint") or "").strip()

    if service_id in COMPUTER_SERVICES or tags & COMPUTER_TAGS:
        return _profile(
            kind="pc_computer",
            tags=("runtime:pc", "runtime:computer", "cap:screen", "cap:input"),
            layers=("pc-defaultspack-runtime", "computer-use"),
            requirements=("pc_session", "user_approval"),
            reason="computer_or_desktop_surface",
            host_bound=True,
        )

    if service_id in BROWSER_SERVICES or tags & BROWSER_TAGS:
        return _profile(
            kind="python_chrome",
            tags=("runtime:python", "runtime:chrome", "cap:browser_session"),
            layers=("python", "pc-browser-session"),
            requirements=("browser_session",),
            reason="browser_session_required",
            host_bound=True,
        )

    if tool_id in SANDBOX_EXEC_TOOL_IDS:
        runtime = "python" if tool_id == "python_exec" else "node" if tool_id == "node_exec" else "shell"
        return _profile(
            kind=f"sandbox_{runtime}",
            tags=("runtime:sandbox", f"runtime:{runtime}", "cap:exec"),
            layers=("managed-sandbox", runtime),
            requirements=("sandbox_provider", "user_approval"),
            reason="managed_sandbox_execution",
            host_bound=False,
        )

    if service_id in CONNECTOR_SERVICES or tags & CONNECTOR_TAGS:
        return _profile(
            kind="external_connector",
            tags=("runtime:python", "cap:network", "cap:oauth"),
            layers=("python", "defaultspack-connector"),
            requirements=("connector_credentials", "audit_context"),
            reason="external_connector_required",
            host_bound=False,
        )

    if _is_network_tool(tool_id, service_id, tags, capability_grants):
        runtime_tags = ["runtime:python", "cap:network"]
        layers = ["python"]
        if calling_convention == "subprocess" or entrypoint.endswith(".py:run"):
            runtime_tags.append("runtime:subprocess")
            layers.append("subprocess")
        elif str(execution.get("type") or "").strip() == "rumi_function":
            runtime_tags.append("runtime:rumi_function")
            layers.append("rumi_function")
        return _profile(
            kind="python_network",
            tags=tuple(runtime_tags),
            layers=tuple(layers),
            requirements=("network",),
            reason="network_read_tool",
            host_bound=False,
        )

    if service_id in HOST_WORKSPACE_SERVICES or tags & HOST_WORKSPACE_TAGS:
        return _profile(
            kind="pc_workspace",
            tags=("runtime:pc", "runtime:python", "cap:workspace"),
            layers=("pc-defaultspack-runtime", "workspace"),
            requirements=("pc_workspace",),
            reason="host_workspace_required",
            host_bound=True,
        )

    if calling_convention == "subprocess" or entrypoint.endswith(".py:run"):
        return _profile(
            kind="python_subprocess",
            tags=("runtime:python", "runtime:subprocess"),
            layers=("python", "subprocess"),
            requirements=(),
            reason="python_subprocess_entrypoint",
            host_bound=False,
        )

    return _profile(
        kind="python_defaultspack",
        tags=("runtime:python", "runtime:defaultspack"),
        layers=("python", "defaultspack-runtime"),
        requirements=(),
        reason="defaultspack_python_runtime",
        host_bound=False,
    )


def tool_runtime_profile_summary(records: list[dict[str, Any]]) -> dict[str, Any]:
    by_kind = Counter(str(record.get("kind") or "unknown") for record in records)
    return {
        "schema": RUNTIME_PROFILE_SCHEMA,
        "count": len(records),
        "by_kind": dict(sorted(by_kind.items())),
        "host_bound_count": sum(1 for record in records if record.get("host_bound") is True),
        "network_count": sum(1 for record in records if "cap:network" in set(record.get("tags") or [])),
        "browser_session_count": sum(1 for record in records if "cap:browser_session" in set(record.get("tags") or [])),
        "sandbox_count": sum(1 for record in records if "runtime:sandbox" in set(record.get("tags") or [])),
    }


def _profile(
    *,
    kind: str,
    tags: tuple[str, ...],
    layers: tuple[str, ...],
    requirements: tuple[str, ...],
    reason: str,
    host_bound: bool,
) -> dict[str, Any]:
    return {
        "schema": RUNTIME_PROFILE_SCHEMA,
        "kind": kind,
        "tags": list(dict.fromkeys(tags)),
        "layers": list(dict.fromkeys(layers)),
        "requirements": list(dict.fromkeys(requirements)),
        "reason": reason,
        "host_bound": host_bound,
    }


def _is_network_tool(
    tool_id: str,
    service_id: str,
    tags: set[str],
    capability_grants: set[str],
) -> bool:
    return (
        tool_id in NETWORK_TOOL_IDS
        or service_id in NETWORK_SERVICES
        or "network" in tags
        or "research" in tags
        or "network.read" in capability_grants
    )


def _tool_id(tool: dict[str, Any], record: dict[str, Any] | None) -> str:
    if isinstance(record, dict):
        value = str(record.get("tool_id") or "").strip().lower()
        if value:
            return value
    return str(tool.get("tool_id") or tool.get("function_id") or tool.get("name") or "").strip().lower()


def _tags(tool: dict[str, Any], record: dict[str, Any] | None) -> set[str]:
    values: list[Any] = []
    if isinstance(record, dict):
        values.extend(record.get("tags") or [])
    values.extend(tool.get("tags") or [])
    metadata = tool.get("metadata") if isinstance(tool.get("metadata"), dict) else {}
    values.extend(metadata.get("tags") or [])
    category = str(tool.get("category") or metadata.get("category") or "").strip()
    if category:
        values.append(category)
    return {str(value).strip().lower() for value in values if str(value or "").strip()}


def _string_set(value: Any) -> set[str]:
    if not isinstance(value, list):
        return set()
    return {str(item).strip().lower() for item in value if str(item or "").strip()}
