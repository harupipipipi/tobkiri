"""Capability Graph binding handlers owned by defaultspack."""

from __future__ import annotations

from typing import Any, Dict

from core_runtime.surface_launch_target import surface_launch_target_from_instance
from ecosystem.defaultspack.defaultspack.surface_contributions import (
    migrate_defaultspack_frontend_surface_contributions,
    register_defaultspack_surface_contribution,
)


def register_defaultspack_binding_handlers(interface_registry: Any) -> Dict[str, Any]:
    """Register defaultspack binding handlers in InterfaceRegistry."""
    handlers = {
        "defaultspack:agent.compile_node": compile_agent_node,
        "defaultspack:agent.bind_ai": bind_agent_ai,
        "defaultspack:agent.bind_tools": bind_agent_tools,
        "defaultspack:ai_client.compile_node": compile_ai_client_node,
        "defaultspack:tool.compile_node": compile_tool_node,
        "defaultspack:frontend.compile_node": compile_frontend_node,
        "defaultspack:frontend.bind_surface": bind_frontend_surface,
        "defaultspack:cli_surface.compile_node": compile_cli_surface_node,
        "defaultspack:memory.compile_node": compile_memory_node,
        "defaultspack:prompt.compile_node": compile_prompt_node,
        "defaultspack:agent.bind_memory": bind_agent_memory,
        "defaultspack:agent.bind_prompt": bind_agent_prompt,
    }
    for handler_id, handler in handlers.items():
        if interface_registry.get(handler_id) is not None:
            continue
        interface_registry.register(
            handler_id,
            handler,
            meta={
                "source": "defaultspack.capability_bindings",
                "pack_id": "defaultspack",
            },
        )
    return {
        "status": "ok",
        "registered": sorted(handlers),
    }


def compile_agent_node(runtime_profile: Dict[str, Any], instance: Any) -> None:
    agent = _agent_record(runtime_profile, instance)
    agent.setdefault("ai", None)
    agent.setdefault("tools", [])


def bind_agent_ai(runtime_profile: Dict[str, Any], source: Any, target: Any) -> None:
    agent = _agent_record(runtime_profile, target)
    agent["ai"] = _instance_ref(source)


def bind_agent_tools(runtime_profile: Dict[str, Any], source: Any, target: Any) -> None:
    agent = _agent_record(runtime_profile, target)
    tools = agent.setdefault("tools", [])
    tool_ref = _instance_ref(source)
    if tool_ref not in tools:
        tools.append(tool_ref)


def bind_agent_memory(runtime_profile: Dict[str, Any], source: Any, target: Any) -> None:
    agent = _agent_record(runtime_profile, target)
    memory_ref = _instance_ref(source)
    agent["memory"] = memory_ref


def bind_agent_prompt(runtime_profile: Dict[str, Any], source: Any, target: Any) -> None:
    agent = _agent_record(runtime_profile, target)
    prompt_ref = _instance_ref(source)
    agent["prompt"] = prompt_ref


def compile_ai_client_node(runtime_profile: Dict[str, Any], instance: Any) -> None:
    _section(runtime_profile, "ai_clients")[_instance_ref(instance)] = _node_record(instance)


def compile_tool_node(runtime_profile: Dict[str, Any], instance: Any) -> None:
    _section(runtime_profile, "tools")[_instance_ref(instance)] = _node_record(instance)


def compile_frontend_node(runtime_profile: Dict[str, Any], instance: Any) -> None:
    frontend = _frontend_record(runtime_profile, instance)
    frontend.setdefault("surface", None)
    frontend.setdefault("surfaces", [])


def bind_frontend_surface(
    runtime_profile: Dict[str, Any],
    source: Any,
    target: Any,
    nodes: Dict[str, Any] | None = None,
    profile: Any = None,
) -> Dict[str, Any] | None:
    migrate_defaultspack_frontend_surface_contributions(runtime_profile)
    frontend = _frontend_record(runtime_profile, target)
    surface_ref = _instance_ref(source)
    frontend["surface"] = surface_ref
    surfaces = frontend.setdefault("surfaces", [])
    if surface_ref not in surfaces:
        surfaces.append(surface_ref)
    diagnostics: list[Dict[str, Any]] = []
    launch_target = surface_launch_target_from_instance(
        runtime_profile=runtime_profile,
        instance=source,
        nodes=nodes or {},
        profile=profile,
        surfaces=_profile_surfaces(profile),
        diagnostics=diagnostics,
    )
    if launch_target:
        contribution = register_defaultspack_surface_contribution(
            runtime_profile,
            launch_target,
        )
        frontend["surface_launch_target"] = contribution["target"]
        runtime_profile.setdefault("launch", {})["surface"] = contribution["target"]
    if diagnostics:
        return {"diagnostics": diagnostics}
    return None


def compile_cli_surface_node(runtime_profile: Dict[str, Any], instance: Any) -> None:
    _section(runtime_profile, "cli_surfaces")[_instance_ref(instance)] = _node_record(instance)


def compile_memory_node(runtime_profile: Dict[str, Any], instance: Any) -> None:
    _section(runtime_profile, "memory")[_instance_ref(instance)] = _node_record(instance)


def compile_prompt_node(runtime_profile: Dict[str, Any], instance: Any) -> None:
    _section(runtime_profile, "prompts")[_instance_ref(instance)] = _node_record(instance)


def _agent_record(runtime_profile: Dict[str, Any], instance: Any) -> Dict[str, Any]:
    agents = _section(runtime_profile, "agents")
    ref = _instance_ref(instance)
    agents.setdefault(ref, _node_record(instance))
    return agents[ref]


def _frontend_record(runtime_profile: Dict[str, Any], instance: Any) -> Dict[str, Any]:
    frontends = _section(runtime_profile, "frontends")
    ref = _instance_ref(instance)
    frontends.setdefault(ref, _node_record(instance))
    return frontends[ref]


def _section(runtime_profile: Dict[str, Any], name: str) -> Dict[str, Any]:
    defaultspack = runtime_profile.setdefault("defaultspack", {})
    section = defaultspack.setdefault(name, {})
    if not isinstance(section, dict):
        section = {}
        defaultspack[name] = section
    return section


def _node_record(instance: Any) -> Dict[str, Any]:
    return {
        "node_instance_id": _instance_ref(instance),
        "node_id": getattr(instance, "ref", None),
    }


def _instance_ref(instance: Any) -> str:
    return str(getattr(instance, "id", ""))


def _profile_surfaces(profile: Any) -> Dict[str, Any] | None:
    surfaces = getattr(profile, "surfaces", None)
    return dict(surfaces) if isinstance(surfaces, dict) else None
