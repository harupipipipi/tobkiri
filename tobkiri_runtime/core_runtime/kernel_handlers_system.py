"""
kernel_handlers_system.py - 起動/システム系ハンドラ Mixin

Kernelの _h_* メソッドのうち起動・初期化・システム系を提供する。
Mixin方式でKernelクラスに合成される。

含まれるハンドラ:
- mounts/registry/active_ecosystem/interfaces
- security/approval/docker/container/privilege/api
- component discover/load
- ctx.set/get/copy, ir.get/call/register
- exec_python, execute_flow, save_flow, load_flows
- flow.compose
- emit, startup.failed, vocab.load, noop
"""

from __future__ import annotations

import json
import importlib
import os
import subprocess
from pathlib import Path
from typing import Any, Dict, List

from .logging_utils import get_structured_logger
from .metrics import get_metrics_collector
from .runtime_port import resolve_runtime_port
from .paths import BASE_DIR, CORE_PACK_DIR

_logger = get_structured_logger("rumi.kernel.handlers.system")


def _resolve_api_port(args: Dict[str, Any]) -> int:
    return resolve_runtime_port(fallback=args.get("port"))

# ------------------------------------------------------------------
# Wave 17-A: inject ブロックリスト — 内部サービス参照の注入を禁止
# ------------------------------------------------------------------
_EXEC_PYTHON_ALLOWED_ROOTS = (Path(CORE_PACK_DIR).resolve(),)


def _path_relative_to(child: Path, parent: Path) -> bool:
    try:
        child.relative_to(parent)
        return True
    except ValueError:
        return False


def _blocked_exec_python(reason: str, **meta: Any) -> Dict[str, Any]:
    details = {"reason": reason, **meta}
    return {
        "error": reason,
        "status": "blocked",
        "_kernel_step_status": "failed",
        "_kernel_step_meta": details,
    }


_INJECT_BLOCKED_KEYS = frozenset({
    "interface_registry",
    "event_bus",
    "diagnostics",
    "install_journal",
    "permission_manager",
    "approval_manager",
    "lifecycle",
    "active_ecosystem",
    "registry",
})


def initialize_approval_manager():
    from .approval_manager import initialize_approval_manager as _initialize_approval_manager

    return _initialize_approval_manager()


def get_approval_manager():
    from .approval_manager import get_approval_manager as _get_approval_manager

    return _get_approval_manager()


def get_registry():
    from backend_core.ecosystem.registry import get_registry as _get_registry

    return _get_registry()


def _failed_step_result(handler: str, error: Exception | str, **meta: Any) -> Dict[str, Any]:
    error_text = str(error)
    step_meta = {"error": error_text, "handler": handler}
    if meta:
        step_meta.update(meta)
    return {
        "_kernel_step_status": "failed",
        "_kernel_step_error": error_text,
        "_kernel_step_meta": step_meta,
    }




class KernelSystemHandlersMixin:
    """
    起動/システム系ハンドラ Mixin

    __init__ を持たない。self の属性（diagnostics, interface_registry 等）は
    KernelCore.__init__ で初期化済みの前提でアクセスする。
    """

    # ------------------------------------------------------------------
    # ハンドラ登録（Kernel._init_kernel_handlers から呼ばれる）
    # ------------------------------------------------------------------

    def _register_system_handlers(self) -> Dict[str, Any]:
        """システム系ハンドラの辞書を返す"""
        return {
            "kernel:mounts.init": self._h_mounts_init,
            "kernel:registry.load": self._h_registry_load,
            "kernel:active_ecosystem.load": self._h_active_ecosystem_load,
            "kernel:interfaces.publish": self._h_interfaces_publish,
            "kernel:ir.get": self._h_ir_get,
            "kernel:ir.call": self._h_ir_call,
            "kernel:ir.register": self._h_ir_register,
            "kernel:node.load_all": self._h_node_load_all,
            "kernel:node.list": self._h_node_list,
            "kernel:node.get": self._h_node_get,
            "kernel:profile.load_all": self._h_profile_load_all,
            "kernel:profile.list": self._h_profile_list,
            "kernel:profile.get": self._h_profile_get,
            "kernel:profile.node_state": self._h_profile_node_state,
            "kernel:graph.load_all": self._h_graph_load_all,
            "kernel:graph.get": self._h_graph_get,
            "kernel:graph.validate": self._h_graph_validate,
            "kernel:graph.compile": self._h_graph_compile,
            "kernel:exec_python": self._h_exec_python,
            "kernel:ctx.set": self._h_ctx_set,
            "kernel:ctx.get": self._h_ctx_get,
            "kernel:ctx.copy": self._h_ctx_copy,
            "kernel:execute_flow": self._h_execute_flow,
            "kernel:save_flow": self._h_save_flow,
            "kernel:load_flows": self._h_load_flows,
            "kernel:flow.compose": self._h_flow_compose,
            "kernel:security.init": self._h_security_init,
            "kernel:docker.check": self._h_docker_check,
            "kernel:approval.init": self._h_approval_init,
            "kernel:approval.scan": self._h_approval_scan,
            "kernel:container.init": self._h_container_init,
            "kernel:privilege.init": self._h_privilege_init,
            "kernel:api.init": self._h_api_init,
            "kernel:container.start_approved": self._h_container_start_approved,
            "kernel:component.discover": self._h_component_discover,
            "kernel:defaults.compat.build": self._h_defaults_compat_build,
            "kernel:component.load": self._h_component_load,
            "kernel:emit": self._h_emit,
            "kernel:startup.failed": self._h_startup_failed,
            "kernel:vocab.load": self._h_vocab_load,
            "kernel:noop": self._h_noop,
            "kernel:register_kernel_functions": self._h_register_kernel_functions,
        }

    # ------------------------------------------------------------------
    # mounts / registry / active_ecosystem / interfaces
    # ------------------------------------------------------------------

    def _h_mounts_init(self, args: Dict[str, Any], ctx: Dict[str, Any]) -> Any:
        mounts_file = str(args.get("mounts_file", "user_data/mounts.json"))
        try:
            from backend_core.ecosystem.mounts import DEFAULT_MOUNTS, initialize_mounts, get_mount_manager
            mf = Path(mounts_file)
            if not mf.exists():
                mf.parent.mkdir(parents=True, exist_ok=True)
                mf.write_text(json.dumps({"version": "1.0", "mounts": DEFAULT_MOUNTS}, ensure_ascii=False, indent=2), encoding="utf-8")
            initialize_mounts(config_path=str(mf))
            mm = get_mount_manager()
            ctx["mount_manager"] = mm
            self.interface_registry.register("ecosystem.mount_manager", mm, meta={"source": "kernel"})
            return mm
        except Exception as e:
            self.diagnostics.record_step(phase="startup", step_id="startup.mounts.internal", handler="kernel:mounts.init",
                                          status="failed", error=e, meta={"mounts_file": mounts_file})
            _logger.error("Mounts init failed", exc_info=e, mounts_file=mounts_file)
            return _failed_step_result("kernel:mounts.init", e, mounts_file=mounts_file)

    def _h_registry_load(self, args: Dict[str, Any], ctx: Dict[str, Any]) -> Any:
        ecosystem_dir = str(args.get("ecosystem_dir", "ecosystem"))
        try:
            regmod = importlib.import_module("backend_core.ecosystem.registry")
            Registry = getattr(regmod, "Registry")
            reg = Registry(ecosystem_dir=ecosystem_dir)
            reg.load_all_packs()
            regmod._global_registry = reg
            ctx["registry"] = reg
            self.lifecycle.registry = reg
            self.interface_registry.register("ecosystem.registry", reg, meta={"source": "kernel"})
            return reg
        except Exception as e:
            self.diagnostics.record_step(phase="startup", step_id="startup.registry.internal", handler="kernel:registry.load",
                                          status="failed", error=e, meta={"ecosystem_dir": ecosystem_dir})
            _logger.error("Registry load failed", exc_info=e, ecosystem_dir=ecosystem_dir)
            return _failed_step_result("kernel:registry.load", e, ecosystem_dir=ecosystem_dir)

    def _h_active_ecosystem_load(self, args: Dict[str, Any], ctx: Dict[str, Any]) -> Any:
        config_file = str(args.get("config_file", "user_data/active_ecosystem.json"))
        user_data_dir = os.environ.get("RUMI_USER_DATA")
        config_path = Path(config_file)
        if user_data_dir and not config_path.is_absolute() and config_path.parts[:1] == ("user_data",):
            config_file = str(Path(user_data_dir).joinpath(*config_path.parts[1:]))
        try:
            import backend_core.ecosystem.active_ecosystem as amod
            from backend_core.ecosystem.active_ecosystem import ActiveEcosystemManager
            mgr = ActiveEcosystemManager(config_path=config_file)
            amod._global_manager = mgr
            ctx["active_ecosystem"] = mgr
            self.lifecycle.active_ecosystem = mgr
            self.interface_registry.register("ecosystem.active_ecosystem", mgr, meta={"source": "kernel"})
            return mgr
        except Exception as e:
            self.diagnostics.record_step(phase="startup", step_id="startup.active_ecosystem.internal", handler="kernel:active_ecosystem.load",
                                          status="failed", error=e, meta={"config_file": config_file})
            _logger.error("Active ecosystem load failed", exc_info=e, config_file=config_file)
            return _failed_step_result("kernel:active_ecosystem.load", e, config_file=config_file)

    def _h_interfaces_publish(self, args: Dict[str, Any], ctx: Dict[str, Any]) -> Any:
        self.interface_registry.register("kernel.state", {"services_ready": True, "ts": self._now_ts()}, meta={"source": "kernel"})
        return {"services_ready": True}

    # ------------------------------------------------------------------
    # IR (Interface Registry) ハンドラ
    # ------------------------------------------------------------------

    def _h_ir_get(self, args: Dict[str, Any], ctx: Dict[str, Any]) -> Any:
        key = args.get("key")
        if not key:
            return {"_kernel_step_status": "failed", "_kernel_step_meta": {"error": "missing 'key' argument"}}
        strategy = args.get("strategy", "last")
        value = self.interface_registry.get(key, strategy=strategy)
        if args.get("store_as"):
            ctx[args["store_as"]] = value
        # BUG-20260306-03 fix: strategy-aware な found 判定
        # strategy="all" の場合 [] を返すことがあるため bool(value) で判定
        found = bool(value) if strategy == "all" else value is not None
        return {"_kernel_step_status": "success", "_kernel_step_meta": {"key": key, "strategy": strategy, "found": found}, "value": value}

    def _h_ir_call(self, args: Dict[str, Any], ctx: Dict[str, Any]) -> Any:
        key = args.get("key")
        if not key:
            return {"_kernel_step_status": "failed", "_kernel_step_meta": {"error": "missing 'key' argument"}}
        fn = self.interface_registry.get(key, strategy=args.get("strategy", "last"))
        if fn is None:
            return {"_kernel_step_status": "skipped", "_kernel_step_meta": {"reason": "not_found", "key": key}}
        if not callable(fn):
            return {"_kernel_step_status": "skipped", "_kernel_step_meta": {"reason": "not_callable", "key": key}}
        resolved_args = self._resolve_args(args.get("call_args", {}), ctx)
        try:
            result = fn(ctx) if args.get("pass_ctx", False) else (fn(**resolved_args) if resolved_args else fn())
        except TypeError:
            try:
                result = fn(ctx)
            except Exception as e:
                return {"_kernel_step_status": "failed", "_kernel_step_meta": {"error": str(e), "key": key}}
        except Exception as e:
            return {"_kernel_step_status": "failed", "_kernel_step_meta": {"error": str(e), "key": key}}
        if args.get("store_as"):
            ctx[args["store_as"]] = result
        return {"_kernel_step_status": "success", "_kernel_step_meta": {"key": key, "has_result": result is not None}, "result": result}

    def _h_ir_register(self, args: Dict[str, Any], ctx: Dict[str, Any]) -> Any:
        key = args.get("key")
        if not key:
            return {"_kernel_step_status": "failed", "_kernel_step_meta": {"error": "missing 'key' argument"}}
        value = ctx.get(args["value_from_ctx"]) if args.get("value_from_ctx") else (self._resolve_value(args.get("value"), ctx) if args.get("value") is not None else None)
        self.interface_registry.register(key, value, meta=args.get("meta", {}))
        return {"_kernel_step_status": "success", "_kernel_step_meta": {"key": key, "has_value": value is not None}}

    # ------------------------------------------------------------------
    # Capability Graph node registry handlers
    # ------------------------------------------------------------------

    def _get_node_registry(self: Any, args: Dict[str, Any], ctx: Dict[str, Any]):
        from .ecosystem_nodes import EcosystemNodeRegistry

        existing = ctx.get("node_registry")
        if existing is not None:
            return existing
        registry = EcosystemNodeRegistry(
            registry=ctx.get("registry") or getattr(self.lifecycle, "registry", None),
            interface_registry=self.interface_registry,
            approval_manager=ctx.get("approval_manager"),
            ecosystem_dir=args.get("ecosystem_dir"),
        )
        ctx["node_registry"] = registry
        self.interface_registry.register(
            "node.registry",
            registry,
            meta={"source": "kernel", "_system": True},
        )
        return registry

    def _h_node_load_all(self: Any, args: Dict[str, Any], ctx: Dict[str, Any]) -> Any:
        try:
            node_registry = self._get_node_registry(args, ctx)
            nodes = node_registry.load_all_nodes(register=True)
            return {
                "_kernel_step_status": "success",
                "_kernel_step_meta": {"count": len(nodes)},
                "nodes": [node.to_dict() for node in nodes.values()],
                "diagnostics": list(node_registry.diagnostics),
            }
        except Exception as e:
            return _failed_step_result("kernel:node.load_all", e)

    def _h_node_list(self: Any, args: Dict[str, Any], ctx: Dict[str, Any]) -> Any:
        try:
            node_registry = self._get_node_registry(args, ctx)
            nodes = node_registry.list_nodes()
            return {
                "_kernel_step_status": "success",
                "_kernel_step_meta": {"count": len(nodes)},
                "nodes": [node.to_dict() for node in nodes],
            }
        except Exception as e:
            return _failed_step_result("kernel:node.list", e)

    def _h_node_get(self: Any, args: Dict[str, Any], ctx: Dict[str, Any]) -> Any:
        node_id = args.get("node_id")
        if not node_id:
            return {"_kernel_step_status": "failed", "_kernel_step_meta": {"error": "missing 'node_id' argument"}}
        try:
            node_registry = self._get_node_registry(args, ctx)
            node = node_registry.get_node(str(node_id))
            return {
                "_kernel_step_status": "success" if node else "failed",
                "_kernel_step_meta": {"node_id": node_id, "found": node is not None},
                "node": node.to_dict() if node else None,
            }
        except Exception as e:
            return _failed_step_result("kernel:node.get", e, node_id=node_id)

    # ------------------------------------------------------------------
    # Capability Graph profile handlers
    # ------------------------------------------------------------------

    def _get_profile_loader(self: Any, args: Dict[str, Any], ctx: Dict[str, Any]):
        from .profile_loader import CapabilityProfileLoader

        existing = ctx.get("profile_loader")
        if existing is not None:
            return existing
        loader = CapabilityProfileLoader(
            registry=ctx.get("registry") or getattr(self.lifecycle, "registry", None),
            interface_registry=self.interface_registry,
            approval_manager=ctx.get("approval_manager"),
            ecosystem_dir=args.get("ecosystem_dir"),
            shared_profiles_dir=args.get("shared_profiles_dir"),
        )
        ctx["profile_loader"] = loader
        self.interface_registry.register(
            "profile.loader",
            loader,
            meta={"source": "kernel", "_system": True},
        )
        return loader

    def _h_profile_load_all(self: Any, args: Dict[str, Any], ctx: Dict[str, Any]) -> Any:
        try:
            loader = self._get_profile_loader(args, ctx)
            profiles = loader.load_all_profiles(register=True)
            return {
                "_kernel_step_status": "success",
                "_kernel_step_meta": {"count": len(profiles)},
                "profiles": [profile.to_dict() for profile in profiles.values()],
                "diagnostics": list(loader.diagnostics),
            }
        except Exception as e:
            return _failed_step_result("kernel:profile.load_all", e)

    def _h_profile_list(self: Any, args: Dict[str, Any], ctx: Dict[str, Any]) -> Any:
        try:
            loader = self._get_profile_loader(args, ctx)
            profiles = loader.list_profiles()
            return {
                "_kernel_step_status": "success",
                "_kernel_step_meta": {"count": len(profiles)},
                "profiles": [profile.to_dict() for profile in profiles],
            }
        except Exception as e:
            return _failed_step_result("kernel:profile.list", e)

    def _h_profile_get(self: Any, args: Dict[str, Any], ctx: Dict[str, Any]) -> Any:
        profile_id = args.get("profile_id")
        if not profile_id:
            return {"_kernel_step_status": "failed", "_kernel_step_meta": {"error": "missing 'profile_id' argument"}}
        try:
            loader = self._get_profile_loader(args, ctx)
            profile = loader.get_profile(str(profile_id))
            return {
                "_kernel_step_status": "success" if profile else "failed",
                "_kernel_step_meta": {"profile_id": profile_id, "found": profile is not None},
                "profile": profile.to_dict() if profile else None,
            }
        except Exception as e:
            return _failed_step_result("kernel:profile.get", e, profile_id=profile_id)

    def _h_profile_node_state(self: Any, args: Dict[str, Any], ctx: Dict[str, Any]) -> Any:
        profile_id = args.get("profile_id")
        if not profile_id:
            return {"_kernel_step_status": "failed", "_kernel_step_meta": {"error": "missing 'profile_id' argument"}}
        try:
            from .profile_node_registry import ProfileNodeRegistry

            loader = self._get_profile_loader(args, ctx)
            profile = loader.get_profile(str(profile_id))
            if profile is None:
                return {
                    "_kernel_step_status": "failed",
                    "_kernel_step_meta": {"profile_id": profile_id, "found": False},
                    "node_state": None,
                }
            profile_nodes = ProfileNodeRegistry(
                node_registry=self._get_node_registry(args, ctx),
                profile=profile,
            )
            node_id = args.get("node_id")
            state = profile_nodes.node_state(str(node_id)) if node_id else profile_nodes.node_state()
            return {
                "_kernel_step_status": "success",
                "_kernel_step_meta": {"profile_id": profile_id, "node_id": node_id},
                "node_state": state,
            }
        except Exception as e:
            return _failed_step_result("kernel:profile.node_state", e, profile_id=profile_id)

    # ------------------------------------------------------------------
    # Capability Graph graph handlers
    # ------------------------------------------------------------------

    def _get_graph_loader(self: Any, args: Dict[str, Any], ctx: Dict[str, Any]):
        from .capability_graph_loader import CapabilityGraphLoader

        existing = ctx.get("graph_loader")
        if existing is not None:
            return existing
        loader = CapabilityGraphLoader(
            registry=ctx.get("registry") or getattr(self.lifecycle, "registry", None),
            interface_registry=self.interface_registry,
            approval_manager=ctx.get("approval_manager"),
            ecosystem_dir=args.get("ecosystem_dir"),
            shared_graphs_dir=args.get("shared_graphs_dir"),
            workspace_graphs_dir=args.get("workspace_graphs_dir"),
        )
        ctx["graph_loader"] = loader
        self.interface_registry.register(
            "graph.loader",
            loader,
            meta={"source": "kernel", "_system": True},
        )
        return loader

    def _h_graph_load_all(self: Any, args: Dict[str, Any], ctx: Dict[str, Any]) -> Any:
        try:
            loader = self._get_graph_loader(args, ctx)
            graphs = loader.load_all_graphs(register=True)
            return {
                "_kernel_step_status": "success",
                "_kernel_step_meta": {"count": len(graphs)},
                "graphs": [graph.to_dict() for graph in graphs.values()],
                "diagnostics": list(loader.diagnostics),
            }
        except Exception as e:
            return _failed_step_result("kernel:graph.load_all", e)

    def _h_graph_get(self: Any, args: Dict[str, Any], ctx: Dict[str, Any]) -> Any:
        graph_id = args.get("graph_id")
        if not graph_id:
            return {"_kernel_step_status": "failed", "_kernel_step_meta": {"error": "missing 'graph_id' argument"}}
        try:
            loader = self._get_graph_loader(args, ctx)
            graph = loader.get_graph(str(graph_id))
            return {
                "_kernel_step_status": "success" if graph else "failed",
                "_kernel_step_meta": {"graph_id": graph_id, "found": graph is not None},
                "graph": graph.to_dict() if graph else None,
            }
        except Exception as e:
            return _failed_step_result("kernel:graph.get", e, graph_id=graph_id)

    def _h_graph_validate(self: Any, args: Dict[str, Any], ctx: Dict[str, Any]) -> Any:
        graph_id = args.get("graph_id")
        if not graph_id:
            return {"_kernel_step_status": "failed", "_kernel_step_meta": {"error": "missing 'graph_id' argument"}}
        try:
            profile = None
            profile_id = args.get("profile_id")
            if profile_id:
                profile_loader = self._get_profile_loader(args, ctx)
                profile = profile_loader.get_profile(str(profile_id))
                if profile is None:
                    return {
                        "_kernel_step_status": "failed",
                        "_kernel_step_meta": {"graph_id": graph_id, "profile_id": profile_id, "profile_found": False},
                        "ok": False,
                        "diagnostics": [
                            {
                                "level": "error",
                                "code": "profile_not_found",
                                "message": f"Profile '{profile_id}' was not found",
                                "profile_id": profile_id,
                            }
                        ],
                    }
            loader = self._get_graph_loader(args, ctx)
            result = loader.validate_graph(
                str(graph_id),
                node_registry=self._get_node_registry(args, ctx),
                profile=profile,
            )
            return {
                "_kernel_step_status": "success" if result.ok else "failed",
                "_kernel_step_meta": {"graph_id": graph_id, "profile_id": profile_id},
                "ok": result.ok,
                "diagnostics": list(result.diagnostics),
            }
        except Exception as e:
            return _failed_step_result("kernel:graph.validate", e, graph_id=graph_id)

    def _h_graph_compile(self: Any, args: Dict[str, Any], ctx: Dict[str, Any]) -> Any:
        graph_id = args.get("graph_id")
        profile_id = args.get("profile_id")
        if not graph_id:
            return {"_kernel_step_status": "failed", "_kernel_step_meta": {"error": "missing 'graph_id' argument"}}
        if not profile_id:
            return {"_kernel_step_status": "failed", "_kernel_step_meta": {"error": "missing 'profile_id' argument"}}
        try:
            from .capability_graph_compiler import CapabilityGraphCompiler

            profile_loader = self._get_profile_loader(args, ctx)
            profile = profile_loader.get_profile(str(profile_id))
            if profile is None:
                return {
                    "_kernel_step_status": "failed",
                    "_kernel_step_meta": {"graph_id": graph_id, "profile_id": profile_id, "profile_found": False},
                    "ok": False,
                    "runtime_profile": None,
                    "diagnostics": [
                        {
                            "level": "error",
                            "code": "profile_not_found",
                            "message": f"Profile '{profile_id}' was not found",
                            "profile_id": profile_id,
                        }
                    ],
                }

            graph_loader = self._get_graph_loader(args, ctx)
            graph = graph_loader.get_graph(str(graph_id))
            if graph is None:
                return {
                    "_kernel_step_status": "failed",
                    "_kernel_step_meta": {"graph_id": graph_id, "profile_id": profile_id, "graph_found": False},
                    "ok": False,
                    "runtime_profile": None,
                    "diagnostics": [
                        {
                            "level": "error",
                            "code": "graph_not_found",
                            "message": f"Graph '{graph_id}' was not found",
                            "graph_id": graph_id,
                        }
                    ],
                }

            node_registry = self._get_node_registry(args, ctx)
            nodes = getattr(node_registry, "nodes", None)
            if not nodes:
                nodes = node_registry.load_all_nodes(register=True)
            diagnostics: List[Dict[str, Any]] = []
            try:
                from .capability_binding_registration import register_pack_binding_handlers

                registration = register_pack_binding_handlers(
                    interface_registry=self.interface_registry,
                    approval_manager=ctx.get("approval_manager"),
                    ecosystem_dir=args.get("ecosystem_dir"),
                    registry=ctx.get("registry") or getattr(self.lifecycle, "registry", None),
                )
                diagnostics.extend(registration.diagnostics)
            except Exception as exc:
                diagnostics.append(
                    {
                        "level": "warning",
                        "code": "pack_binding_registration_unavailable",
                        "message": f"Pack binding registration is unavailable: {exc}",
                    }
                )
            compiler = CapabilityGraphCompiler(interface_registry=self.interface_registry)
            result = compiler.compile(
                graph,
                profile=profile,
                nodes=dict(nodes),
                register=bool(args.get("register", True)),
            )
            diagnostics.extend(result.diagnostics)
            return {
                "_kernel_step_status": "success" if result.ok else "failed",
                "_kernel_step_meta": {"graph_id": graph_id, "profile_id": profile_id},
                "ok": result.ok,
                "runtime_profile": result.runtime_profile,
                "diagnostics": diagnostics,
            }
        except Exception as e:
            return _failed_step_result("kernel:graph.compile", e, graph_id=graph_id, profile_id=profile_id)

    # ------------------------------------------------------------------
    # ctx ハンドラ
    # ------------------------------------------------------------------

    def _h_ctx_set(self, args: Dict[str, Any], ctx: Dict[str, Any]) -> Any:
        key = args.get("key")
        if not key:
            return {"_kernel_step_status": "failed", "_kernel_step_meta": {"error": "missing 'key' argument"}}
        ctx[key] = self._resolve_value(args.get("value"), ctx)
        return {"_kernel_step_status": "success", "_kernel_step_meta": {"key": key}}

    def _h_ctx_get(self, args: Dict[str, Any], ctx: Dict[str, Any]) -> Any:
        key = args.get("key")
        if not key:
            return {"_kernel_step_status": "failed", "_kernel_step_meta": {"error": "missing 'key' argument"}}
        value = ctx.get(key, args.get("default"))
        if args.get("store_as"):
            ctx[args["store_as"]] = value
        return {"_kernel_step_status": "success", "_kernel_step_meta": {"key": key, "found": key in ctx}, "value": value}

    def _h_ctx_copy(self, args: Dict[str, Any], ctx: Dict[str, Any]) -> Any:
        from_key, to_key = args.get("from_key"), args.get("to_key")
        if not from_key or not to_key:
            return {"_kernel_step_status": "failed", "_kernel_step_meta": {"error": "missing 'from_key' or 'to_key' argument"}}
        ctx[to_key] = ctx.get(from_key)
        return {"_kernel_step_status": "success", "_kernel_step_meta": {"from_key": from_key, "to_key": to_key}}

    # ------------------------------------------------------------------
    # exec_python / execute_flow / save_flow / load_flows
    # ------------------------------------------------------------------

    def _h_exec_python(self, args: Dict[str, Any], ctx: Dict[str, Any]) -> Any:
        file_arg = args.get("file")
        if not file_arg:
            return {"_kernel_step_status": "failed", "_kernel_step_meta": {"error": "missing 'file' argument"}}

        file_path = Path(str(file_arg))
        if file_path.is_absolute():
            _logger.warning(f"Absolute exec_python file path blocked: {file_arg}")
            return _blocked_exec_python("absolute_path_not_allowed", path=str(file_arg))

        base_path = args.get("base_path") or ctx.get("_foreach_current_path", ".")
        base_candidate = Path(str(base_path)) if base_path else Path(".")
        base_root = (base_candidate if base_candidate.is_absolute() else BASE_DIR / base_candidate).resolve()
        full_path = (base_root / file_path).resolve()

        # Flow YAML is declarative configuration, not a general host-code execution
        # authority.  Only relative Python files from the built-in core pack may be
        # executed by this bootstrap primitive; pack code should use the approved
        # component/capability execution paths instead.
        if not _path_relative_to(full_path, base_root):
            _logger.warning(f"Path traversal detected: {file_arg} (base: {base_path})")
            return _blocked_exec_python("Path traversal detected", path=str(file_arg), base_path=str(base_path))
        if not any(_path_relative_to(full_path, root) for root in _EXEC_PYTHON_ALLOWED_ROOTS):
            _logger.warning(f"exec_python target outside trusted roots blocked: {full_path}")
            return _blocked_exec_python(
                "exec_python_target_not_allowed",
                path=str(full_path),
                allowed_roots=[str(root) for root in _EXEC_PYTHON_ALLOWED_ROOTS],
            )
        if full_path.suffix != ".py":
            _logger.warning(f"Non-Python exec_python target blocked: {full_path}")
            return _blocked_exec_python("exec_python_target_not_python", path=str(full_path))
        if not full_path.exists():
            return {"_kernel_step_status": "skipped", "_kernel_step_meta": {"reason": "file_not_found", "path": str(full_path)}}
        phase = args.get("phase", "exec")
        exec_ctx = {"phase": phase, "ts": self._now_ts(), "paths": {"file": str(full_path), "dir": str(full_path.parent), "component_runtime_dir": str(full_path.parent)},
                    "ids": ctx.get("_foreach_ids", {}), "interface_registry": self.interface_registry, "event_bus": self.event_bus,
                    "diagnostics": self.diagnostics, "install_journal": self.install_journal}
        # Wave 17-A: inject ブロックリストで内部サービス参照の注入を制限
        for k, v in args.get("inject", {}).items():
            if k in _INJECT_BLOCKED_KEYS:
                _logger.warning(f"inject blocked for protected key: {k}")
                continue
            exec_ctx[k] = self._resolve_value(v, ctx)
        try:
            self.lifecycle._exec_python_file(full_path, exec_ctx)
            return {"_kernel_step_status": "success", "_kernel_step_meta": {"file": str(full_path), "phase": phase}}
        except Exception as e:
            return {"_kernel_step_status": "failed", "_kernel_step_meta": {"error": str(e), "file": str(full_path), "phase": phase}}

    def _h_execute_flow(self, args: Dict[str, Any], ctx: Dict[str, Any]) -> Any:
        flow_id = args.get("flow_id")
        if not flow_id:
            return {"_error": "missing flow_id"}
        flow_ctx = args.get("context", {})
        if ctx.get("_flow_execution_id"):
            flow_ctx["_parent_flow_execution_id"] = ctx["_flow_execution_id"]
        return self.execute_flow_sync(flow_id, flow_ctx, args.get("timeout"))

    def _h_save_flow(self, args: Dict[str, Any], ctx: Dict[str, Any]) -> Any:
        flow_id, flow_def = args.get("flow_id"), args.get("flow_def")
        if not flow_id or not flow_def:
            return {"_error": "missing flow_id or flow_def"}
        return {"path": self.save_flow_to_file(flow_id, flow_def, args.get("path", "user_data/flows"))}

    def _h_load_flows(self, args: Dict[str, Any], ctx: Dict[str, Any]) -> Any:
        return {"loaded": self.load_user_flows(args.get("path", "user_data/flows"))}

    # ------------------------------------------------------------------
    # flow.compose
    # ------------------------------------------------------------------

    def _h_flow_compose(self, args: Dict[str, Any], ctx: Dict[str, Any]) -> Any:
        try:
            from .flow_composer import get_flow_composer
            from .function_alias import get_function_alias_registry

            composer = get_flow_composer()
            alias_registry = get_function_alias_registry()
            composer.set_alias_registry(alias_registry)

            modifiers = composer.collect_modifiers(self.interface_registry)

            if not modifiers:
                return {
                    "_kernel_step_status": "skipped",
                    "_kernel_step_meta": {"reason": "no_modifiers"}
                }

            capabilities = {}
            all_caps = self.interface_registry.get("component.capabilities", strategy="all") or []
            for cap_dict in all_caps:
                if isinstance(cap_dict, dict):
                    capabilities.update(cap_dict)

            if self._flow:
                self._flow = composer.apply_modifiers(
                    self._flow,
                    modifiers,
                    self.interface_registry,
                    capabilities
                )

            applied = composer.get_applied_modifiers()

            self.diagnostics.record_step(
                phase="startup",
                step_id="flow.compose.complete",
                handler="kernel:flow.compose",
                status="success",
                meta={
                    "modifiers_collected": len(modifiers),
                    "modifiers_applied": len(applied),
                    "applied_ids": [m.get("id") for m in applied]
                }
            )

            return {
                "_kernel_step_status": "success",
                "_kernel_step_meta": {
                    "modifiers_collected": len(modifiers),
                    "modifiers_applied": len(applied)
                }
            }

        except Exception as e:
            self.diagnostics.record_step(
                phase="startup",
                step_id="flow.compose.error",
                handler="kernel:flow.compose",
                status="failed",
                error=e
            )
            return {
                "_kernel_step_status": "failed",
                "_kernel_step_meta": {"error": str(e)}
            }

    # ------------------------------------------------------------------
    # security / docker / approval / container / privilege / api
    # ------------------------------------------------------------------

    def _h_security_init(self, args: Dict[str, Any], ctx: Dict[str, Any]) -> Any:
        try:
            ctx["_security_initialized"] = True
            ctx["_strict_mode"] = args.get("strict_mode", True)

            self.diagnostics.record_step(
                phase="startup",
                step_id="security.init",
                handler="kernel:security.init",
                status="success",
                meta={"strict_mode": ctx["_strict_mode"]}
            )
            _logger.info("Security initialized", strict_mode=ctx["_strict_mode"])
            return {"_kernel_step_status": "success"}
        except Exception as e:
            _logger.error("Security init failed", exc_info=e, error=str(e))
            return {"_kernel_step_status": "failed", "_kernel_step_meta": {"error": str(e)}}

    def _h_docker_check(self, args: Dict[str, Any], ctx: Dict[str, Any]) -> Any:
        required = args.get("required", True)
        timeout = args.get("timeout_seconds", 10)

        try:
            result = subprocess.run(
                ["docker", "info"],
                capture_output=True,
                timeout=timeout
            )
            available = result.returncode == 0
        except (subprocess.TimeoutExpired, FileNotFoundError, Exception):
            available = False

        ctx["_docker_available"] = available

        try:
            get_metrics_collector().set_gauge("docker.available", 1.0 if available else 0.0)
        except Exception:
            pass

        if required and not available:
            self.diagnostics.record_step(
                phase="startup",
                step_id="docker.check",
                handler="kernel:docker.check",
                status="failed",
                error={"type": "DockerNotAvailable", "message": "Docker is required but not available"},
                meta={"required": required}
            )
            _logger.error("Docker check failed: Docker is required but not available",
                          required=required)
            return {"_kernel_step_status": "failed", "_kernel_step_meta": {"error": "Docker not available"}}

        self.diagnostics.record_step(
            phase="startup",
            step_id="docker.check",
            handler="kernel:docker.check",
            status="success",
            meta={"available": available, "required": required}
        )
        _logger.info("Docker check completed", available=available, required=required)
        return {"_kernel_step_status": "success", "_kernel_step_meta": {"docker_available": available}}

    def _h_approval_init(self, args: Dict[str, Any], ctx: Dict[str, Any]) -> Any:
        try:
            initialize_approval_manager()
            am = get_approval_manager()
            ctx["approval_manager"] = am

            self.diagnostics.record_step(
                phase="startup",
                step_id="approval.init",
                handler="kernel:approval.init",
                status="success"
            )
            _logger.info("Approval manager initialized")
            return {"_kernel_step_status": "success"}
        except Exception as e:
            self.diagnostics.record_step(
                phase="startup",
                step_id="approval.init",
                handler="kernel:approval.init",
                status="failed",
                error=e
            )
            _logger.error("Approval manager init failed", exc_info=e, error=str(e))
            return {"_kernel_step_status": "failed", "_kernel_step_meta": {"error": str(e)}}

    def _h_approval_scan(self, args: Dict[str, Any], ctx: Dict[str, Any]) -> Any:
        try:
            am = get_approval_manager()

            packs = am.scan_packs()
            check_hash = args.get("check_hash", True)

            modified = []
            pending = []
            approved = []

            for pack_id in packs:
                status = am.get_status(pack_id)
                # SV-10 fix: get_status() が None を返す場合（未登録 Pack）をハンドリング
                if status is None:
                    pending.append(pack_id)
                    continue
                if (
                    callable(getattr(am, "auto_approve_if_dev", None))
                    and am.auto_approve_if_dev(pack_id) is True
                ):
                    approved.append(pack_id)
                    continue
                if status:
                    status_str = status.value if hasattr(status, 'value') else str(status)
                    if status_str in ("installed", "pending"):
                        try:
                            if (
                                callable(getattr(am, "auto_approve_if_dev", None))
                                and am.auto_approve_if_dev(pack_id) is True
                            ):
                                status = am.get_status(pack_id)
                                status_str = status.value if hasattr(status, 'value') else str(status)
                        except Exception:
                            _logger.debug(
                                "DEV auto-approve failed during approval scan",
                                exc_info=True,
                                pack_id=pack_id,
                            )
                    if status_str == "approved":
                        if check_hash and not am.verify_hash(pack_id):
                            am.mark_modified(pack_id)
                            modified.append(pack_id)
                        else:
                            approved.append(pack_id)
                    elif status_str in ("installed", "pending"):
                        pending.append(pack_id)
                    elif status_str == "modified":
                        modified.append(pack_id)

            ctx["_packs_approved"] = approved
            ctx["_packs_pending"] = pending
            ctx["_packs_modified"] = modified

            if modified:
                import sys as _sys
                for _pid in modified:
                    print(
                        f"[Rumi] WARNING: Pack '{_pid}' has been modified since approval. "
                        f"Component setup will be skipped. Re-approve to fix: "
                        f"POST /api/packs/{_pid}/approve",
                        file=_sys.stderr,
                    )

            if pending:
                import sys as _sys
                for _pid in pending:
                    print(
                        f"[Rumi] WARNING: Pack '{_pid}' is awaiting approval. "
                        f"Component setup will be skipped.",
                        file=_sys.stderr,
                    )


            self.diagnostics.record_step(
                phase="startup",
                step_id="approval.scan",
                handler="kernel:approval.scan",
                status="success",
                meta={
                    "total": len(packs),
                    "approved": len(approved),
                    "pending": len(pending),
                    "modified": len(modified)
                }
            )
            return {"_kernel_step_status": "success", "_kernel_step_meta": {
                "approved": approved, "pending": pending, "modified": modified
            }}
        except Exception as e:
            return {"_kernel_step_status": "failed", "_kernel_step_meta": {"error": str(e)}}

    def _h_container_init(self, args: Dict[str, Any], ctx: Dict[str, Any]) -> Any:
        try:
            from .container_orchestrator import initialize_container_orchestrator, get_container_orchestrator
            initialize_container_orchestrator()
            co = get_container_orchestrator()
            ctx["container_orchestrator"] = co

            self.diagnostics.record_step(
                phase="startup",
                step_id="container.init",
                handler="kernel:container.init",
                status="success"
            )
            return {"_kernel_step_status": "success"}
        except Exception as e:
            _logger.error("Container orchestrator init failed", exc_info=e, error=str(e))
            return {"_kernel_step_status": "failed", "_kernel_step_meta": {"error": str(e)}}

    def _h_privilege_init(self, args: Dict[str, Any], ctx: Dict[str, Any]) -> Any:
        try:
            from .host_privilege_manager import initialize_host_privilege_manager, get_host_privilege_manager
            initialize_host_privilege_manager()
            hpm = get_host_privilege_manager()
            ctx["host_privilege_manager"] = hpm

            self.diagnostics.record_step(
                phase="startup",
                step_id="privilege.init",
                handler="kernel:privilege.init",
                status="success"
            )
            return {"_kernel_step_status": "success"}
        except Exception as e:
            _logger.error("Host privilege manager init failed", exc_info=e, error=str(e))
            return {"_kernel_step_status": "failed", "_kernel_step_meta": {"error": str(e)}}

    def _h_api_init(self, args: Dict[str, Any], ctx: Dict[str, Any]) -> Any:
        try:
            from .pack_api_server import initialize_pack_api_server
            from .app_lifecycle_manager import AppLifecycleManager, mark_panel_ready
            from .paths import BASE_DIR as _api_base_dir

            host = args.get("host", "127.0.0.1")
            port = _resolve_api_port(args)

            api_server = initialize_pack_api_server(
                host=host,
                port=port,
                approval_manager=ctx.get("approval_manager"),
                container_orchestrator=ctx.get("container_orchestrator"),
                host_privilege_manager=ctx.get("host_privilege_manager"),
                kernel=self,
                app_lifecycle_manager=AppLifecycleManager(base_dir=_api_base_dir)
            )
            ctx["pack_api_server"] = api_server

            # Wave fix: io.http.server を InterfaceRegistry に登録
            # app.py が HTTP サーバーの起動を検知できるようにする
            _api_host, _api_port = host, port
            def _api_server_runner(kernel_facade):
                """Pack API server is already running. Block until termination."""
                print(f"[Rumi] Pack API server running on http://{_api_host}:{_api_port}")
                import signal as _sig
                try:
                    _sig.pause()
                except AttributeError:
                    # Windows: signal.pause() is not available
                    import time as _time
                    while True:
                        _time.sleep(1)
            self.interface_registry.register(
                "io.http.server",
                _api_server_runner,
                meta={"_system": True, "source": "kernel:api.init", "host": host, "port": port},
            )

            self.diagnostics.record_step(
                phase="startup",
                step_id="api.init",
                handler="kernel:api.init",
                status="success",
                meta={"host": host, "port": port}
            )
            mark_panel_ready()
            return {"_kernel_step_status": "success"}
        except Exception as e:
            _logger.error("Pack API server init failed", exc_info=e, error=str(e))
            return {"_kernel_step_status": "failed", "_kernel_step_meta": {"error": str(e)}}

    def _h_container_start_approved(self, args: Dict[str, Any], ctx: Dict[str, Any]) -> Any:
        approved = ctx.get("_packs_approved", [])
        if not approved:
            return {"_kernel_step_status": "success", "_kernel_step_meta": {"started": 0}}

        co = ctx.get("container_orchestrator")
        if not co:
            return {"_kernel_step_status": "skipped", "_kernel_step_meta": {"reason": "no_orchestrator"}}

        started = []
        failed = []
        timeout = args.get("timeout_per_pack", 30)

        for pack_id in approved:
            try:
                result = co.start_container(pack_id, timeout=timeout)
                if result.success:
                    started.append(pack_id)
                else:
                    failed.append({"pack_id": pack_id, "error": result.error})
            except Exception as e:
                failed.append({"pack_id": pack_id, "error": str(e)})

        ctx["_containers_started"] = started

        try:
            mc = get_metrics_collector()
            mc.increment("container.start.success", value=len(started))
            mc.increment("container.start.failure", value=len(failed))
        except Exception:
            pass

        self.diagnostics.record_step(
            phase="startup",
            step_id="container.start_approved",
            handler="kernel:container.start_approved",
            status="success",
            meta={"started": len(started), "failed_count": len(failed), "failed": failed}
        )
        _logger.info("Container start completed",
                      started_count=len(started), failed_count=len(failed))
        if failed:
            _logger.warning("Some containers failed to start",
                            failed_packs=[f["pack_id"] for f in failed])
        return {"_kernel_step_status": "success", "_kernel_step_meta": {"started": started, "failed": failed}}

    # ------------------------------------------------------------------
    # component discover / load
    # ------------------------------------------------------------------

    def _clear_defaults_compat_handles(self) -> None:
        """Clear only the kernel-owned compatibility handle retention field."""
        self._defaults_compat_handles = ()

    def _h_defaults_compat_build(
        self,
        args: dict[str, Any],
        ctx: dict[str, Any],
    ) -> Any:
        """Build and retain the immutable Defaults compatibility metadata."""
        from .global_contracts.defaults_compat import (
            build_defaults_compatibility_handle,
            defaults_compatibility_api_inventory,
            legacy_inventory_from_components,
        )

        self._defaults_compat_handles = ()
        try:
            legacy_inventory = args.get("legacy_inventory")
            if legacy_inventory is None:
                legacy_inventory = legacy_inventory_from_components(
                    ctx.get("_discovered_component_objects", ())
                )
            api_inventory = args.get("api_inventory")
            if api_inventory is None:
                api_inventory = defaults_compatibility_api_inventory()
            handle = build_defaults_compatibility_handle(
                legacy_inventory,
                api_inventory,
            )
        except (AttributeError, TypeError, ValueError) as exc:
            return _failed_step_result(
                "kernel:defaults.compat.build",
                exc,
            )

        self._defaults_compat_handles = (handle,)
        if not getattr(
            self,
            "_defaults_compat_shutdown_registered",
            False,
        ):
            self.on_shutdown(self._clear_defaults_compat_handles)
            self._defaults_compat_shutdown_registered = True
        return {
            "_kernel_step_status": "success",
            "_kernel_step_meta": {"compatibility_ids": len(handle.entries)},
            "output": handle,
        }

    def _h_component_discover(self, args: Dict[str, Any], ctx: Dict[str, Any]) -> Any:
        approved_only = args.get("approved_only", True)
        approved = ctx.get("_packs_approved", [])

        try:
            reg = get_registry()


            # ActiveEcosystem の overrides/disabled を反映
            _active_eco = ctx.get("active_ecosystem")
            _overrides = {}
            _disabled_set = set()
            if _active_eco:
                try:
                    _overrides = _active_eco.get_all_overrides() if hasattr(_active_eco, 'get_all_overrides') else {}
                    _cfg = _active_eco.config
                    _disabled_set = set(getattr(_cfg, 'disabled_components', []))
                except Exception:
                    pass
            _override_selected = {}
            for _ct, _ci in _overrides.items():
                _override_selected[_ct] = str(_ci)

            components = []
            component_objects = []
            for comp in reg.get_all_components():
                pack_id = getattr(comp, "pack_id", None)
                if approved_only and pack_id not in approved:
                    continue
                _full_id = getattr(comp, "full_id", None)
                _comp_type = getattr(comp, "type", None)
                _comp_id = getattr(comp, "id", None)

                # disabled チェック
                if _full_id and _full_id in _disabled_set:
                    continue
                # override チェック
                if _comp_type in _override_selected:
                    _selected_override = _override_selected[_comp_type]
                    if _comp_id != _selected_override and _full_id != _selected_override:
                        continue

                components.append({
                    "full_id": _full_id,
                    "pack_id": pack_id,
                    "type": _comp_type,
                    "id": _comp_id
                })
                component_objects.append(comp)

            ctx["_discovered_components"] = components
            ctx["_discovered_component_objects"] = component_objects

            try:
                get_metrics_collector().set_gauge("component.discovered.count", float(len(components)))
            except Exception:
                pass

            self.diagnostics.record_step(
                phase="startup",
                step_id="component.discover",
                handler="kernel:component.discover",
                status="success",
                meta={"count": len(components), "approved_only": approved_only}
            )
            _logger.info("Component discovery completed",
                          count=len(components), approved_only=approved_only)
            return {"_kernel_step_status": "success", "_kernel_step_meta": {"count": len(components)}}
        except Exception as e:
            _logger.error("Component discovery failed", exc_info=e, error=str(e))
            return {"_kernel_step_status": "failed", "_kernel_step_meta": {"error": str(e)}}

    def _h_component_load(self, args: Dict[str, Any], ctx: Dict[str, Any]) -> Any:
        container_execution = args.get("container_execution", True)
        components = ctx.get("_discovered_component_objects", [])

        if not components:
            return {"_kernel_step_status": "success", "_kernel_step_meta": {"loaded": 0}}

        self.lifecycle.run_phase("setup", components=components)

        self.diagnostics.record_step(
            phase="startup",
            step_id="component.load",
            handler="kernel:component.load",
            status="success",
            meta={"container_execution": container_execution}
        )
        return {"_kernel_step_status": "success"}

    # ------------------------------------------------------------------
    # emit / startup.failed / vocab.load / noop
    # ------------------------------------------------------------------

    def _h_emit(self, args: Dict[str, Any], ctx: Dict[str, Any]) -> Any:
        event = args.get("event", "")
        if event == "system.ready":
            try:
                from .app_lifecycle_manager import mark_runtime_ready

                mark_runtime_ready()
            except Exception:
                _logger.warning("Failed to mark runtime as ready", exc_info=True)
        if event and self.event_bus:
            self.event_bus.publish(event, {"ts": self._now_ts()})
        return {"_kernel_step_status": "success"}

    def _h_startup_failed(self, args: Dict[str, Any], ctx: Dict[str, Any]) -> Any:
        pending = ctx.get("_packs_pending", [])
        modified = ctx.get("_packs_modified", [])

        self.diagnostics.record_step(
            phase="startup",
            step_id="startup.failed",
            handler="kernel:startup.failed",
            status="failed",
            meta={
                "pending_approvals": pending,
                "modified_packs": modified,
                "message": "Startup failed. Check pending approvals or Docker availability."
            }
        )
        return {"_kernel_step_status": "success"}

    def _h_vocab_load(self, args: Dict[str, Any], ctx: Dict[str, Any]) -> Any:
        try:
            from .vocab_registry import get_vocab_registry
            vr = get_vocab_registry()

            file_path = args.get("file")
            pack_id = args.get("pack_id")

            if file_path:
                from pathlib import Path
                count = vr.load_vocab_file(Path(file_path), pack_id)
                return {"_kernel_step_status": "success", "_kernel_step_meta": {"groups_loaded": count}}

            return {"_kernel_step_status": "skipped", "_kernel_step_meta": {"reason": "no_file"}}
        except Exception as e:
            return {"_kernel_step_status": "failed", "_kernel_step_meta": {"error": str(e)}}

    def _h_noop(self, args: Dict[str, Any], ctx: Dict[str, Any]) -> Any:
        """何もしないハンドラ(プレースホルダー)"""
        return {"_kernel_step_status": "success", "_kernel_step_meta": {"handler": "noop"}}

    # ------------------------------------------------------------------
    # Phase B-1: kernel function registration handler
    # ------------------------------------------------------------------

    def _h_register_kernel_functions(self, args: Dict[str, Any], ctx: Dict[str, Any]) -> Any:
        """
        Register all kernel handler manifests into FunctionRegistry.

        Called during ecosystem phase of startup flow.
        Retrieves FunctionRegistry from InterfaceRegistry and invokes
        _register_kernel_functions() from kernel.py.

        Manifest metadata source: kernel.py _KERNEL_HANDLER_MANIFESTS (原則 D)
        """
        try:
            from . import kernel as _kernel_module

            # Retrieve FunctionRegistry from InterfaceRegistry
            fr = self.interface_registry.get("function_registry", strategy="last")
            if fr is None:
                # Fallback: create a FunctionRegistry and register it
                try:
                    from .function_registry import FunctionRegistry
                    fr = FunctionRegistry()
                    self.interface_registry.register(
                        "function_registry", fr,
                        meta={"source": "kernel:register_kernel_functions", "auto_created": True},
                    )
                    _logger.info("FunctionRegistry auto-created and registered to IR")
                except Exception as e_fr:
                    _logger.error("FunctionRegistry not available: %s", e_fr)
                    return {
                        "_kernel_step_status": "failed",
                        "_kernel_step_meta": {
                            "error": f"FunctionRegistry not available: {e_fr}",
                        },
                    }

            registered = _kernel_module._register_kernel_functions(fr)

            self.diagnostics.record_step(
                phase="startup",
                step_id="register_kernel_functions",
                handler="kernel:register_kernel_functions",
                status="success",
                meta={"registered_count": registered},
            )

            return {
                "_kernel_step_status": "success",
                "_kernel_step_meta": {"registered_count": registered},
            }

        except Exception as e:
            self.diagnostics.record_step(
                phase="startup",
                step_id="register_kernel_functions",
                handler="kernel:register_kernel_functions",
                status="failed",
                error=e,
            )
            _logger.error("Failed to register kernel functions: %s", e, exc_info=True)
            return {
                "_kernel_step_status": "failed",
                "_kernel_step_meta": {"error": str(e)},
            }
