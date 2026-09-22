from __future__ import annotations

from functools import lru_cache
import re
from dataclasses import dataclass, field
import importlib
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from ecosystem.defaultspack.domain.components import get_domain_component_registry
from ecosystem.defaultspack.domain.extensions.runtime import get_extension_registry
from ecosystem.defaultspack.domain.tool.security import is_trusted_pack_id


@dataclass(frozen=True)
class HttpRouteSpec:
    method: str
    pattern: str
    function_id: str = ""
    legacy_block_module: str = ""
    flow_id: str = ""
    handler_name: str = ""
    path_inject: Dict[str, str] = field(default_factory=dict)
    defaults: Dict[str, Any] = field(default_factory=dict)
    pre_auth: bool = False
    sensitive: bool = False
    local_only: bool = False
    block_module: str = ""
    function_name: str = ""
    fallback_block_module: str = ""
    permission_id: str = ""
    owner_pack_id: str = ""
    provider_id: str = ""
    frontend_id: str = ""
    audience: str = "kernel_api"
    resource_template: Dict[str, Any] = field(default_factory=dict)
    core_only: bool = False

    def __post_init__(self) -> None:
        resolved_function_id = str(self.function_id or self.function_name or "").strip()
        resolved_legacy_block = str(
            self.legacy_block_module or self.fallback_block_module or ""
        ).strip()
        if self.block_module and not resolved_function_id:
            try:
                from domain.function_runtime.registry import function_id_for_block_module

                resolved_function_id = str(
                    function_id_for_block_module(self.block_module) or ""
                ).strip()
            except Exception:
                resolved_function_id = ""
        if self.block_module and not resolved_legacy_block and not resolved_function_id:
            resolved_legacy_block = str(self.block_module).strip()
        if resolved_function_id and not self.function_name:
            object.__setattr__(self, "function_name", resolved_function_id)
        if resolved_legacy_block and not self.fallback_block_module:
            object.__setattr__(self, "fallback_block_module", resolved_legacy_block)
        object.__setattr__(self, "function_id", resolved_function_id)
        object.__setattr__(self, "legacy_block_module", resolved_legacy_block)
        if str(self.pattern or "").startswith("/api/prompts"):
            object.__setattr__(self, "sensitive", True)
            object.__setattr__(self, "local_only", True)


_ROUTE_PARAM_RE = re.compile(r"\{(\w+)\}")


_ALLOWED_FIRST_PARTY_COMPONENT_ROUTE_BLOCK_MODULES = {
    "blocks.integrations.discord",
    "blocks.integrations.line",
    "blocks.integrations.slack",
    "blocks.ui.catalog",
    "blocks.ui.provider_health",
}


def _pack_approved_for_component_routes(pack_id: str) -> bool:
    pack_id = str(pack_id or "").strip()
    if not pack_id:
        return False
    if is_trusted_pack_id(pack_id):
        return True
    try:
        from core_runtime.approval_manager import get_approval_manager

        approved, _reason = get_approval_manager().is_pack_approved_and_verified(pack_id)
        return bool(approved)
    except Exception:
        return False


def _safe_component_route_block_module(module_name: str, source_pack_id: str) -> bool:
    module_name = str(module_name or "").strip()
    if not module_name:
        return True
    return (
        is_trusted_pack_id(source_pack_id)
        and module_name in _ALLOWED_FIRST_PARTY_COMPONENT_ROUTE_BLOCK_MODULES
    )


def _component_route_target_allowed(
    *,
    source_pack_id: str,
    block_module: str = "",
    fallback_block_module: str = "",
    handler_name: str = "",
) -> bool:
    if not _pack_approved_for_component_routes(source_pack_id):
        return False
    if block_module and not _safe_component_route_block_module(block_module, source_pack_id):
        return False
    if fallback_block_module and not _safe_component_route_block_module(
        fallback_block_module,
        source_pack_id,
    ):
        return False
    if handler_name and not is_trusted_pack_id(source_pack_id):
        return False
    return True


def http_route_sort_key(method: str, pattern: str, index: int = 0):
    """Sort exact/static routes before parameterized catch-all siblings."""
    segments = [segment for segment in str(pattern or "").split("/") if segment]
    param_count = 0
    catch_all_count = 0
    static_segment_count = 0
    for segment in segments:
        match = _ROUTE_PARAM_RE.fullmatch(segment)
        if match is None:
            static_segment_count += 1
            continue
        param_count += 1
        if match.group(1) == "path":
            catch_all_count += 1
    literal_chars = len(_ROUTE_PARAM_RE.sub("", str(pattern or "")))
    return (
        str(method or "").upper(),
        catch_all_count,
        param_count,
        -static_segment_count,
        -len(segments),
        -literal_chars,
        index,
    )


def compile_http_route_pattern(pattern: str):
    regex_pattern = _ROUTE_PARAM_RE.sub(
        lambda match: (
            r"(?P<{}>.+)".format(match.group(1))
            if match.group(1) == "path"
            else r"(?P<{}>[^/]+)".format(match.group(1))
        ),
        pattern,
    )
    return re.compile("^" + regex_pattern + "$")


def _component_route_specs() -> List[HttpRouteSpec]:
    specs: List[HttpRouteSpec] = []
    try:
        components = get_domain_component_registry().list()
    except Exception:
        return specs
    for component in components:
        manifest = component.as_dict()
        source_pack_id = str(getattr(component, "source_pack_id", "") or "").strip()
        routes = manifest.get("routes")
        if not isinstance(routes, list):
            continue
        for route in routes:
            if not isinstance(route, dict):
                continue
            method = str(route.get("method") or "").strip().upper()
            pattern = str(route.get("path") or route.get("pattern") or "").strip()
            block_module = str(route.get("block_module") or "").strip()
            function_id = str(
                route.get("function_id")
                or route.get("function_name")
                or route.get("qualified_name")
                or route.get("function")
                or ""
            ).strip()
            function_name = str(
                route.get("function_name")
                or route.get("qualified_name")
                or route.get("function")
                or ""
            ).strip()
            flow_id = str(route.get("flow_id") or "").strip()
            fallback_block_module = str(
                route.get("fallback_block_module") or route.get("fallback_block") or ""
            ).strip()
            legacy_block_module = str(route.get("legacy_block_module") or "").strip()
            handler_name = str(route.get("handler_name") or "").strip()
            if (
                not method
                or not pattern
                or not (block_module or function_id or function_name or flow_id or handler_name)
            ):
                continue
            if not _component_route_target_allowed(
                source_pack_id=source_pack_id,
                block_module=block_module,
                fallback_block_module=fallback_block_module,
                handler_name=handler_name,
            ):
                continue
            path_inject = route.get("path_inject")
            defaults = route.get("defaults")
            specs.append(
                HttpRouteSpec(
                    method,
                    pattern,
                    function_id=function_id,
                    legacy_block_module=legacy_block_module,
                    block_module=block_module,
                    function_name=function_name,
                    flow_id=flow_id,
                    fallback_block_module=fallback_block_module,
                    handler_name=handler_name,
                    path_inject=dict(path_inject) if isinstance(path_inject, dict) else {},
                    defaults=dict(defaults) if isinstance(defaults, dict) else {},
                    permission_id=str(route.get("permission_id") or "").strip(),
                    owner_pack_id=str(route.get("owner_pack_id") or source_pack_id).strip(),
                    provider_id=str(route.get("provider_id") or "").strip(),
                    frontend_id=str(route.get("frontend_id") or "").strip(),
                    audience=str(route.get("audience") or "kernel_api").strip(),
                    resource_template=dict(route.get("resource_template") or {})
                    if isinstance(route.get("resource_template"), dict)
                    else {},
                    core_only=bool(route.get("core_only", False)),
                )
            )
    return specs


def component_http_route_specs() -> List[HttpRouteSpec]:
    return list(_component_route_specs())


def component_route_diagnostics() -> list[dict[str, str]]:
    diagnostics: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for spec in _component_route_specs():
        key = (spec.method, spec.pattern)
        if key in seen:
            diagnostics.append(
                {
                    "level": "warning",
                    "code": "component_route_duplicate",
                    "message": f"duplicate component route {spec.method} {spec.pattern}",
                    "source": "domain component manifests",
                }
            )
        seen.add(key)
    return diagnostics


# The prompt workspace template was retired, but its public routes remain a
# finite compatibility surface. Project the old function identities through
# the current contract adapter without restoring the retired local writers.
_PROMPT_COMPATIBILITY_TEMPLATE_ROUTES: tuple[
    tuple[str, str, str, str, dict[str, str]], ...
] = (
    ("GET", "/api/prompts", "prompt_editor_load", "load", {}),
    ("GET", "/api/prompts/active", "prompt_active", "active", {}),
    ("GET", "/api/prompts/traces", "prompt_trace_list", "traces", {}),
    (
        "GET",
        "/api/prompts/traces/{trace_id}",
        "prompt_trace_get",
        "traces",
        {"trace_id": "trace_id"},
    ),
    ("POST", "/api/prompts/toggle", "prompt_toggle", "toggle", {}),
    (
        "POST",
        "/api/prompts/preview-toggle",
        "prompt_preview_toggle",
        "preview_toggle",
        {},
    ),
    ("GET", "/api/prompts/editor", "prompt_editor_load", "load", {}),
    ("POST", "/api/prompts/editor/save", "prompt_editor_save", "save", {}),
    (
        "POST",
        "/api/prompts/override",
        "prompt_create_override",
        "override",
        {},
    ),
    ("POST", "/api/prompts/diff", "prompt_diff", "diff", {}),
    ("POST", "/api/prompts/test", "prompt_test", "test", {}),
    (
        "GET",
        "/api/prompts/{name}/versions",
        "prompt_versions",
        "versions",
        {"name": "name"},
    ),
    (
        "POST",
        "/api/prompts/{name}/rollback",
        "prompt_rollback",
        "rollback",
        {"name": "name"},
    ),
    ("POST", "/api/prompts/lint", "prompt_lint_prompt", "lint", {}),
)

_PROMPT_COMPATIBILITY_BLOCK_MODULES = {
    "prompt_active": "blocks.prompt.active",
    "prompt_trace_list": "blocks.prompt.trace",
    "prompt_trace_get": "blocks.prompt.trace",
    "prompt_toggle": "blocks.prompt.toggle",
    "prompt_preview_toggle": "blocks.prompt.preview_toggle",
}


def _prompt_compatibility_template_route_specs() -> list[HttpRouteSpec]:
    if not prompt_contract_routes_enabled():
        return []
    specs: list[HttpRouteSpec] = []
    for method, pattern, function_id, action, path_inject in (
        _PROMPT_COMPATIBILITY_TEMPLATE_ROUTES
    ):
        spec = HttpRouteSpec(
            method,
            pattern,
            block_module=_PROMPT_COMPATIBILITY_BLOCK_MODULES.get(
                function_id,
                "blocks.prompt.contract_adapter",
            ),
            defaults={"action": action},
            path_inject=dict(path_inject),
            sensitive=True,
            local_only=True,
        )
        # Keep the historical name for clients and diagnostics, but do not
        # present it as a live function-registry entry. The adapter remains
        # the sole executable target and owns the approval boundary.
        object.__setattr__(spec, "function_name", f"defaultspack:{function_id}")
        specs.append(spec)
    return specs


def template_http_route_specs(defaultspack_root: str | Path | None = None) -> List[HttpRouteSpec]:
    try:
        from domain.function_runtime.template_specs import template_route_items
    except Exception:
        try:
            from ecosystem.defaultspack.domain.function_runtime.template_specs import (
                template_route_items,
            )
        except Exception:
            return []
    specs: List[HttpRouteSpec] = []
    for item in template_route_items(defaultspack_root):
        function_id = str(item.get("function_id") or "").strip()
        method = str(item.get("method") or "").strip().upper()
        pattern = str(item.get("path") or "").strip()
        if not function_id or not method or not pattern.startswith("/"):
            continue
        sensitive = bool(item.get("sensitive")) or pattern.startswith("/api/prompts")
        local_only = bool(item.get("local_only")) or pattern.startswith("/api/prompts")
        specs.append(
            HttpRouteSpec(
                method,
                pattern,
                function_id=function_id,
                function_name=f"defaultspack:{function_id}",
                block_module=str(item.get("block_module") or "").strip(),
                path_inject=dict(item.get("path_inject") or {}),
                defaults=dict(item.get("default_args") or {}),
                pre_auth=bool(item.get("pre_auth")),
                sensitive=sensitive,
                local_only=local_only,
                permission_id=str(item.get("permission_id") or "").strip(),
                owner_pack_id=str(item.get("owner_pack_id") or "defaultspack").strip(),
                provider_id=str(item.get("provider_id") or "").strip(),
                frontend_id=str(item.get("frontend_id") or "").strip(),
                audience=str(item.get("audience") or "kernel_api").strip(),
                resource_template=dict(item.get("resource_template") or {})
                if isinstance(item.get("resource_template"), dict)
                else {},
                core_only=bool(item.get("core_only", False)),
            )
        )
    existing = {(spec.method, spec.pattern) for spec in specs}
    specs.extend(
        spec
        for spec in _prompt_compatibility_template_route_specs()
        if (spec.method, spec.pattern) not in existing
    )
    return specs


def template_route_diagnostics(defaultspack_root: str | Path | None = None) -> list[dict[str, str]]:
    diagnostics: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    flow_routes = {(spec.method, spec.pattern) for spec in flow_http_route_specs()}
    for spec in template_http_route_specs(defaultspack_root):
        key = (spec.method, spec.pattern)
        if key in seen:
            diagnostics.append(
                {
                    "level": "warning",
                    "code": "template_route_duplicate",
                    "message": f"duplicate template route {spec.method} {spec.pattern}",
                    "source": "RumiTemplate function route_path",
                }
            )
        elif key in flow_routes:
            diagnostics.append(
                {
                    "level": "info",
                    "code": "template_route_shadowed_by_builtin",
                    "message": f"template route {spec.method} {spec.pattern} is already provided by a flow route",
                    "source": "RumiTemplate function route_path",
                }
            )
        seen.add(key)
    return diagnostics


def _defaultspack_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _read_yaml(path: Path) -> dict[str, Any]:
    try:
        text = path.read_text(encoding="utf-8")
    except Exception:
        return {}
    try:
        yaml_module = importlib.import_module("yaml")
        safe_load = getattr(yaml_module, "safe_load", None)
        if not callable(safe_load):
            raise RuntimeError("yaml.safe_load is unavailable")
        data = safe_load(text)
    except Exception:
        if path.name == "legacy_http_routes.yaml":
            return _read_legacy_routes_yaml_without_pyyaml(text)
        return {}
    return data if isinstance(data, dict) else {}


def _read_legacy_routes_yaml_without_pyyaml(text: str) -> dict[str, Any]:
    """Parse the simple legacy route allowlist when PyYAML is unavailable."""
    routes: list[dict[str, str]] = []
    metadata_defaults: dict[str, str] = {}
    current: dict[str, str] | None = None
    in_legacy_routes = False
    in_metadata_defaults = False
    for raw_line in text.splitlines():
        line = raw_line.split("#", 1)[0].rstrip()
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("- "):
            if not in_legacy_routes:
                continue
            if current:
                routes.append(current)
            current = {}
            stripped = stripped[2:].strip()
            if not stripped:
                continue
        elif not line.startswith(" "):
            in_legacy_routes = stripped == "legacy_routes:"
            in_metadata_defaults = stripped == "metadata_defaults:"
            continue
        if in_metadata_defaults and not in_legacy_routes:
            if ":" in stripped:
                key, value = stripped.split(":", 1)
                metadata_defaults[key.strip()] = value.strip().strip("\"'")
            continue
        if not in_legacy_routes:
            continue
        if current is None or ":" not in stripped:
            continue
        key, value = stripped.split(":", 1)
        current[key.strip()] = value.strip().strip("\"'")
    if current:
        routes.append(current)
    result: dict[str, Any] = {}
    if metadata_defaults:
        result["metadata_defaults"] = metadata_defaults
    if routes:
        result["legacy_routes"] = routes
    return result


def _read_flow_yaml(path: Path) -> dict[str, Any]:
    return _read_yaml(path)


def _legacy_http_routes_path() -> Path:
    return _defaultspack_root() / "docs" / "legacy_http_routes.yaml"


@lru_cache(maxsize=1)
def load_legacy_http_route_allowlist() -> dict[tuple[str, str, str], dict[str, Any]]:
    data = _read_yaml(_legacy_http_routes_path())
    metadata_defaults = data.get("metadata_defaults")
    if not isinstance(metadata_defaults, dict):
        metadata_defaults = {}
    allowlist: dict[tuple[str, str, str], dict[str, Any]] = {}
    for route in data.get("legacy_routes") or []:
        if not isinstance(route, dict):
            continue
        method = str(route.get("method") or "").strip().upper()
        pattern = str(route.get("pattern") or "").strip()
        legacy_block_module = str(route.get("legacy_block_module") or "").strip()
        if not method or not pattern or not legacy_block_module:
            continue
        metadata = dict(metadata_defaults)
        metadata.update(route)
        metadata["function_id"] = str(
            route.get("function_id")
            or route.get("replacement_function_id")
            or metadata_defaults.get("function_id")
            or ""
        ).strip()
        metadata["legacy_until"] = str(
            route.get("legacy_until")
            or route.get("remove_after")
            or metadata_defaults.get("legacy_until")
            or ""
        ).strip()
        allowlist[(method, pattern, legacy_block_module)] = metadata
    return allowlist


_REQUIRED_LEGACY_ROUTE_METADATA = frozenset(
    {
        "owner",
        "reason",
        "auth_mode",
        "principal",
        "csrf_origin",
        "rate_limit",
        "audit_category",
        "function_id",
        "legacy_until",
    }
)


def legacy_http_route_metadata(spec: HttpRouteSpec) -> dict[str, Any]:
    """Return complete policy metadata for an allowlisted compatibility route."""
    legacy_block_module = str(spec.legacy_block_module or "").strip()
    key = (
        str(spec.method or "").upper(),
        str(spec.pattern or "").strip(),
        legacy_block_module,
    )
    metadata = load_legacy_http_route_allowlist().get(key)
    if metadata is None:
        raise ValueError(
            f"legacy HTTP route is not allowlisted: {key[0]} {key[1]} -> {legacy_block_module}"
        )
    missing = sorted(
        field
        for field in _REQUIRED_LEGACY_ROUTE_METADATA
        if not str(metadata.get(field) or "").strip()
    )
    if missing:
        raise ValueError(
            "legacy HTTP route allowlist metadata is incomplete for "
            f"{key[0]} {key[1]} -> {legacy_block_module}: {', '.join(missing)}"
        )
    return dict(metadata)


def require_legacy_route_allowlisted(spec: HttpRouteSpec) -> None:
    legacy_block_module = str(spec.legacy_block_module or "").strip()
    if not legacy_block_module:
        return
    key = (
        str(spec.method or "").upper(),
        str(spec.pattern or "").strip(),
        legacy_block_module,
    )
    if key in load_legacy_http_route_allowlist():
        legacy_http_route_metadata(spec)
        return
    if _legacy_route_matches_mobile_contract(spec, legacy_block_module):
        return
    if _legacy_route_matches_mobile_contract(spec, legacy_block_module):
        return
    raise ValueError(
        f"legacy HTTP route is not allowlisted: {key[0]} {key[1]} -> {legacy_block_module}"
    )


def _legacy_route_matches_mobile_contract(
    spec: HttpRouteSpec,
    legacy_block_module: str,
) -> bool:
    """Treat the mobile route contract as the allowlist for mobile facade routes."""
    try:
        from ecosystem.defaultspack.domain.mobile.contract import iter_mobile_route_contracts
    except Exception:
        return False

    method = str(spec.method or "").strip().upper()
    pattern = str(spec.pattern or "").strip()
    legacy_block_module = str(legacy_block_module or "").strip()
    if not method or not pattern or not legacy_block_module:
        return False
    for route in iter_mobile_route_contracts():
        if method != str(route.method or "").strip().upper():
            continue
        if pattern != str(route.pattern or "").strip():
            continue
        allowed_modules = {
            str(route.block_module or "").strip(),
            str(route.fallback_block_module or "").strip(),
        }
        return legacy_block_module in allowed_modules
    return False


def flow_http_route_specs() -> List[HttpRouteSpec]:
    """Load endpoint -> flow declarations embedded in top-level flow YAML."""
    flows_dir = _defaultspack_root() / "flows"
    specs: List[HttpRouteSpec] = []
    if not flows_dir.is_dir():
        return specs
    for yaml_path in sorted(flows_dir.glob("*.flow.yaml")):
        flow_def = _read_flow_yaml(yaml_path)
        flow_id = str(flow_def.get("flow_id") or yaml_path.name[: -len(".flow.yaml")]).strip()
        if not flow_id:
            continue
        transport = flow_def.get("transport")
        http = transport.get("http") if isinstance(transport, dict) else None
        routes = http.get("routes") if isinstance(http, dict) else None
        if not isinstance(routes, list):
            continue
        for route in routes:
            if not isinstance(route, dict):
                continue
            method = str(route.get("method") or "").strip().upper()
            pattern = str(route.get("path") or route.get("pattern") or "").strip()
            if not method or not pattern:
                continue
            path_inject = route.get("path_inject")
            defaults = route.get("defaults")
            specs.append(
                HttpRouteSpec(
                    method,
                    pattern,
                    flow_id=flow_id,
                    fallback_block_module=str(
                        route.get("fallback_block_module") or route.get("fallback_block") or ""
                    ).strip(),
                    path_inject=dict(path_inject) if isinstance(path_inject, dict) else {},
                    defaults=dict(defaults) if isinstance(defaults, dict) else {},
                )
            )
    return specs


def _dedupe_http_route_specs(groups: list[list[HttpRouteSpec]]) -> list[HttpRouteSpec]:
    result: list[HttpRouteSpec] = []
    seen: set[tuple[str, str]] = set()
    for group in groups:
        for spec in group:
            key = (spec.method, spec.pattern)
            if key in seen:
                continue
            seen.add(key)
            result.append(spec)
    return result


def _change_request_commit_route_enabled() -> bool:
    """Never derive write authority from an ambient environment flag."""
    return False


def _route_enabled_by_default(spec: HttpRouteSpec) -> bool:
    if spec.pattern.startswith("/api/prompts"):
        return prompt_contract_routes_enabled()
    if spec.method == "POST" and spec.pattern == "/api/change-requests/{id}/commit":
        return _change_request_commit_route_enabled()
    return True


def prompt_contract_routes_enabled() -> bool:
    """Expose legacy prompt shims only when a prompt owner is resolved."""
    try:
        from core_runtime.resolved_profile_scope import active_resolved_profile

        plan = active_resolved_profile()
    except Exception:
        plan = None
    if plan is None:
        return False
    return any(
        provider.contract_id.startswith(("rumi.resource.prompt.", "rumi.action.prompt."))
        for provider in plan.providers
    )


def canonical_http_route_specs(*, include_always_available: bool = True) -> list[HttpRouteSpec]:
    """Return Host-owned routes that remain outside Pack v4 dispatch.

    Pack operations are dispatched by exact Contract/Operation/Function principal
    through the Host Broker. They are intentionally absent here: an old HTTP,
    flow, template, component, or block call therefore fails closed instead of
    selecting a compatibility implementation.
    """
    if not include_always_available:
        return []
    return _dedupe_http_route_specs([list(_ALWAYS_AVAILABLE_HTTP_ROUTE_SPECS)])


def flow_http_output_is_compatible(
    flow_id: str, output: Any, *, fallback_block_module: str = ""
) -> bool:
    def _has_streamable_events(data: Any) -> bool:
        if not isinstance(data, dict) or not data.get("_sse"):
            return False
        events = data.get("events", [])
        return not isinstance(events, (str, bytes))

    if flow_id == "defaultspack.chat_turn" and fallback_block_module == "blocks.chat.send":
        if not isinstance(output, dict) or output.get("status") != "ok":
            return False
        data = output.get("data")
        if not isinstance(data, dict):
            return False
        return bool(data.get("role") == "assistant" and ("content" in data or "raw_text" in data))
    if flow_id == "defaultspack.chat_stream_turn" and fallback_block_module == "blocks.chat.stream":
        if _has_streamable_events(output):
            return True
        if (
            isinstance(output, dict)
            and output.get("status") == "ok"
            and _has_streamable_events(output.get("data"))
        ):
            return True
        return False
    return True


_PROMPT_HTTP_ROUTE_SPECS = [
    HttpRouteSpec("GET", "/api/prompts", block_module="blocks.prompt.contract_adapter", defaults={"_contract_operation": "editor.load"}, sensitive=True),
    HttpRouteSpec("GET", "/api/prompts/active", block_module="blocks.prompt.active", sensitive=True),
    HttpRouteSpec("GET", "/api/prompts/traces", block_module="blocks.prompt.trace", sensitive=True),
    HttpRouteSpec(
        "GET",
        "/api/prompts/traces/{trace_id}",
        block_module="blocks.prompt.trace",
        path_inject={"trace_id": "trace_id"},
        sensitive=True,
    ),
    HttpRouteSpec("POST", "/api/prompts/toggle", block_module="blocks.prompt.toggle", sensitive=True),
    HttpRouteSpec(
        "POST",
        "/api/prompts/preview-toggle",
        block_module="blocks.prompt.preview_toggle",
        sensitive=True,
    ),
    HttpRouteSpec("GET", "/api/prompts/editor", block_module="blocks.prompt.contract_adapter", defaults={"_contract_operation": "editor.load"}, sensitive=True),
    HttpRouteSpec(
        "POST",
        "/api/prompts/editor/save",
        block_module="blocks.prompt.contract_adapter",
        defaults={"_contract_operation": "save"},
        sensitive=True,
    ),
    HttpRouteSpec(
        "POST",
        "/api/prompts/override",
        block_module="blocks.prompt.contract_adapter",
        defaults={"_contract_operation": "override"},
        sensitive=True,
    ),
    HttpRouteSpec("POST", "/api/prompts/diff", block_module="blocks.prompt.contract_adapter", defaults={"_contract_operation": "diff"}, sensitive=True),
    HttpRouteSpec("POST", "/api/prompts/test", block_module="blocks.prompt.contract_adapter", defaults={"_contract_operation": "test"}, sensitive=True),
    HttpRouteSpec("GET", "/api/prompts/migration/inspect", block_module="blocks.prompt.migration_adapter", defaults={"_migration_operation": "inspect"}, sensitive=True),
    HttpRouteSpec("POST", "/api/prompts/migration/apply", block_module="blocks.prompt.migration_adapter", defaults={"_migration_operation": "apply"}, sensitive=True),
    HttpRouteSpec("POST", "/api/prompts/migration/rollback", block_module="blocks.prompt.migration_adapter", defaults={"_migration_operation": "rollback"}, sensitive=True),
    HttpRouteSpec(
        "GET",
        "/api/prompts/{name}/versions",
        block_module="blocks.prompt.contract_adapter",
        defaults={"_contract_operation": "versions"},
        path_inject={"name": "name"},
        sensitive=True,
    ),
    HttpRouteSpec(
        "POST",
        "/api/prompts/{name}/versions",
        block_module="blocks.prompt.contract_adapter",
        defaults={"_contract_operation": "save"},
        path_inject={"name": "name"},
        sensitive=True,
    ),
    HttpRouteSpec(
        "PUT",
        "/api/prompts/{name}/versions/{version}",
        block_module="blocks.prompt.contract_adapter",
        defaults={"_contract_operation": "rollback"},
        path_inject={"name": "name", "version": "version"},
        sensitive=True,
    ),
    HttpRouteSpec(
        "POST",
        "/api/prompts/{name}/rollback",
        block_module="blocks.prompt.contract_adapter",
        defaults={"_contract_operation": "rollback"},
        path_inject={"name": "name"},
        sensitive=True,
    ),
    HttpRouteSpec("PUT", "/api/prompts/{name}", block_module="blocks.prompt.contract_adapter", path_inject={"name": "name"}, defaults={"_contract_operation": "save"}, sensitive=True),
    HttpRouteSpec("DELETE", "/api/prompts/{name}", block_module="blocks.prompt.contract_adapter", path_inject={"name": "name"}, defaults={"_contract_operation": "delete"}, sensitive=True),
    HttpRouteSpec("POST", "/api/prompts/convert", block_module="blocks.prompt.contract_adapter", defaults={"_contract_operation": "convert"}, sensitive=True),
    HttpRouteSpec("POST", "/api/prompts/lint", block_module="blocks.prompt.contract_adapter", defaults={"_contract_operation": "lint"}, sensitive=True),
    HttpRouteSpec("POST", "/api/prompts/compact", block_module="blocks.prompt.contract_adapter", defaults={"_contract_operation": "compact"}, sensitive=True),
    HttpRouteSpec("POST", "/api/prompts/build", block_module="blocks.prompt.contract_adapter", defaults={"_contract_operation": "build"}, sensitive=True),
    HttpRouteSpec("GET", "/api/prompts/context-vars", block_module="blocks.prompt.contract_adapter", defaults={"_contract_operation": "context_vars"}, sensitive=True),
    HttpRouteSpec(
        "POST",
        "/api/prompts/{name}/conditional",
        block_module="blocks.prompt.contract_adapter",
        defaults={"_contract_operation": "conditional"},
        path_inject={"name": "name"},
        sensitive=True,
    ),
    HttpRouteSpec(
        "POST",
        "/api/prompts/{name}/inherit",
        block_module="blocks.prompt.contract_adapter",
        defaults={"_contract_operation": "inherit"},
        path_inject={"name": "name"},
        sensitive=True,
    ),
    HttpRouteSpec("POST", "/api/prompts/preview", block_module="blocks.prompt.contract_adapter", defaults={"_contract_operation": "preview"}, sensitive=True),
]


def prompt_http_route_specs() -> List[HttpRouteSpec]:
    return list(_PROMPT_HTTP_ROUTE_SPECS)


def _mobile_http_route_specs() -> list[HttpRouteSpec]:
    from ecosystem.defaultspack.domain.mobile.contract import iter_mobile_route_contracts

    admin_features = {"pairing_admin", "device_admin", "credentials_admin"}
    return [
        HttpRouteSpec(
            route.method,
            route.pattern,
            block_module=route.block_module,
            flow_id=route.flow_id,
            fallback_block_module=route.fallback_block_module or route.block_module,
            path_inject=dict(route.path_inject),
            defaults=dict(route.defaults),
            sensitive=route.feature in admin_features,
            local_only=route.feature in admin_features,
        )
        for route in iter_mobile_route_contracts()
    ]


_FALLBACK_HTTP_ROUTE_SPECS: list[HttpRouteSpec] = []
_SUBAGENT_TEAM_HTTP_ROUTE_SPECS = [
    spec for spec in _FALLBACK_HTTP_ROUTE_SPECS if spec.pattern.startswith("/api/subagent-team")
]

_ALWAYS_AVAILABLE_HTTP_ROUTE_SPECS = [
    HttpRouteSpec("GET", "/api/health", handler_name="_handle_health"),
    HttpRouteSpec("GET", "/api/context", handler_name="_handle_context_info"),
    HttpRouteSpec("GET", "/api/desktop-system-info", handler_name="_handle_desktop_system_info"),
    HttpRouteSpec("GET", "/", handler_name="_handle_chat_redirect"),
    HttpRouteSpec("GET", "/chat", handler_name="_handle_static"),
    HttpRouteSpec("GET", "/defaultspack", handler_name="_handle_static"),
    HttpRouteSpec("GET", "/pack/defaultspack", handler_name="_handle_static"),
    HttpRouteSpec("GET", "/coding", handler_name="_handle_static"),
    HttpRouteSpec("GET", "/desktops", handler_name="_handle_static"),
    HttpRouteSpec("GET", "/calendar", handler_name="_handle_static"),
    HttpRouteSpec("GET", "/approval", handler_name="_handle_static"),
    HttpRouteSpec("GET", "/prompts", handler_name="_handle_static"),
    HttpRouteSpec("POST", "/api/authority/browser-exchange", handler_name="_handle_authority_browser_exchange"),
    HttpRouteSpec("POST", "/api/authority/browser-exchange/revoke", handler_name="_handle_authority_browser_exchange_revoke"),
    HttpRouteSpec("GET", "/share/{token}", handler_name="_handle_static"),
    HttpRouteSpec("POST", "/api/authority/browser-ui-operator", handler_name="_handle_authority_browser_ui_operator"),
    HttpRouteSpec("GET", "/ambient", handler_name="_handle_static"),
    HttpRouteSpec("GET", "/ambient-debug", handler_name="_handle_static"),
    HttpRouteSpec("GET", "/finger-recording", handler_name="_handle_static"),
    HttpRouteSpec("GET", "/console", handler_name="_handle_static"),
    HttpRouteSpec("GET", "/host-permissions", handler_name="_handle_static"),
    HttpRouteSpec("GET", "/adaptive", handler_name="_handle_static"),
    HttpRouteSpec("GET", "/operating-profile", handler_name="_handle_static"),
    *_SUBAGENT_TEAM_HTTP_ROUTE_SPECS,
    HttpRouteSpec("GET", "/static/{path}", handler_name="_handle_static_file"),
]


class TransportRegistry:
    """Transport extension lookup for route/entrypoint migration."""

    def __init__(self) -> None:
        self._registry = get_extension_registry().transports()

    def list_transports(self) -> List[Dict[str, Any]]:
        return self._registry.list(enabled_only=True)

    def get_transport(self, transport_id: str) -> Optional[Dict[str, Any]]:
        return self._registry.get(transport_id)


def build_http_routes_from_specs(server: Any, specs: List[HttpRouteSpec]):
    routes = []
    ordered_specs = [
        spec
        for _, spec in sorted(
            enumerate(specs),
            key=lambda item: http_route_sort_key(item[1].method, item[1].pattern, item[0]),
        )
    ]
    for spec in ordered_specs:
        compiled = compile_http_route_pattern(spec.pattern)
        require_legacy_route_allowlisted(spec)
        handler: Callable[..., Any]
        if spec.flow_id:

            def _flow_handler(
                request_data,
                path_params,
                *,
                flow_id=spec.flow_id,
                fallback_block_module=spec.legacy_block_module or spec.fallback_block_module,
                path_inject=dict(spec.path_inject),
                route_defaults=dict(spec.defaults),
                route_method=spec.method,
            ):
                payload = dict(request_data or {})
                payload.update(route_defaults)
                payload["_method"] = route_method
                return server._invoke_flow_route(
                    flow_id,
                    payload,
                    path_params,
                    path_inject,
                    fallback_block_module=fallback_block_module,
                )

            handler = _flow_handler
        elif spec.function_id:

            def _function_handler(
                request_data,
                path_params,
                *,
                function_name=spec.function_name or spec.function_id,
                fallback_block_module=spec.legacy_block_module
                or spec.fallback_block_module
                or spec.block_module,
                path_inject=dict(spec.path_inject),
                route_defaults=dict(spec.defaults),
                route_method=spec.method,
            ):
                payload = dict(request_data or {})
                payload.update(route_defaults)
                payload["_method"] = route_method
                function_route = getattr(server, "_invoke_function_route", None)
                if callable(function_route):
                    return function_route(
                        function_name,
                        payload,
                        path_params,
                        path_inject,
                        fallback_block_module=fallback_block_module,
                    )
                if fallback_block_module:
                    return server._invoke_fallback_block(
                        fallback_block_module,
                        payload,
                        path_params,
                        path_inject,
                    )
                raise AttributeError("_invoke_function_route")

            handler = _function_handler
        elif spec.legacy_block_module or spec.block_module:

            def _block_handler(
                request_data,
                path_params,
                *,
                block_module=spec.legacy_block_module or spec.block_module,
                path_inject=dict(spec.path_inject),
                route_defaults=dict(spec.defaults),
                route_method=spec.method,
            ):
                payload = dict(request_data or {})
                payload.update(route_defaults)
                payload["_method"] = route_method
                return server._invoke_fallback_block(
                    block_module,
                    payload,
                    path_params,
                    path_inject,
                )

            handler = _block_handler
        else:
            handler = getattr(server, spec.handler_name)
        _set_http_route_handler_metadata(handler, spec)
        routes.append((spec.method, compiled, handler, "fallback", dict(spec.path_inject)))
    return routes


def _set_http_route_handler_metadata(handler: Any, spec: HttpRouteSpec) -> None:
    target = getattr(handler, "__func__", handler)
    try:
        setattr(target, "__rumi_route_pattern__", spec.pattern)
        setattr(target, "__rumi_route_sensitive__", bool(spec.sensitive))
        setattr(target, "__rumi_route_pre_auth__", bool(spec.pre_auth))
        setattr(target, "__rumi_route_local_only__", bool(spec.local_only))
        setattr(target, "__rumi_route_authority__", http_route_authority_metadata(spec))
    except Exception:
        pass


def http_route_authority_metadata(spec: HttpRouteSpec) -> dict[str, Any]:
    metadata: dict[str, Any] = {
        "permission_id": str(spec.permission_id or "").strip(),
        "owner_pack_id": str(spec.owner_pack_id or "").strip(),
        "provider_id": str(spec.provider_id or "").strip(),
        "frontend_id": str(spec.frontend_id or "").strip(),
        "audience": str(spec.audience or "kernel_api").strip(),
        "resource_template": dict(spec.resource_template or {}),
    }
    if spec.core_only:
        metadata["core_only"] = True
    if spec.function_id:
        metadata["function_id"] = spec.function_id
    return {key: value for key, value in metadata.items() if value not in ("", {}, None)}


def build_always_available_http_routes(server: Any):
    return build_http_routes_from_specs(server, _ALWAYS_AVAILABLE_HTTP_ROUTE_SPECS)


def build_fallback_http_routes(server: Any):
    return build_http_routes_from_specs(
        server,
        canonical_http_route_specs(include_always_available=True),
    )
