from __future__ import annotations

import json
from pathlib import Path

from core_runtime.pack_api_server import PackAPIHandler


def _handler(**attrs) -> PackAPIHandler:
    handler = object.__new__(PackAPIHandler)
    for key, value in attrs.items():
        setattr(handler, key, value)
    return handler


class FakeKernel:
    def __init__(self) -> None:
        self._startup_ctx = {}
        self.calls: list[tuple[str, dict]] = []
        self.handlers = {
            "kernel:node.list": self._node_list,
            "kernel:node.get": self._node_get,
            "kernel:profile.list": self._profile_list,
            "kernel:profile.get": self._profile_get,
            "kernel:profile.node_state": self._profile_node_state,
            "kernel:graph.load_all": self._graph_load_all,
            "kernel:graph.get": self._graph_get,
            "kernel:graph.validate": self._graph_validate,
            "kernel:graph.compile": self._graph_compile,
        }

    def _resolve_handler(self, handler_id: str):
        return self.handlers.get(handler_id)

    def _record(self, handler_id: str, args: dict) -> None:
        self.calls.append((handler_id, dict(args)))

    def _node_list(self, args: dict, _ctx: dict) -> dict:
        self._record("kernel:node.list", args)
        return {
            "_kernel_step_status": "success",
            "nodes": [_agent_node(), _tool_node()],
        }

    def _node_get(self, args: dict, _ctx: dict) -> dict:
        self._record("kernel:node.get", args)
        nodes = {node["node_id"]: node for node in [_agent_node(), _tool_node()]}
        node = nodes.get(args.get("node_id"))
        return {
            "_kernel_step_status": "success" if node else "failed",
            "node": node,
        }

    def _profile_list(self, args: dict, _ctx: dict) -> dict:
        self._record("kernel:profile.list", args)
        return {
            "_kernel_step_status": "success",
            "profiles": [_profile()],
        }

    def _profile_get(self, args: dict, _ctx: dict) -> dict:
        self._record("kernel:profile.get", args)
        return {
            "_kernel_step_status": "success",
            "profile": _profile() if args.get("profile_id") == "coding" else None,
        }

    def _profile_node_state(self, args: dict, _ctx: dict) -> dict:
        self._record("kernel:profile.node_state", args)
        return {
            "_kernel_step_status": "success",
            "node_state": [
                {
                    "node_id": "sample.agent",
                    "enabled": True,
                    "installed": True,
                    "configured": True,
                    "approved": True,
                    "status": "ready",
                    "missing": [],
                },
                {
                    "node_id": "sample.tool",
                    "enabled": False,
                    "installed": True,
                    "configured": True,
                    "approved": True,
                    "status": "disabled",
                    "missing": [],
                },
            ],
        }

    def _graph_load_all(self, args: dict, _ctx: dict) -> dict:
        self._record("kernel:graph.load_all", args)
        return {
            "_kernel_step_status": "success",
            "graphs": [_graph()],
            "diagnostics": [],
        }

    def _graph_get(self, args: dict, _ctx: dict) -> dict:
        self._record("kernel:graph.get", args)
        return {
            "_kernel_step_status": "success",
            "graph": _graph() if args.get("graph_id") == "coding_graph" else None,
        }

    def _graph_validate(self, args: dict, _ctx: dict) -> dict:
        self._record("kernel:graph.validate", args)
        return {
            "_kernel_step_status": "success",
            "ok": True,
            "diagnostics": [],
        }

    def _graph_compile(self, args: dict, _ctx: dict) -> dict:
        self._record("kernel:graph.compile", args)
        return {
            "_kernel_step_status": "success",
            "ok": True,
            "runtime_profile": {
                "profile_id": args["profile_id"],
                "graph_id": args["graph_id"],
            },
            "diagnostics": [],
        }


def _agent_node() -> dict:
    return {
        "node_id": "sample.agent",
        "display_name": {"ja": "エージェント", "en": "Agent"},
        "description": {"en": "Runtime agent"},
        "kind": "ecosystem.component",
        "ports": [
            {
                "id": "tools",
                "direction": "input",
                "standards": ["rumi.tool.bundle"],
                "aliases": ["tools"],
                "multiple": True,
            }
        ],
        "bindings": {"on_input": {"tools": "sample.bind_tools"}},
        "metadata": {"icon": "bot", "category": "runtime"},
        "requirements": {},
        "permissions": {},
    }


def _tool_node() -> dict:
    return {
        "node_id": "sample.tool",
        "display_name": {"en": "Tool Bundle"},
        "description": {},
        "kind": "ecosystem.component",
        "ports": [
            {
                "id": "tools",
                "direction": "output",
                "standards": ["rumi.tool.bundle"],
                "aliases": ["functions"],
                "multiple": True,
            }
        ],
        "bindings": {},
        "metadata": {"icon": "wrench", "category": "tools"},
        "requirements": {},
        "permissions": {},
    }


def _profile() -> dict:
    return {
        "profile_id": "coding",
        "locale": "ja",
        "display_name": {"ja": "コーディング", "en": "Coding"},
        "description": {},
        "default_graph": "coding_graph",
        "enabled_nodes": ["sample.agent"],
        "disabled_nodes": ["sample.tool"],
        "permissions": {"can_create_profile": False},
        "node_settings": {},
        "policy": {},
    }


def _graph() -> dict:
    return {
        "graph_id": "coding_graph",
        "display_name": {"en": "Coding Graph"},
        "description": {},
        "nodes": [{"id": "agent", "ref": "sample.agent"}],
        "edges": [],
        "metadata": {},
    }


def test_nodes_api_returns_viewer_palette_shape() -> None:
    handler = _handler(kernel=FakeKernel())

    result = handler._capability_get_nodes()

    assert result["count"] == 2
    agent = result["nodes"][0]
    assert agent["label"] == "Agent"
    assert agent["ports"][0]["label"] == "tools"
    assert agent["ports"][0]["standards"] == ["rumi.tool.bundle"]
    assert agent["bindings"]["on_input"]["tools"] == "sample.bind_tools"


def test_profile_nodes_api_filters_palette_by_profile_state_and_locale() -> None:
    fake_kernel = FakeKernel()
    handler = _handler(kernel=fake_kernel)

    result = handler._capability_get_profile_nodes("coding")

    assert result["profile"]["label"] == "コーディング"
    assert [node["node_id"] for node in result["nodes"]] == ["sample.agent", "sample.tool"]
    assert [node["node_id"] for node in result["palette_nodes"]] == ["sample.agent"]
    assert result["nodes"][0]["state"]["status"] == "ready"


def test_profile_nodes_api_tolerates_nodes_without_profile_state() -> None:
    class PartialStateKernel(FakeKernel):
        def _profile_node_state(self, args: dict, _ctx: dict) -> dict:
            self._record("kernel:profile.node_state", args)
            return {"_kernel_step_status": "success", "node_state": []}

    result = _handler(kernel=PartialStateKernel())._capability_get_profile_nodes(
        "coding"
    )

    assert result["count"] == 2
    assert result["palette_nodes"] == []


def test_profile_nodes_api_falls_back_when_state_registry_is_invalid() -> None:
    class InvalidStateKernel(FakeKernel):
        def _profile_node_state(self, args: dict, _ctx: dict) -> dict:
            self._record("kernel:profile.node_state", args)
            return {
                "_kernel_step_status": "failed",
                "_kernel_step_meta": {"error": "legacy profile is invalid"},
            }

    result = _handler(kernel=InvalidStateKernel())._capability_get_profile_nodes(
        "coding"
    )

    assert result["count"] == 2
    assert [node["node_id"] for node in result["palette_nodes"]] == [
        "sample.agent"
    ]
    assert result["nodes"][1]["state"]["status"] == "disabled"


def test_profiles_api_documents_startup_profile_boundary() -> None:
    handler = _handler(kernel=FakeKernel())

    result = handler._capability_get_profiles()

    relation = result["startup_profile_relationship"]
    assert relation["launch_time_source_of_truth"] == "StartupProfileManager"
    assert relation["capability_graph_profiles_role"] == "graph_runtime_presets"


def test_graph_compile_preview_requires_profile_and_does_not_register_by_default() -> None:
    fake_kernel = FakeKernel()
    handler = _handler(kernel=fake_kernel)

    missing_profile = handler._capability_compile_graph("coding_graph", {})
    result = handler._capability_compile_graph("coding_graph", {"profile_id": "coding"})

    assert missing_profile["status_code"] == 400
    assert result["ok"] is True
    assert result["runtime_profile"] == {"profile_id": "coding", "graph_id": "coding_graph"}
    assert (
        "kernel:graph.compile",
        {"graph_id": "coding_graph", "profile_id": "coding", "register": False},
    ) in fake_kernel.calls


def test_graph_compile_preview_returns_surface_launch_target() -> None:
    class SurfaceKernel(FakeKernel):
        def _graph_compile(self, args: dict, _ctx: dict) -> dict:
            self._record("kernel:graph.compile", args)
            return {
                "_kernel_step_status": "success",
                "ok": True,
                "runtime_profile": {
                    "profile": {"surfaces": {"preferred": "browser", "enabled": ["browser"]}},
                    "launch": {
                        "surface": {
                            "kind": "desktop_app",
                            "pack_id": "frontendpack",
                            "node_id": "frontendpack.web_surface",
                        }
                    },
                },
                "diagnostics": [],
            }

    handler = _handler(kernel=SurfaceKernel())

    result = handler._capability_compile_graph("coding_graph", {"profile_id": "coding"})

    assert result["surface_launch_target"]["pack_id"] == "frontendpack"
    assert result["surface_launch_target"]["node_id"] == "frontendpack.web_surface"


def test_draft_graph_validation_error_returns_400_diagnostics() -> None:
    handler = _handler(kernel=FakeKernel())

    result = handler._capability_validate_draft_graph({"graph_id": "bad id"}, None)

    assert result["status_code"] == 400
    assert result["ok"] is False
    assert result["diagnostics"][0]["code"] == "invalid_graph"


def test_graph_save_validation_error_returns_400_diagnostics() -> None:
    handler = _handler(kernel=FakeKernel())

    result = handler._capability_save_graph({"graph_id": "bad id"}, create=True)

    assert result["status_code"] == 400
    assert result["ok"] is False
    assert result["diagnostics"][0]["code"] == "invalid_graph"


def test_core_control_panel_registers_capability_graph_api_routes() -> None:
    path = (
        Path(__file__).resolve().parent.parent
        / "core_runtime"
        / "core_pack"
        / "core_control_panel"
        / "ecosystem.json"
    )
    routes = json.loads(path.read_text(encoding="utf-8"))["api_routes"]
    exact_paths = {(route["method"], route.get("path")) for route in routes}
    pattern_paths = {(route["method"], route.get("path_pattern")) for route in routes}

    assert ("GET", "/api/nodes") in exact_paths
    assert ("GET", "/api/panel/profiles") in exact_paths
    assert ("GET", "/api/profiles/{id}/nodes") in pattern_paths
    assert ("POST", "/api/panel/startup/profiles/{id}/compile-preview") in pattern_paths
    assert ("POST", "/api/graphs/{id}/compile") in pattern_paths
