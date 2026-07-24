"""Bridge Startup Profiles to compiled Capability Graph runtime profiles."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any, Dict, List, Optional

from .capability_graph_compiler import CapabilityGraphCompiler
from .capability_graph_loader import CapabilityGraphLoader
from .ecosystem_nodes import EcosystemNodeRegistry
from .frontend_host import build_frontend_catalog
from .interface_registry import InterfaceRegistry
from .profile_models import CapabilityProfileDefinition
from .profile_loader import CapabilityProfileLoader
from .resolved_profile import (
    ResolvedProfile,
    resolution_input_from_startup_profile,
    resolve_profile,
)
from .resolved_profile_scope import (
    activate_resolved_profile,
    restore_resolved_profile,
)
from .startup_graph_overrides import apply_startup_node_overrides
from .surface_launch_target import extract_surface_launch_target


@dataclass
class StartupCapabilityCompileResult:
    ok: bool
    graph_id: Optional[str] = None
    capability_profile_id: Optional[str] = None
    runtime_profile_key: Optional[str] = None
    runtime_profile: Optional[Dict[str, Any]] = None
    surface_launch_target: Optional[Dict[str, Any]] = None
    resolved_profile: Optional[ResolvedProfile] = None
    frontend_catalog: Optional[Dict[str, Any]] = None
    diagnostics: List[Dict[str, Any]] = field(default_factory=list)
    skipped: bool = False
    reason: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ok": self.ok,
            "skipped": self.skipped,
            "reason": self.reason,
            "graph_id": self.graph_id,
            "capability_profile_id": self.capability_profile_id,
            "runtime_profile_key": self.runtime_profile_key,
            "runtime_profile": self.runtime_profile,
            "surface_launch_target": self.surface_launch_target,
            "resolved_profile": (
                self.resolved_profile.to_dict() if self.resolved_profile else None
            ),
            "frontend_catalog": self.frontend_catalog,
            "diagnostics": list(self.diagnostics),
        }


def compile_startup_capabilities(
    startup_profile: Dict[str, Any],
    *,
    interface_registry: InterfaceRegistry,
    approval_manager: Any = None,
    ecosystem_dir: Optional[str] = None,
    register: bool = True,
) -> StartupCapabilityCompileResult:
    """Compile the startup profile's opt-in Capability Graph bridge."""
    graph_id = _string_or_none(startup_profile.get("default_graph"))
    capability_profile_id = _string_or_none(startup_profile.get("capability_profile_id"))
    diagnostics: List[Dict[str, Any]] = []
    resolved_profile: Optional[ResolvedProfile] = None
    frontend_catalog: Optional[Dict[str, Any]] = None
    activation_token = None

    if not graph_id:
        diagnostics.append(
            _diagnostic(
                "warning",
                "startup_graph_missing",
                "Startup profile has launch_capability_graph enabled but no default_graph",
            )
        )
        return StartupCapabilityCompileResult(
            ok=False,
            graph_id=None,
            capability_profile_id=capability_profile_id,
            diagnostics=diagnostics,
        )

    if not capability_profile_id:
        diagnostics.append(
            _diagnostic(
                "warning",
                "startup_capability_profile_missing",
                "Startup profile has launch_capability_graph enabled but no capability_profile_id",
                graph_id=graph_id,
            )
        )
        return StartupCapabilityCompileResult(
            ok=False,
            graph_id=graph_id,
            capability_profile_id=None,
            diagnostics=diagnostics,
        )

    try:
        normalized_startup_profile = dict(startup_profile)
        normalized_startup_profile.setdefault("profile_id", capability_profile_id)
        provisional_input = resolution_input_from_startup_profile(
            normalized_startup_profile
        )
        provisional_profile = resolve_profile(
            provisional_input,
            ecosystem_dir=ecosystem_dir,
        )
        verified_pack_trust = _verified_pack_trust(
            approval_manager,
            provisional_profile.selected_pack_ids
        )
        resolution_input = resolution_input_from_startup_profile(
            normalized_startup_profile,
            verified_pack_trust=verified_pack_trust,
        )
        resolution_input = replace(
            resolution_input,
            authorized_pack_ids=tuple(verified_pack_trust),
        )
        resolved_profile = resolve_profile(
            resolution_input,
            ecosystem_dir=ecosystem_dir,
        )
        diagnostics.extend(
            {
                "level": item.severity,
                "code": item.code,
                "message": item.message,
                "subject": item.subject,
                "details": dict(item.details),
            }
            for item in resolved_profile.diagnostics
        )
        if not resolved_profile.effective_pack_set:
            diagnostics.append(
                _diagnostic(
                    "error",
                    "effective_pack_set_empty",
                    "Resolved profile has no healthy authorized packs",
                    profile_id=resolution_input.profile_id,
                )
            )
            return StartupCapabilityCompileResult(
                ok=False,
                graph_id=graph_id,
                capability_profile_id=capability_profile_id,
                resolved_profile=resolved_profile,
                diagnostics=diagnostics,
            )
        frontend_catalog_result = build_frontend_catalog(
            resolved_profile,
            ecosystem_dir=ecosystem_dir,
        )
        frontend_catalog = frontend_catalog_result.to_dict()
        diagnostics.extend(
            {
                "level": item.severity,
                "code": item.code,
                "message": item.message,
                "pack_id": item.owner_pack_id,
                "contribution_id": item.contribution_id,
            }
            for item in frontend_catalog_result.diagnostics
        )
        profile_loader = CapabilityProfileLoader(
            interface_registry=interface_registry,
            approval_manager=approval_manager,
            ecosystem_dir=ecosystem_dir,
            effective_pack_ids=resolved_profile.effective_pack_set,
        )
        profile = profile_loader.get_profile(capability_profile_id)
        diagnostics.extend(profile_loader.diagnostics)
        if profile is None:
            diagnostics.append(
                _diagnostic(
                    "error",
                    "capability_profile_not_found",
                    f"Capability profile '{capability_profile_id}' was not found",
                    capability_profile_id=capability_profile_id,
                )
            )
            return StartupCapabilityCompileResult(
                ok=False,
                graph_id=graph_id,
                capability_profile_id=capability_profile_id,
                resolved_profile=resolved_profile,
                frontend_catalog=frontend_catalog,
                diagnostics=diagnostics,
            )

        graph_loader = CapabilityGraphLoader(
            interface_registry=interface_registry,
            approval_manager=approval_manager,
            ecosystem_dir=ecosystem_dir,
            effective_pack_ids=resolved_profile.effective_pack_set,
        )
        graph = graph_loader.get_graph(graph_id)
        diagnostics.extend(graph_loader.diagnostics)
        if graph is None:
            diagnostics.append(
                _diagnostic(
                    "error",
                    "startup_graph_not_found",
                    f"Capability graph '{graph_id}' was not found",
                    graph_id=graph_id,
                )
            )
            return StartupCapabilityCompileResult(
                ok=False,
                graph_id=graph_id,
                capability_profile_id=capability_profile_id,
                resolved_profile=resolved_profile,
                frontend_catalog=frontend_catalog,
                diagnostics=diagnostics,
            )

        node_registry = EcosystemNodeRegistry(
            interface_registry=interface_registry,
            approval_manager=approval_manager,
            ecosystem_dir=ecosystem_dir,
            effective_pack_ids=resolved_profile.effective_pack_set,
        )
        nodes = node_registry.load_all_nodes(register=True)
        diagnostics.extend(node_registry.diagnostics)

        graph, override_diagnostics = apply_startup_node_overrides(
            graph,
            startup_profile=startup_profile,
            nodes=dict(nodes),
        )
        diagnostics.extend(override_diagnostics)
        if any(item.get("level") == "error" for item in override_diagnostics):
            return StartupCapabilityCompileResult(
                ok=False,
                graph_id=graph_id,
                capability_profile_id=capability_profile_id,
                resolved_profile=resolved_profile,
                frontend_catalog=frontend_catalog,
                diagnostics=diagnostics,
            )

        activation_token = activate_resolved_profile(resolved_profile)
        _register_pack_binding_handlers(
            interface_registry,
            diagnostics,
            approval_manager=approval_manager,
            ecosystem_dir=ecosystem_dir,
            effective_pack_ids=resolved_profile.effective_pack_set,
        )

        profile = extend_profile_for_startup_overrides(
            profile,
            startup_profile=startup_profile,
            graph=graph,
        )
        compile_result = CapabilityGraphCompiler(interface_registry=interface_registry).compile(
            graph,
            profile=profile,
            nodes=dict(nodes),
            register=register,
        )
        diagnostics.extend(compile_result.diagnostics)
        runtime_profile = compile_result.runtime_profile
        if isinstance(runtime_profile, dict):
            runtime_profile = _apply_startup_runtime_selection(runtime_profile, startup_profile)
        runtime_profile_key = None
        if isinstance(runtime_profile, dict):
            runtime_profile_key = _string_or_none(runtime_profile.get("registry_key"))
        surface_launch_target = extract_surface_launch_target(
            runtime_profile,
            fallback_pack_id=_string_or_none(startup_profile.get("base_pack")),
            surfaces=_surfaces_from_startup_or_profile(startup_profile, profile),
        )
        if not compile_result.ok and activation_token is not None:
            restore_resolved_profile(activation_token)
            activation_token = None

        return StartupCapabilityCompileResult(
            ok=compile_result.ok,
            graph_id=graph_id,
            capability_profile_id=capability_profile_id,
            runtime_profile_key=runtime_profile_key,
            runtime_profile=runtime_profile,
            surface_launch_target=surface_launch_target,
            resolved_profile=resolved_profile,
            frontend_catalog=frontend_catalog,
            diagnostics=diagnostics,
        )
    except Exception as exc:
        if activation_token is not None:
            restore_resolved_profile(activation_token)
        diagnostics.append(
            _diagnostic(
                "error",
                "startup_capability_compile_failed",
                f"Startup capability graph compile failed: {exc}",
                graph_id=graph_id,
                capability_profile_id=capability_profile_id,
            )
        )
        return StartupCapabilityCompileResult(
            ok=False,
            graph_id=graph_id,
            capability_profile_id=capability_profile_id,
            resolved_profile=resolved_profile,
            frontend_catalog=frontend_catalog,
            diagnostics=diagnostics,
        )


def _pack_is_approved(approval_manager: Any, pack_id: str) -> bool:
    """Return existing approval evidence without trusting profile selection."""
    if approval_manager is None:
        try:
            from .approval_manager import get_approval_manager

            approval_manager = get_approval_manager()
        except Exception:
            return False
    checker = getattr(approval_manager, "is_pack_approved_and_verified", None)
    if not callable(checker):
        return False
    try:
        result = checker(pack_id)
    except Exception:
        return False
    if isinstance(result, tuple):
        return bool(result[0])
    return bool(result)


def _verified_pack_trust(
    approval_manager: Any,
    pack_ids: tuple[str, ...],
) -> Dict[str, str]:
    """Read host-verified trust while preserving test/legacy manager support."""
    if approval_manager is None:
        try:
            from .approval_manager import get_approval_manager

            approval_manager = get_approval_manager()
        except Exception:
            return {}
    getter = getattr(approval_manager, "get_verified_pack_trust", None)
    if callable(getter):
        try:
            result = getter(pack_ids)
        except Exception:
            return {}
        if isinstance(result, dict):
            return {
                str(pack_id): str(trust_class)
                for pack_id, trust_class in result.items()
            }
        return {}
    return {
        pack_id: "verified"
        for pack_id in pack_ids
        if _pack_is_approved(approval_manager, pack_id)
    }


def _register_pack_binding_handlers(
    interface_registry: InterfaceRegistry,
    diagnostics: List[Dict[str, Any]],
    *,
    approval_manager: Any = None,
    ecosystem_dir: Optional[str] = None,
    effective_pack_ids: Optional[List[str] | tuple[str, ...]] = None,
) -> None:
    try:
        from .capability_binding_registration import register_pack_binding_handlers
    except Exception as exc:
        diagnostics.append(
            _diagnostic(
                "warning",
                "pack_binding_registration_unavailable",
                f"Pack binding registration is unavailable: {exc}",
            )
        )
        return

    result = register_pack_binding_handlers(
        interface_registry=interface_registry,
        approval_manager=approval_manager,
        ecosystem_dir=ecosystem_dir,
        effective_pack_ids=effective_pack_ids,
    )
    diagnostics.extend(result.diagnostics)


def _string_or_none(value: Any) -> Optional[str]:
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value or None


def extend_profile_for_startup_overrides(
    profile: CapabilityProfileDefinition,
    *,
    startup_profile: Dict[str, Any],
    graph: Any,
) -> CapabilityProfileDefinition:
    """Allow startup-profile pack override nodes only for this launch compile."""
    if not profile.enabled_nodes:
        return profile
    allowed_packs = {
        str(pack_id)
        for pack_id in startup_profile.get("packs", [])
        if isinstance(pack_id, str) and pack_id
    }
    graph_metadata = getattr(graph, "metadata", {})
    override_refs = graph_metadata.get("startup_override_node_refs") if isinstance(graph_metadata, dict) else None
    if not isinstance(override_refs, list):
        override_refs = []
    extra_nodes = {
        node_ref
        for node_ref in override_refs
        if isinstance(node_ref, str)
        and _node_pack_id(node_ref) in allowed_packs
        and node_ref not in profile.enabled_nodes
    }
    if not extra_nodes:
        return profile
    return replace(
        profile,
        enabled_nodes=sorted(set(profile.enabled_nodes) | extra_nodes),
        metadata={
            **dict(profile.metadata),
            "startup_override_nodes": sorted(extra_nodes),
        },
    )


def _surfaces_from_startup_or_profile(
    startup_profile: Dict[str, Any],
    profile: CapabilityProfileDefinition,
) -> Dict[str, Any]:
    startup_surfaces = startup_profile.get("surfaces")
    if isinstance(startup_surfaces, dict):
        return dict(startup_surfaces)
    return dict(profile.surfaces)


def _node_pack_id(node_ref: str) -> str:
    return node_ref.split(".", 1)[0] if isinstance(node_ref, str) and "." in node_ref else ""


def _diagnostic(level: str, code: str, message: str, **meta: Any) -> Dict[str, Any]:
    return {
        "level": level,
        "code": code,
        "message": message,
        **meta,
    }


def _apply_startup_runtime_selection(
    runtime_profile: Dict[str, Any],
    startup_profile: Dict[str, Any],
) -> Dict[str, Any]:
    selected = {}
    metadata = startup_profile.get("metadata")
    if isinstance(metadata, dict) and isinstance(metadata.get("selected"), dict):
        selected = metadata.get("selected") or {}
    tools = [
        str(item).strip()
        for item in (selected.get("tools") if isinstance(selected, dict) and isinstance(selected.get("tools"), list) else [])
        if str(item or "").strip()
    ]
    policy = dict(runtime_profile.get("policy") if isinstance(runtime_profile.get("policy"), dict) else {})
    startup_policy = startup_profile.get("policy") if isinstance(startup_profile.get("policy"), dict) else {}
    policy.update(startup_policy)
    if "tool_allowlist" not in policy and tools:
        policy["tool_allowlist"] = list(tools)
    runtime_profile["policy"] = policy

    if not tools:
        return runtime_profile

    defaultspack = runtime_profile.get("defaultspack")
    if not isinstance(defaultspack, dict):
        defaultspack = {}
        runtime_profile["defaultspack"] = defaultspack
    agents = defaultspack.get("agents")
    if not isinstance(agents, dict) or not agents:
        defaultspack["agents"] = {"profile_selected": {"tools": list(tools)}}
        return runtime_profile

    selected_tools = set(tools)
    for agent_key, agent_config in list(agents.items()):
        if not isinstance(agent_config, dict):
            continue
        current_tools = agent_config.get("tools")
        if isinstance(current_tools, list) and current_tools:
            agent_config["tools"] = [
                str(item).strip()
                for item in current_tools
                if str(item or "").strip() in selected_tools
            ]
        else:
            agent_config["tools"] = list(tools)
        agents[agent_key] = agent_config
    defaultspack["agents"] = agents
    return runtime_profile
