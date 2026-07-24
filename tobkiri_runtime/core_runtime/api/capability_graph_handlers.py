"""Capability Graph API handlers for viewer and external clients."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Optional

from ._helpers import _log_internal_error, _SAFE_ERROR_MSG


def _label(value: Any, fallback: str, locale: str = "en") -> str:
    if isinstance(value, dict):
        return (
            str(value.get(locale) or value.get("en") or fallback)
        )
    if isinstance(value, str) and value:
        return value
    return fallback


def _status_from_kernel_result(result: Dict[str, Any]) -> bool:
    return result.get("_kernel_step_status") == "success"


class CapabilityGraphHandlersMixin:
    """HTTP handlers that expose Capability Graph registries to the viewer."""

    def _capability_kernel(self) -> Any:
        kernel = getattr(self.__class__, "kernel", None) or getattr(self, "kernel", None)
        if kernel is None:
            return None
        return kernel

    def _capability_ctx(self, kernel: Any) -> Dict[str, Any]:
        ctx = getattr(kernel, "_startup_ctx", None)
        if not isinstance(ctx, dict):
            ctx = {}
            try:
                setattr(kernel, "_startup_ctx", ctx)
            except Exception:
                pass
        return ctx

    def _capability_registry_context(self) -> Dict[str, Any]:
        kernel = self._capability_kernel()
        ctx = self._capability_ctx(kernel)
        lifecycle = getattr(kernel, "lifecycle", None)
        return {
            "interface_registry": ctx.get("interface_registry") or getattr(kernel, "interface_registry", None),
            "approval_manager": (
                ctx.get("approval_manager")
                or getattr(self, "approval_manager", None)
                or getattr(kernel, "approval_manager", None)
            ),
            "registry": ctx.get("registry") or getattr(lifecycle, "registry", None),
            "ecosystem_dir": ctx.get("ecosystem_dir") or getattr(kernel, "ecosystem_dir", None),
        }

    def _capability_invalid_graph_response(self, exc: Exception) -> Dict[str, Any]:
        return {
            "ok": False,
            "status_code": 400,
            "diagnostics": [
                {
                    "level": "error",
                    "code": "invalid_graph",
                    "message": str(exc),
                }
            ],
        }

    def _call_kernel_handler(self, handler_id: str, args: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        kernel = self._capability_kernel()
        if kernel is None:
            return {
                "_kernel_step_status": "failed",
                "_kernel_step_meta": {"error": "Kernel not initialized"},
            }
        handler = None
        resolver = getattr(kernel, "_resolve_handler", None)
        if callable(resolver):
            handler = resolver(handler_id)
        if handler is None:
            handler = getattr(kernel, "_kernel_handlers", {}).get(handler_id)
        if handler is None:
            return {
                "_kernel_step_status": "failed",
                "_kernel_step_meta": {"error": f"Kernel handler '{handler_id}' not found"},
            }
        return handler(dict(args or {}), self._capability_ctx(kernel))

    def _capability_startup_relationship(self) -> Dict[str, Any]:
        return {
            "launch_time_source_of_truth": "StartupProfileManager",
            "capability_graph_profiles_role": "graph_runtime_presets",
            "bridge_policy": (
                "Capability Graph profiles do not supersede startup profiles. "
                "They are exposed as graph/runtime presets until an explicit "
                "migration or bridge writes startup-profile state."
            ),
            "startup_profile_api": "/api/panel/startup/profiles",
        }

    def _capability_public_port(self, port: Dict[str, Any], *, locale: str) -> Dict[str, Any]:
        port_id = str(port.get("id") or "")
        return {
            **port,
            "label": _label(port.get("display_name"), port_id, locale),
            "standards": list(port.get("standards") or []),
            "aliases": list(port.get("aliases") or []),
        }

    def _capability_public_node(
        self,
        node: Dict[str, Any],
        *,
        locale: str,
        state_by_id: Optional[Dict[str, Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        node_id = str(node.get("node_id") or "")
        public = {
            **node,
            "label": _label(node.get("display_name"), node_id, locale),
            "description_label": _label(node.get("description"), "", locale),
            "ports": [
                self._capability_public_port(port, locale=locale)
                for port in node.get("ports") or []
                if isinstance(port, dict)
            ],
            "metadata": dict(node.get("metadata") or {}),
            "bindings": dict(node.get("bindings") or {}),
        }
        if state_by_id is not None:
            public["state"] = state_by_id.get(node_id)
        return public

    def _capability_public_profile(self, profile: Dict[str, Any], *, locale: str) -> Dict[str, Any]:
        profile_id = str(profile.get("profile_id") or "")
        profile_locale = str(profile.get("locale") or locale)
        return {
            **profile,
            "label": _label(profile.get("display_name"), profile_id, profile_locale),
            "description_label": _label(profile.get("description"), "", profile_locale),
        }

    def _capability_public_graph(self, graph: Dict[str, Any], *, locale: str) -> Dict[str, Any]:
        graph_id = str(graph.get("graph_id") or "")
        return {
            **graph,
            "label": _label(graph.get("display_name"), graph_id, locale),
            "description_label": _label(graph.get("description"), "", locale),
        }

    def _capability_get_nodes(self) -> Dict[str, Any]:
        """GET /api/nodes and /api/panel/nodes."""
        try:
            result = self._call_kernel_handler("kernel:node.list")
            if not _status_from_kernel_result(result):
                return {
                    "error": result.get("_kernel_step_meta", {}).get("error", _SAFE_ERROR_MSG),
                    "status_code": 500,
                }
            nodes = [
                self._capability_public_node(node, locale="en")
                for node in result.get("nodes", [])
                if isinstance(node, dict)
            ]
            return {
                "nodes": nodes,
                "count": len(nodes),
            }
        except Exception as exc:
            _log_internal_error("capability_get_nodes", exc)
            return {"error": _SAFE_ERROR_MSG, "status_code": 500}

    def _capability_get_node(self, node_id: str) -> Dict[str, Any]:
        """GET /api/nodes/{node_id} and /api/panel/nodes/{node_id}."""
        try:
            result = self._call_kernel_handler("kernel:node.get", {"node_id": node_id})
            if not _status_from_kernel_result(result) or result.get("node") is None:
                return {"error": f"Node '{node_id}' not found", "status_code": 404}
            return {
                "node": self._capability_public_node(result["node"], locale="en"),
            }
        except Exception as exc:
            _log_internal_error("capability_get_node", exc)
            return {"error": _SAFE_ERROR_MSG, "status_code": 500}

    def _capability_get_profiles(self) -> Dict[str, Any]:
        """GET /api/profiles and /api/panel/profiles."""
        try:
            result = self._call_kernel_handler(
                "kernel:profile.list",
                {"isolate_invalid_profiles": True},
            )
            if not _status_from_kernel_result(result):
                return {
                    "error": result.get("_kernel_step_meta", {}).get("error", _SAFE_ERROR_MSG),
                    "status_code": 500,
                }
            profiles = [
                self._capability_public_profile(profile, locale="en")
                for profile in result.get("profiles", [])
                if isinstance(profile, dict)
            ]
            return {
                "profiles": profiles,
                "count": len(profiles),
                "startup_profile_relationship": self._capability_startup_relationship(),
            }
        except Exception as exc:
            _log_internal_error("capability_get_profiles", exc)
            return {"error": _SAFE_ERROR_MSG, "status_code": 500}

    def _capability_get_profile(self, profile_id: str) -> Dict[str, Any]:
        """GET /api/profiles/{profile_id} and /api/panel/profiles/{profile_id}."""
        try:
            result = self._call_kernel_handler(
                "kernel:profile.get",
                {"profile_id": profile_id, "isolate_invalid_profiles": True},
            )
            if not _status_from_kernel_result(result) or result.get("profile") is None:
                return {"error": f"Profile '{profile_id}' not found", "status_code": 404}
            profile = self._capability_public_profile(result["profile"], locale="en")
            return {
                "profile": profile,
                "startup_profile_relationship": self._capability_startup_relationship(),
            }
        except Exception as exc:
            _log_internal_error("capability_get_profile", exc)
            return {"error": _SAFE_ERROR_MSG, "status_code": 500}

    def _capability_get_profile_nodes(self, profile_id: str) -> Dict[str, Any]:
        """GET /api/profiles/{profile_id}/nodes and panel alias."""
        try:
            profile_result = self._call_kernel_handler(
                "kernel:profile.get",
                {"profile_id": profile_id, "isolate_invalid_profiles": True},
            )
            if (
                not _status_from_kernel_result(profile_result)
                or profile_result.get("profile") is None
            ):
                return {"error": f"Profile '{profile_id}' not found", "status_code": 404}
            profile = profile_result["profile"]
            locale = str(profile.get("locale") or "en")

            nodes_result = self._call_kernel_handler("kernel:node.list")
            if not _status_from_kernel_result(nodes_result):
                return {
                    "error": nodes_result.get("_kernel_step_meta", {}).get(
                        "error", _SAFE_ERROR_MSG
                    ),
                    "status_code": 500,
                }
            state_result = self._call_kernel_handler(
                "kernel:profile.node_state",
                {"profile_id": profile_id, "isolate_invalid_profiles": True},
            )
            raw_nodes = [
                node for node in nodes_result.get("nodes", [])
                if isinstance(node, dict)
            ]
            states = self._capability_profile_node_states(
                profile,
                raw_nodes,
                state_result.get("node_state")
                if _status_from_kernel_result(state_result)
                else None,
            )
            state_by_id = {str(item.get("node_id")): item for item in states}
            nodes = [
                self._capability_public_node(node, locale=locale, state_by_id=state_by_id)
                for node in raw_nodes
            ]
            palette_nodes = [
                node for node in nodes
                if (node.get("state") or {}).get("enabled") is True
                and (node.get("state") or {}).get("installed") is True
            ]
            return {
                "profile": self._capability_public_profile(profile, locale=locale),
                "nodes": nodes,
                "node_state": states,
                "palette_nodes": palette_nodes,
                "count": len(nodes),
                "palette_count": len(palette_nodes),
            }
        except Exception as exc:
            _log_internal_error("capability_get_profile_nodes", exc)
            return {"error": _SAFE_ERROR_MSG, "status_code": 500}

    def _capability_profile_node_states(
        self,
        profile: Dict[str, Any],
        nodes: list[Dict[str, Any]],
        kernel_states: Any,
    ) -> list[Dict[str, Any]]:
        """Return profile-scoped Node state, with a read-only safe fallback.

        The catalog must remain usable when an unrelated legacy profile file
        prevents the mutable profile registry from being constructed.  The
        fallback mirrors ``ProfileDefinition.is_node_enabled`` and does not
        grant or mutate any capability.
        """
        if isinstance(kernel_states, list):
            return [item for item in kernel_states if isinstance(item, dict)]

        enabled = {
            str(item) for item in profile.get("enabled_nodes") or [] if item
        }
        disabled = {
            str(item) for item in profile.get("disabled_nodes") or [] if item
        }
        settings = profile.get("node_settings")
        settings = settings if isinstance(settings, dict) else {}
        profile_id = str(profile.get("profile_id") or "")
        states: list[Dict[str, Any]] = []
        for node in nodes:
            node_id = str(node.get("node_id") or "")
            node_settings = settings.get(node_id)
            node_settings = (
                node_settings if isinstance(node_settings, dict) else {}
            )
            requirements = node.get("requirements")
            requirements = requirements if isinstance(requirements, dict) else {}
            required = (
                requirements.get("required_settings")
                or requirements.get("settings")
                or []
            )
            required = required if isinstance(required, list) else []
            missing = [
                key
                for key in required
                if isinstance(key, str)
                and key
                and node_settings.get(key) in (None, "")
            ]
            node_enabled = (
                node_id not in disabled
                and (not enabled or node_id in enabled or node_id == "rumi.start")
            )
            configured = not missing
            states.append(
                {
                    "node_id": node_id,
                    "installed": True,
                    "approved": True,
                    "enabled": node_enabled,
                    "configured": configured,
                    "status": (
                        "disabled"
                        if not node_enabled
                        else "ready" if configured else "missing_config"
                    ),
                    "missing": missing,
                    "credential_ref": node_settings.get("credential_ref"),
                    "profile_id": profile_id,
                }
            )
        return states

    def _capability_get_graphs(self) -> Dict[str, Any]:
        """GET /api/graphs and /api/panel/graphs."""
        try:
            result = self._call_kernel_handler("kernel:graph.load_all")
            if not _status_from_kernel_result(result):
                return {
                    "error": result.get("_kernel_step_meta", {}).get("error", _SAFE_ERROR_MSG),
                    "status_code": 500,
                }
            graphs = [
                self._capability_public_graph(graph, locale="en")
                for graph in result.get("graphs", [])
                if isinstance(graph, dict)
            ]
            return {
                "graphs": graphs,
                "count": len(graphs),
                "diagnostics": list(result.get("diagnostics") or []),
            }
        except Exception as exc:
            _log_internal_error("capability_get_graphs", exc)
            return {"error": _SAFE_ERROR_MSG, "status_code": 500}

    def _capability_get_graph(self, graph_id: str) -> Dict[str, Any]:
        """GET /api/graphs/{graph_id} and /api/panel/graphs/{graph_id}."""
        try:
            result = self._call_kernel_handler("kernel:graph.get", {"graph_id": graph_id})
            if not _status_from_kernel_result(result) or result.get("graph") is None:
                return {"error": f"Graph '{graph_id}' not found", "status_code": 404}
            return {
                "graph": self._capability_public_graph(result["graph"], locale="en"),
            }
        except Exception as exc:
            _log_internal_error("capability_get_graph", exc)
            return {"error": _SAFE_ERROR_MSG, "status_code": 500}

    def _capability_validate_graph(self, graph_id: str, body: Dict[str, Any]) -> Dict[str, Any]:
        """POST /api/graphs/{graph_id}/validate and panel alias."""
        try:
            if isinstance(body.get("graph"), dict):
                result = self._capability_validate_draft_graph(body["graph"], body.get("profile_id"))
                result["graph_id"] = graph_id
                return result
            result = self._call_kernel_handler(
                "kernel:graph.validate",
                {"graph_id": graph_id, "profile_id": body.get("profile_id")},
            )
            return {
                "ok": bool(result.get("ok")),
                "graph_id": graph_id,
                "profile_id": body.get("profile_id"),
                "diagnostics": list(result.get("diagnostics") or []),
            }
        except Exception as exc:
            _log_internal_error("capability_validate_graph", exc)
            return {"error": _SAFE_ERROR_MSG, "status_code": 500}

    def _capability_compile_graph(self, graph_id: str, body: Dict[str, Any]) -> Dict[str, Any]:
        """POST /api/graphs/{graph_id}/compile and panel alias."""
        profile_id = body.get("profile_id")
        if not profile_id:
            return {"error": "profile_id is required", "status_code": 400}
        try:
            if isinstance(body.get("graph"), dict):
                result = self._capability_compile_draft_graph(
                    body["graph"],
                    profile_id,
                    register=bool(body.get("register", False)),
                )
                result["graph_id"] = graph_id
                return result
            result = self._call_kernel_handler(
                "kernel:graph.compile",
                {
                    "graph_id": graph_id,
                    "profile_id": profile_id,
                    "register": bool(body.get("register", False)),
                },
            )
            runtime_profile = result.get("runtime_profile")
            return {
                "ok": bool(result.get("ok")),
                "graph_id": graph_id,
                "profile_id": profile_id,
                "runtime_profile": runtime_profile,
                "surface_launch_target": _surface_launch_target_for_runtime_profile(runtime_profile),
                "diagnostics": list(result.get("diagnostics") or []),
            }
        except Exception as exc:
            _log_internal_error("capability_compile_graph", exc)
            return {"error": _SAFE_ERROR_MSG, "status_code": 500}

    def _capability_create_graph(self, body: Dict[str, Any]) -> Dict[str, Any]:
        """POST /api/panel/graphs."""
        graph_data = body.get("graph") if isinstance(body.get("graph"), dict) else body
        return self._capability_save_graph(graph_data, create=True)

    def _capability_update_graph(self, graph_id: str, body: Dict[str, Any]) -> Dict[str, Any]:
        """PUT /api/panel/graphs/{graph_id}."""
        graph_data = body.get("graph") if isinstance(body.get("graph"), dict) else body
        if isinstance(graph_data, dict):
            graph_data = {**graph_data, "graph_id": graph_id}
        return self._capability_save_graph(graph_data, create=False)

    def _capability_edge_compatibility(self, body: Dict[str, Any]) -> Dict[str, Any]:
        """POST /api/panel/graphs/edge-compatibility."""
        try:
            from ..port_standards import can_connect_ports

            source = body.get("source_port")
            target = body.get("target_port")
            if not isinstance(source, dict) or not isinstance(target, dict):
                return {"error": "source_port and target_port are required", "status_code": 400}
            compatible = can_connect_ports(
                str(source.get("direction") or ""),
                source,
                str(target.get("direction") or ""),
                target,
            )
            return {
                "ok": True,
                "compatible": compatible,
                "shared_standards": sorted(
                    set(source.get("standards") or []).intersection(target.get("standards") or [])
                ),
            }
        except Exception as exc:
            _log_internal_error("capability_edge_compatibility", exc)
            return {"error": _SAFE_ERROR_MSG, "status_code": 500}

    def _capability_get_current_runtime_profile(self) -> Dict[str, Any]:
        """GET /api/panel/runtime-profile/current."""
        try:
            ctx = self._capability_ctx(self._capability_kernel())
            from ..runtime_profile_resolver import resolve_runtime_profile_context

            kernel = self._capability_kernel()
            resolved = resolve_runtime_profile_context(
                ctx,
                interface_registry=ctx.get("interface_registry") or getattr(kernel, "interface_registry", None),
            )
            runtime_profile = resolved.get("runtime_profile") or resolved.get("_capability_profile")
            return {
                "runtime_profile_key": resolved.get("_runtime_profile_key"),
                "runtime_profile": runtime_profile,
                "diagnostics": [] if isinstance(runtime_profile, dict) else [
                    {
                        "level": "warning",
                        "code": "runtime_profile_not_found",
                        "message": "No active runtime profile is registered",
                    }
                ],
            }
        except Exception as exc:
            _log_internal_error("capability_get_current_runtime_profile", exc)
            return {"error": _SAFE_ERROR_MSG, "status_code": 500}

    def _capability_get_runtime_profile(self, runtime_profile_key: str) -> Dict[str, Any]:
        """GET /api/panel/runtime-profile/{runtime_profile_key}."""
        try:
            ctx = self._capability_ctx(self._capability_kernel())
            kernel = self._capability_kernel()
            registry = ctx.get("interface_registry") or getattr(kernel, "interface_registry", None)
            value = registry.get(runtime_profile_key) if registry is not None else None
            if not isinstance(value, dict):
                return {"error": f"Runtime profile '{runtime_profile_key}' not found", "status_code": 404}
            return {
                "runtime_profile_key": runtime_profile_key,
                "runtime_profile": value,
                "diagnostics": [],
            }
        except Exception as exc:
            _log_internal_error("capability_get_runtime_profile", exc)
            return {"error": _SAFE_ERROR_MSG, "status_code": 500}

    def _capability_validate_draft_graph(self, graph_data: Dict[str, Any], profile_id: Optional[str]) -> Dict[str, Any]:
        from ..ecosystem_nodes import EcosystemNodeRegistry
        from ..graph_models import GraphValidationError, load_graph_document
        from ..port_standards import validate_graph_ports

        try:
            graph = load_graph_document(graph_data, source_type="draft")
        except GraphValidationError as exc:
            return self._capability_invalid_graph_response(exc)
        profile = None
        if profile_id:
            profile_result = self._call_kernel_handler("kernel:profile.get", {"profile_id": profile_id})
            if _status_from_kernel_result(profile_result):
                profile_dict = profile_result.get("profile")
                if isinstance(profile_dict, dict):
                    from ..profile_models import load_profile_document

                    profile = load_profile_document(profile_dict)
        services = self._capability_registry_context()
        node_registry = EcosystemNodeRegistry(
            registry=services["registry"],
            interface_registry=services["interface_registry"],
            approval_manager=services["approval_manager"],
            ecosystem_dir=services["ecosystem_dir"],
        )
        nodes = node_registry.load_all_nodes(register=True)
        result = validate_graph_ports(graph, nodes=nodes, profile=profile)
        return {
            "ok": result.ok,
            "profile_id": profile_id,
            "diagnostics": list(node_registry.diagnostics) + list(result.diagnostics),
        }

    def _capability_compile_draft_graph(self, graph_data: Dict[str, Any], profile_id: str, *, register: bool) -> Dict[str, Any]:
        from ..capability_binding_registration import register_pack_binding_handlers
        from ..capability_graph_compiler import CapabilityGraphCompiler
        from ..ecosystem_nodes import EcosystemNodeRegistry
        from ..graph_models import GraphValidationError, load_graph_document
        from ..profile_models import load_profile_document

        profile_result = self._call_kernel_handler("kernel:profile.get", {"profile_id": profile_id})
        if not _status_from_kernel_result(profile_result) or not isinstance(profile_result.get("profile"), dict):
            return {"ok": False, "profile_id": profile_id, "runtime_profile": None, "diagnostics": [
                {"level": "error", "code": "profile_not_found", "message": f"Profile '{profile_id}' was not found"}
            ]}
        try:
            graph = load_graph_document(graph_data, source_type="draft")
        except GraphValidationError as exc:
            response = self._capability_invalid_graph_response(exc)
            response.update({"profile_id": profile_id, "runtime_profile": None})
            return response
        profile = load_profile_document(profile_result["profile"])
        services = self._capability_registry_context()
        interface_registry = services["interface_registry"]
        registration = register_pack_binding_handlers(
            interface_registry=interface_registry,
            approval_manager=services["approval_manager"],
            ecosystem_dir=services["ecosystem_dir"],
            registry=services["registry"],
        )
        node_registry = EcosystemNodeRegistry(
            registry=services["registry"],
            interface_registry=interface_registry,
            approval_manager=services["approval_manager"],
            ecosystem_dir=services["ecosystem_dir"],
        )
        nodes = node_registry.load_all_nodes(register=True)
        result = CapabilityGraphCompiler(interface_registry=interface_registry).compile(
            graph,
            profile=profile,
            nodes=nodes,
            register=register,
        )
        return {
            "ok": result.ok,
            "graph_id": graph.graph_id,
            "profile_id": profile_id,
            "runtime_profile": result.runtime_profile,
            "surface_launch_target": _surface_launch_target_for_runtime_profile(result.runtime_profile),
            "diagnostics": list(registration.diagnostics) + list(node_registry.diagnostics) + list(result.diagnostics),
        }

    def _capability_save_graph(self, graph_data: Any, *, create: bool) -> Dict[str, Any]:
        try:
            import yaml  # type: ignore[import-untyped]
            from ..graph_models import GraphValidationError, load_graph_document

            if not isinstance(graph_data, dict):
                return {"error": "graph object is required", "status_code": 400}
            try:
                graph = load_graph_document(graph_data, source_type="user")
            except GraphValidationError as exc:
                return self._capability_invalid_graph_response(exc)
            user_graph_dir = (
                Path(__file__).resolve().parent.parent.parent
                / "user_data"
                / "shared"
                / "graphs"
            )
            user_graph_dir.mkdir(parents=True, exist_ok=True)
            target = user_graph_dir / f"{graph.graph_id}.graph.yaml"
            if create and target.exists():
                return {"error": f"Graph '{graph.graph_id}' already exists", "status_code": 409}
            target.write_text(
                yaml.safe_dump(graph.to_dict(), allow_unicode=True, sort_keys=False),
                encoding="utf-8",
            )
            ctx = self._capability_ctx(self._capability_kernel())
            ctx.pop("graph_loader", None)
            load_result = self._call_kernel_handler("kernel:graph.load_all")
            return {
                "graph": self._capability_public_graph(graph.to_dict(), locale="en"),
                "created": create,
                "path": str(target),
                "diagnostics": list(load_result.get("diagnostics") or []),
            }
        except Exception as exc:
            _log_internal_error("capability_save_graph", exc)
            return {"error": _SAFE_ERROR_MSG, "status_code": 500}

    def _capability_clone_profile(self, profile_id: str, body: Dict[str, Any]) -> Dict[str, Any]:
        """POST /api/panel/profiles/{profile_id}/clone."""
        try:
            import copy
            import yaml  # type: ignore[import-untyped]

            from ..profile_models import load_profile_document

            result = self._call_kernel_handler("kernel:profile.get", {"profile_id": profile_id})
            if not _status_from_kernel_result(result) or result.get("profile") is None:
                return {"error": f"Profile '{profile_id}' not found", "status_code": 404}

            source_profile = result["profile"]
            permissions = source_profile.get("permissions") or {}
            if permissions.get("can_create_profile") is not True:
                return {
                    "error": "Profile cloning is not allowed by this capability profile",
                    "status_code": 403,
                }

            new_profile_id = str(body.get("profile_id") or f"{profile_id}_copy").strip()
            cloned = copy.deepcopy(source_profile)
            cloned["profile_id"] = new_profile_id
            cloned["version"] = "rumi.profile.v1"
            display_name = dict(cloned.get("display_name") or {})
            if body.get("display_name"):
                display_name["en"] = str(body["display_name"])
            elif display_name.get("en"):
                display_name["en"] = f"{display_name['en']} Copy"
            cloned["display_name"] = display_name
            metadata = dict(cloned.get("metadata") or {})
            metadata["source_type"] = "user"
            metadata["cloned_from"] = profile_id
            cloned["metadata"] = metadata

            normalized = load_profile_document(
                cloned,
                source_type="user",
            ).to_dict()
            user_profile_dir = (
                Path(__file__).resolve().parent.parent.parent
                / "user_data"
                / "shared"
                / "profiles"
            )
            user_profile_dir.mkdir(parents=True, exist_ok=True)
            target = user_profile_dir / f"{new_profile_id}.profile.yaml"
            if target.exists():
                return {"error": f"Profile '{new_profile_id}' already exists", "status_code": 409}
            target.write_text(
                yaml.safe_dump(normalized, allow_unicode=True, sort_keys=False),
                encoding="utf-8",
            )

            ctx = self._capability_ctx(self._capability_kernel())
            ctx.pop("profile_loader", None)
            load_result = self._call_kernel_handler("kernel:profile.load_all")
            return {
                "profile": self._capability_public_profile(normalized, locale=str(normalized.get("locale") or "en")),
                "created": True,
                "path": str(target),
                "diagnostics": list(load_result.get("diagnostics") or []),
            }
        except Exception as exc:
            _log_internal_error("capability_clone_profile", exc)
            return {"error": _SAFE_ERROR_MSG, "status_code": 500}


def _surface_launch_target_for_runtime_profile(runtime_profile: Any) -> Optional[Dict[str, Any]]:
    if not isinstance(runtime_profile, dict):
        return None
    try:
        from ..surface_launch_target import extract_surface_launch_target
    except Exception:
        return None
    profile = runtime_profile.get("profile")
    surfaces = profile.get("surfaces") if isinstance(profile, dict) else None
    return extract_surface_launch_target(
        runtime_profile,
        fallback_pack_id=None,
        surfaces=surfaces if isinstance(surfaces, dict) else None,
    )
