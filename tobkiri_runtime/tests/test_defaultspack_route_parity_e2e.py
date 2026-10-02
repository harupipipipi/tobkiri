"""Route parity E2E across the HTTP / Function / Flow / Mobile / offline surfaces.

Merge-gate requirement (formal review, required condition 7): every route a
dispatch surface declares must resolve to a registered implementation, and
surfaces that project the same route must agree on the bound target. A route
that cannot resolve must fail closed instead of silently drifting to a
different implementation.
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path
from typing import Any, Iterable

import pytest


ROOT = Path(__file__).resolve().parent.parent
DEFAULTSPACK_ROOT = ROOT / "ecosystem" / "defaultspack"

sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(DEFAULTSPACK_ROOT))


def _flow_ids() -> set[str]:
    from domain.flow import FlowEngine

    return set(FlowEngine()._flows.keys())


def _module_resolvable(module_name: str) -> bool:
    try:
        module = importlib.import_module(module_name)
    except Exception:
        return False
    return callable(getattr(module, "run", None))


def _http_spec_groups() -> dict[str, list[Any]]:
    """Host-owned HTTP route declarations grouped by declaring surface."""

    from ecosystem.defaultspack.transport import registry as http_registry

    return {
        "always_available": list(http_registry._ALWAYS_AVAILABLE_HTTP_ROUTE_SPECS),
        "flow": http_registry.flow_http_route_specs(),
        "prompt": http_registry.prompt_http_route_specs(),
        "mobile": http_registry._mobile_http_route_specs(),
        "component": http_registry.component_http_route_specs(),
        "template": http_registry.template_http_route_specs(DEFAULTSPACK_ROOT),
    }


def _spec_binding_targets(spec: Any) -> list[tuple[str, str, bool]]:
    """Resolve every declared target on one ``HttpRouteSpec``.

    Returns ``(kind, target, resolved)`` triples; a spec with no target at all
    returns an empty list, which is itself a parity violation.
    """

    from domain.function_runtime.registry import get_spec
    from transport.http import DefaultsHttpServer

    targets: list[tuple[str, str, bool]] = []
    function_id = str(getattr(spec, "function_id", "") or "").strip()
    if function_id:
        targets.append(("function", function_id, get_spec(function_id) is not None))
    flow_id = str(getattr(spec, "flow_id", "") or "").strip()
    if flow_id:
        targets.append(("flow", flow_id, flow_id in _flow_ids()))
    block_module = str(getattr(spec, "block_module", "") or "").strip()
    if block_module:
        targets.append(("block", block_module, _module_resolvable(block_module)))
    fallback = str(getattr(spec, "fallback_block_module", "") or "").strip()
    if fallback and fallback != block_module:
        targets.append(("fallback_block", fallback, _module_resolvable(fallback)))
    handler_name = str(getattr(spec, "handler_name", "") or "").strip()
    if handler_name:
        targets.append(
            ("handler", handler_name, hasattr(DefaultsHttpServer, handler_name))
        )
    return targets


def _capture_pack_http_routes() -> list[dict[str, Any]]:
    """Run the pack UI route registration and capture ``io.http.route`` entries."""

    import blocks.ui.setup as ui_setup

    class _CaptureRegistry:
        def __init__(self) -> None:
            self.entries: list[dict[str, Any]] = []

        def register(self, interface: str, entry: dict[str, Any], meta: Any = None):
            del meta
            if interface == "io.http.route":
                self.entries.append(dict(entry))

    registry = _CaptureRegistry()
    result = ui_setup.run(
        {
            "interface_registry": registry,
            "_source_component": "defaultspack:frontend:ui",
            "_settings_owner_port": None,
        }
    )
    assert result["status"] == "ok"
    return registry.entries


def _owner_bound_protocol(tmp_path: Path):
    """Bind the settings owner explicitly to this test's isolated location."""
    from domain.frontend.command_protocol import CommandProtocolRegistry
    from ecosystem.tobkiri_ui_settings_pack.runtime.store import FrontendSettingsStore

    return CommandProtocolRegistry(
        DEFAULTSPACK_ROOT,
        settings_owner=FrontendSettingsStore(tmp_path / "frontend_settings.json"),
        command_state_dir=tmp_path / "command-state",
    )


def _bind_offline_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Point both direct registries and HTTP-bound blocks at tmp state."""

    monkeypatch.setenv(
        "RUMI_DEFAULTSPACK_FRONTEND_SETTINGS_PATH",
        str(tmp_path / "frontend_settings.json"),
    )
    monkeypatch.setenv(
        "RUMI_DEFAULTSPACK_COMMAND_STATE_DIR",
        str(tmp_path / "command-state"),
    )


def _offline_queueable_commands(protocol: Any) -> list[dict[str, Any]]:
    queueable: list[dict[str, Any]] = []
    for command in protocol.catalog()["commands"]:
        execution = command.get("execution")
        if not isinstance(execution, dict):
            continue
        offline = execution.get("offline")
        if not isinstance(offline, dict):
            continue
        if (
            execution.get("kind") == "state_mutation"
            and offline.get("queueable") is True
            and offline.get("backend_authoritative") is True
            and offline.get("semantics") == "set"
        ):
            queueable.append(command)
    return queueable


def _unresolved_http_bindings() -> list[str]:
    """Return every HTTP spec whose declared targets all fail to resolve."""

    unresolved: list[str] = []
    for group_name, specs in _http_spec_groups().items():
        for spec in specs:
            targets = _spec_binding_targets(spec)
            if not targets:
                unresolved.append(
                    f"{group_name}:{spec.method} {spec.pattern} declares no target"
                )
            elif not any(resolved for _kind, _target, resolved in targets):
                detail = ", ".join(f"{kind}={target}" for kind, target, _ in targets)
                unresolved.append(
                    f"{group_name}:{spec.method} {spec.pattern} -> {detail}"
                )
    return unresolved


# ---------------------------------------------------------------------------
# HTTP surface: every declared route resolves a registered implementation.
# ---------------------------------------------------------------------------


def test_http_surface_route_specs_resolve_registered_targets() -> None:
    unresolved = _unresolved_http_bindings()
    assert unresolved == []


def test_http_surface_pack_ui_routes_resolve_registered_blocks() -> None:
    """Each ``io.http.route`` the pack registers must bind real code."""

    entries = _capture_pack_http_routes()
    assert entries, "pack UI surface must register io.http.route entries"

    missing_fields = [
        f"{entry.get('method')} {entry.get('pattern')}"
        for entry in entries
        if not entry.get("method")
        or not entry.get("pattern")
        or not callable(entry.get("handler"))
    ]
    assert missing_fields == []

    unresolved: list[str] = []
    for entry in entries:
        handler = entry["handler"]
        module_name = str(
            getattr(handler, "__rumi_route_block_module__", "") or ""
        ).strip()
        if not module_name:
            # Direct callables (shell/static/authority bridges) are already
            # bound; there is no deferred import to drift.
            continue
        func_name = str(
            getattr(handler, "__rumi_route_block_function__", "run") or "run"
        ).strip()
        try:
            module = importlib.import_module(module_name)
            resolved = callable(getattr(module, func_name, None))
        except Exception:
            resolved = False
        if not resolved:
            unresolved.append(
                f"{entry['method']} {entry['pattern']} -> {module_name}.{func_name}"
            )
    assert unresolved == []


# ---------------------------------------------------------------------------
# Function surface: every advertised function resolves a dispatchable handler.
# ---------------------------------------------------------------------------


def test_function_surface_specs_resolve_dispatchable_handlers() -> None:
    from domain.function_runtime.dispatcher import get_handler
    from domain.function_runtime.manifest_factory import FUNCTION_SPECS_BY_ID

    assert FUNCTION_SPECS_BY_ID, "function surface must expose registered specs"

    unresolved: list[str] = []
    for function_id in sorted(FUNCTION_SPECS_BY_ID):
        try:
            handler = get_handler(function_id)
        except Exception as exc:  # pragma: no cover - reported below
            unresolved.append(f"{function_id}: {exc}")
        else:
            if not callable(handler):
                unresolved.append(f"{function_id}: handler is not callable")
    assert unresolved == []


def test_function_surface_block_bound_specs_import_run() -> None:
    from domain.function_runtime.manifest_factory import FUNCTION_SPECS_BY_ID

    unresolved = [
        f"{function_id} -> {spec.block_module}"
        for function_id, spec in sorted(FUNCTION_SPECS_BY_ID.items())
        if getattr(spec, "block_module", "")
        and not _module_resolvable(spec.block_module)
    ]
    assert unresolved == []


# ---------------------------------------------------------------------------
# Flow surface: YAML-declared HTTP routes round-trip into the HTTP table and
# every declared flow_id is registered in the FlowEngine.
# ---------------------------------------------------------------------------


def _declared_flow_yaml_routes() -> dict[str, set[tuple[str, str]]]:
    from ecosystem.defaultspack.transport.registry import _read_flow_yaml

    declared: dict[str, set[tuple[str, str]]] = {}
    flows_dir = DEFAULTSPACK_ROOT / "flows"
    for yaml_path in sorted(flows_dir.glob("*.flow.yaml")):
        flow_def = _read_flow_yaml(yaml_path)
        flow_id = str(
            flow_def.get("flow_id") or yaml_path.name[: -len(".flow.yaml")]
        ).strip()
        transport = flow_def.get("transport")
        http = transport.get("http") if isinstance(transport, dict) else None
        routes = http.get("routes") if isinstance(http, dict) else None
        if not isinstance(routes, list):
            continue
        for route in routes:
            if not isinstance(route, dict):
                continue
            method = str(route.get("method") or "").strip().upper()
            path = str(route.get("path") or route.get("pattern") or "").strip()
            if method and path:
                declared.setdefault(flow_id, set()).add((method, path))
    return declared


def test_flow_surface_yaml_routes_project_to_http_without_drift() -> None:
    from ecosystem.defaultspack.transport import registry as http_registry

    declared = _declared_flow_yaml_routes()
    assert declared, "flow YAMLs must declare at least one HTTP route"

    projected = {
        (spec.flow_id, spec.method.upper(), spec.pattern)
        for spec in http_registry.flow_http_route_specs()
    }
    missing = [
        f"{flow_id} {method} {path}"
        for flow_id, routes in declared.items()
        for method, path in routes
        if (flow_id, method, path) not in projected
    ]
    assert missing == []

    flows = _flow_ids()
    unregistered = sorted(flow_id for flow_id in declared if flow_id not in flows)
    assert unregistered == []


def test_flow_surface_http_specs_point_at_declared_yaml_routes() -> None:
    from ecosystem.defaultspack.transport import registry as http_registry

    declared_pairs = {
        (flow_id, method, path)
        for flow_id, routes in _declared_flow_yaml_routes().items()
        for method, path in routes
    }
    orphans = [
        f"{spec.flow_id} {spec.method} {spec.pattern}"
        for spec in http_registry.flow_http_route_specs()
        if (spec.flow_id, spec.method.upper(), spec.pattern) not in declared_pairs
    ]
    assert orphans == []


# ---------------------------------------------------------------------------
# Mobile surface: every enabled route contract projects into the HTTP spec
# table preserving the bound target, and every bound target resolves.
# ---------------------------------------------------------------------------


def test_mobile_surface_contracts_project_to_http_specs_without_drift() -> None:
    from ecosystem.defaultspack.domain.mobile.contract import (
        iter_mobile_route_contracts,
    )
    from ecosystem.defaultspack.transport import registry as http_registry

    contracts = list(iter_mobile_route_contracts())
    assert contracts, "mobile surface must expose enabled route contracts"

    specs = {
        (spec.method.upper(), spec.pattern): spec
        for spec in http_registry._mobile_http_route_specs()
    }
    drift: list[str] = []
    for route in contracts:
        key = (route.method.upper(), route.pattern)
        spec = specs.get(key)
        if spec is None:
            drift.append(f"{key[0]} {key[1]} missing from HTTP projection")
            continue
        if spec.block_module != route.block_module:
            drift.append(f"{key[0]} {key[1]} block_module drift")
        if spec.flow_id != route.flow_id:
            drift.append(f"{key[0]} {key[1]} flow_id drift")
        if dict(spec.path_inject) != dict(route.path_inject):
            drift.append(f"{key[0]} {key[1]} path_inject drift")
        if dict(spec.defaults) != dict(route.defaults):
            drift.append(f"{key[0]} {key[1]} defaults drift")
    assert drift == []


def test_mobile_surface_contract_targets_resolve() -> None:
    from ecosystem.defaultspack.domain.mobile.contract import (
        iter_mobile_route_contracts,
    )

    flows = _flow_ids()
    unresolved: list[str] = []
    for route in iter_mobile_route_contracts():
        block_module = str(route.block_module or "").strip()
        flow_id = str(route.flow_id or "").strip()
        if not block_module and not flow_id:
            unresolved.append(f"{route.method} {route.pattern} declares no target")
            continue
        if block_module and not _module_resolvable(block_module):
            unresolved.append(
                f"{route.method} {route.pattern} -> {block_module} unresolved"
            )
        if flow_id and flow_id not in flows:
            unresolved.append(
                f"{route.method} {route.pattern} -> flow {flow_id} unregistered"
            )
    assert unresolved == []


# ---------------------------------------------------------------------------
# Offline surface: the offline HTTP route, its bound block, and every
# queueable command resolve; replay reaches the same backend-authoritative
# state as a direct invoke (E2E parity across surfaces).
# ---------------------------------------------------------------------------


def test_offline_surface_http_route_binds_registered_block() -> None:
    entries = {
        (entry["method"].upper(), entry["pattern"]): entry
        for entry in _capture_pack_http_routes()
    }
    route = entries.get(("POST", "/api/command-protocol/v1/offline"))
    assert route is not None, "offline command route must be registered"
    handler = route["handler"]
    assert callable(handler)
    module_name = getattr(handler, "__rumi_route_block_module__", "")
    assert module_name == "blocks.ui.command_protocol_offline"
    assert _module_resolvable(module_name)


def test_offline_surface_queueable_commands_keep_source_binding(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _bind_offline_state(tmp_path, monkeypatch)
    protocol = _owner_bound_protocol(tmp_path)

    queueable = _offline_queueable_commands(protocol)
    assert queueable, "offline surface must expose queueable commands"

    source_ids = {
        str(item.get("id") or item.get("name") or "")
        for item in protocol._source_commands()
    }
    missing = [
        command["canonical_id"]
        for command in queueable
        if str((command.get("identity") or {}).get("id") or "") not in source_ids
    ]
    assert missing == []


def test_offline_replay_parity_with_direct_invoke(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The offline surface must reach the same authoritative state as invoke.

    E2E: enqueue through the HTTP-bound offline block, replay through the same
    block, then compare with a direct protocol invoke of the same command.
    """

    _bind_offline_state(tmp_path, monkeypatch)
    from blocks.ui import command_protocol_offline
    from ecosystem.tobkiri_ui_settings_pack.runtime.store import FrontendSettingsStore

    owner = FrontendSettingsStore(tmp_path / "frontend_settings.json")
    protocol = _owner_bound_protocol(tmp_path)
    context = {"authenticated_principal_id": "parity-e2e", "profile_id": "default"}

    queued = command_protocol_offline.run(
        {
            "action": "enqueue",
            "command_ref": "defaultspack:deepthink",
            "args": {"enabled": True},
            "idempotency_key": "parity-offline-1",
            "expected_revision": 0,
        },
        context,
        settings_owner=owner,
    )
    assert queued["status"] == "ok"
    assert queued["data"]["status"] == "queued"

    replayed = command_protocol_offline.run(
        {"action": "replay"},
        context,
        settings_owner=owner,
    )
    assert replayed["status"] == "ok"
    results = replayed["data"]["results"]
    assert len(results) == 1
    assert results[0]["state"] == "completed"

    offline_state = protocol.query_states(
        ["defaultspack:models.deepthink_enabled"]
    )["states"][0]
    assert offline_state["value"] is True

    direct = protocol.invoke(
        {
            "command_ref": "defaultspack:deepthink",
            "args": {"enabled": False},
            "mode": "chat",
            "invocation_id": "parity-direct-1",
            "idempotency_key": "parity-direct-1",
            "expected_revision": int(offline_state["revision"]),
        },
        context,
    )
    assert direct["status"] == "succeeded"

    direct_state = protocol.query_states(
        ["defaultspack:models.deepthink_enabled"]
    )["states"][0]
    assert direct_state["value"] is False
    # Both surfaces mutate the same backend-authoritative state ref and
    # advance the same revision sequence.
    assert direct_state["revision"] == offline_state["revision"] + 1


# ---------------------------------------------------------------------------
# Consolidated parity matrix: all five surfaces expose routes and every
# declared route resolves to a registered implementation.
# ---------------------------------------------------------------------------


def test_route_parity_matrix_is_complete(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from domain.function_runtime.manifest_factory import FUNCTION_SPECS_BY_ID
    from ecosystem.defaultspack.domain.mobile.contract import (
        iter_mobile_route_contracts,
    )

    _bind_offline_state(tmp_path, monkeypatch)
    protocol = _owner_bound_protocol(tmp_path)

    matrix: dict[str, int] = {
        "http_pack_routes": len(_capture_pack_http_routes()),
        "http_host_specs": sum(
            len(specs) for specs in _http_spec_groups().values()
        ),
        "function_specs": len(FUNCTION_SPECS_BY_ID),
        "flow_declared_routes": sum(
            len(routes) for routes in _declared_flow_yaml_routes().values()
        ),
        "mobile_route_contracts": len(list(iter_mobile_route_contracts())),
        "offline_queueable_commands": len(_offline_queueable_commands(protocol)),
    }
    empty_surfaces = [name for name, count in matrix.items() if count <= 0]
    assert empty_surfaces == [], f"surfaces without routes: {empty_surfaces}"
    assert _unresolved_http_bindings() == []
