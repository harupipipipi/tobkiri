"""
blocks/tool/setup.py - Tool component setup phase

Registers tool and consent HTTP routes into the kernel's InterfaceRegistry
under the key ``io.http.route``.
"""

import sys
import os


def run(context):
    """Called by the kernel during the *setup* phase."""
    pack_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    if pack_root not in sys.path:
        sys.path.insert(0, pack_root)

    interface_registry = context["interface_registry"]
    source_component = context.get("_source_component", "defaultspack:tool:tool")
    try:
        from capability_bindings import register_defaultspack_binding_handlers
        register_defaultspack_binding_handlers(interface_registry)
    except Exception as exc:
        print(
            "[defaultspack.tool] setup: failed to register capability bindings - "
            + str(exc),
            file=sys.stderr,
        )

    def _lazy(module_path, func_name="run", *, sensitive=False, pre_auth=False, local_only=False):
        """Return a lazy handler that imports the module on first call."""
        def handler(request_data, context):
            import importlib
            mod = importlib.import_module(module_path)
            fn = getattr(mod, func_name)
            return fn(request_data, context)
        try:
            setattr(handler, "__rumi_route_sensitive__", bool(sensitive))
            setattr(handler, "__rumi_route_pre_auth__", bool(pre_auth))
            setattr(handler, "__rumi_route_local_only__", bool(local_only))
        except Exception:
            pass
        return handler

    def _guarded(handler, operation, risk="high"):
        """Wrap high-risk local routes in the shared approval/audit path."""
        def guarded_handler(request_data, context):
            from blocks.tool._safety import (
                approved_or_request,
                record_tool_attempt,
                record_tool_execution,
                record_tool_failure,
            )

            payload = request_data if isinstance(request_data, dict) else {}
            record_tool_attempt(operation, risk, payload)
            approval = approved_or_request(payload, context, operation, risk)
            if approval is not None:
                return approval
            result = handler(request_data, context)
            if isinstance(result, dict) and result.get("status") == "error":
                err = result.get("error", {})
                message = err.get("message") if isinstance(err, dict) else err
                record_tool_failure(operation, risk, payload, str(message or "route failed"))
            else:
                record_tool_execution(operation, risk, payload)
            return result

        return guarded_handler

    routes = [
        # ---- Tool read/invoke routes ----
        ("GET", "/api/tools", _lazy("blocks.tool.list"), {}),
        ("GET", "/api/tools/names", _lazy("blocks.tool.names"), {}),
        ("GET", "/api/tools/catalog", _lazy("blocks.tool.catalog"), {}),
        ("POST", "/api/tools/selection/preview", _lazy("blocks.tool.selection_preview"), {}),
        ("GET", "/api/tools/selection/traces/{trace_id}", _lazy("blocks.tool.selection_trace"), {"trace_id": "trace_id"}),
        ("POST", "/api/tools/embedding-index/rebuild", _lazy("blocks.tool.embedding_index_rebuild"), {}),
        ("POST", "/api/tools/invoke", _lazy("blocks.tool.invoke"), {}),
        ("POST", "/api/tools/browser-computer", _lazy("blocks.tool.browser_computer"), {}),
        (
            "GET",
            "/api/tools/browser-companion/session",
            _lazy("blocks.tool.browser_companion_session", sensitive=True, local_only=True),
            {},
        ),
        ("POST", "/api/tools/browser-companion/bridge/poll", _lazy("blocks.tool.browser_companion_bridge", "run_poll"), {}),
        ("POST", "/api/tools/browser-companion/bridge/result", _lazy("blocks.tool.browser_companion_bridge", "run_result"), {}),
        # ---- Capability catalog routes ----
        ("GET", "/api/capabilities", _lazy("blocks.capability.list"), {}),
        ("GET", "/api/capabilities/catalog", _lazy("blocks.capability.api"), {}),
        ("GET", "/api/capabilities/settings", _lazy("blocks.capability.api", "run", sensitive=True), {}),
        ("PATCH", "/api/capabilities/settings", _lazy("blocks.capability.api", "run", sensitive=True), {}),
        ("POST", "/api/capabilities/resolve", _lazy("blocks.capability.api", "run", sensitive=True), {}),
        ("GET", "/api/capabilities/plans/{plan_id}", _lazy("blocks.capability.api", "run", sensitive=True), {"plan_id": "plan_id"}),
        ("GET", "/api/capabilities/traces/{trace_id}", _lazy("blocks.capability.api", "run", sensitive=True), {"trace_id": "trace_id"}),
        ("POST", "/api/capabilities/plans/{plan_id}/approve", _lazy("blocks.capability.api", "run", sensitive=True), {"plan_id": "plan_id"}),
        ("POST", "/api/capabilities/plans/{plan_id}/execute", _lazy("blocks.capability.api", "run", sensitive=True), {"plan_id": "plan_id"}),
        ("POST", "/api/capabilities/manifests/validate", _lazy("blocks.capability.api", "run"), {}),
        ("POST", "/api/capabilities/schemas/compile", _lazy("blocks.capability.api", "run"), {}),
        ("GET", "/api/skills", _lazy("blocks.capability.api", "run"), {}),
        ("PATCH", "/api/skills/lifecycle", _lazy("blocks.capability.api", "run", sensitive=True), {}),
        ("GET", "/api/capabilities/{id}", _lazy("blocks.capability.manifest"), {"id": "capability_id"}),
        # ---- Dynamic tool routes ----
        ("POST", "/api/tools/create", _lazy("blocks.tool.create"), {}),
        ("PUT", "/api/tools/{name}", _lazy("blocks.tool.update"), {"name": "name"}),
        ("DELETE", "/api/tools/{name}", _lazy("blocks.tool.delete"), {"name": "name"}),
        ("GET", "/api/tools/{name}/export", _lazy("blocks.tool.export"), {"name": "name"}),
        # ---- Tool policy routes ----
        ("GET", "/api/tools/permissions", _lazy("blocks.tool.permissions", "run_get"), {}),
        ("PUT", "/api/tools/permissions", _lazy("blocks.tool.permissions", "run_put"), {}),
        ("POST", "/api/tools/permissions/check", _lazy("blocks.tool.permissions", "run_check"), {}),
        ("GET", "/api/tools/{name}/permissions", _lazy("blocks.tool.permissions", "run_get"), {"name": "name"}),
        ("PUT", "/api/tools/{name}/permissions", _lazy("blocks.tool.permissions", "run_put"), {"name": "name"}),
        # ---- Consent routes ----
        ("POST", "/api/consent/check", _lazy("blocks.tool.consent_check"), {}),
        ("POST", "/api/consent/{id}/confirm", _lazy("blocks.tool.consent_confirm"), {"id": "consent_id"}),
        # ---- MCP routes ----
        ("POST", "/api/tools/mcp/connect", _lazy("blocks.tool.mcp_connect"), {}),
        ("GET", "/api/tools/mcp", _lazy("blocks.tool.mcp_list"), {}),
        ("POST", "/api/tools/mcp", _lazy("blocks.tool.mcp_registry"), {}),
        ("DELETE", "/api/tools/mcp", _lazy("blocks.tool.mcp_registry"), {}),
        ("GET", "/api/browser/artifacts", _lazy("blocks.browser.artifacts"), {}),
        # ---- Container routes (T14) ----
        ("POST", "/api/container", _guarded(_lazy("blocks.tool.container.create"), "container.create"), {}),
        ("POST", "/api/container/{id}/start", _guarded(_lazy("blocks.tool.container.start"), "container.start"), {"id": "id"}),
        ("POST", "/api/container/{id}/stop", _guarded(_lazy("blocks.tool.container.stop"), "container.stop"), {"id": "id"}),
        ("DELETE", "/api/container/{id}", _guarded(_lazy("blocks.tool.container.delete"), "container.delete"), {"id": "id"}),
        ("POST", "/api/container/{id}/exec", _guarded(_lazy("blocks.tool.container.exec"), "container.exec"), {"id": "id"}),
        ("GET", "/api/container/{id}/screenshot", _guarded(_lazy("blocks.tool.container.screenshot"), "container.screenshot"), {"id": "id"}),
        ("POST", "/api/container/{id}/input", _guarded(_lazy("blocks.tool.container.input"), "container.input"), {"id": "id"}),
        # ---- Container task routes (T14) ----
        ("POST", "/api/container/task", _guarded(_lazy("blocks.tool.container.task_create"), "container.task.create"), {}),
        ("GET", "/api/container/task/{id}", _lazy("blocks.tool.container.task_status"), {"id": "id"}),
        ("GET", "/api/container/task/{id}/result", _lazy("blocks.tool.container.task_result"), {"id": "id"}),
        ("POST", "/api/container/task/{id}/abort", _guarded(_lazy("blocks.tool.container.task_abort"), "container.task.abort"), {"id": "id"}),
        # ---- Container settings routes (T14) ----
        ("PUT", "/api/container/settings", _guarded(_lazy("blocks.tool.container.settings"), "container.settings.update", risk="medium"), {}),
        ("GET", "/api/container/settings", _lazy("blocks.tool.container.settings"), {}),
    ]

    for method, pattern, handler, path_inject in routes:
        capability_actions = {
            ("GET", "/api/capabilities/catalog"): "catalog",
            ("GET", "/api/capabilities/settings"): "settings",
            ("PATCH", "/api/capabilities/settings"): "update_settings",
            ("POST", "/api/capabilities/resolve"): "resolve",
            ("GET", "/api/capabilities/plans/{plan_id}"): "plan",
            ("GET", "/api/capabilities/traces/{trace_id}"): "trace",
            ("POST", "/api/capabilities/plans/{plan_id}/approve"): "approve",
            ("POST", "/api/capabilities/plans/{plan_id}/execute"): "execute",
            ("POST", "/api/capabilities/manifests/validate"): "validate_manifest",
            ("POST", "/api/capabilities/schemas/compile"): "compile_schema",
            ("GET", "/api/skills"): "skills",
            ("PATCH", "/api/skills/lifecycle"): "update_skill",
        }
        action = capability_actions.get((method, pattern))
        if action:
            base_handler = handler

            def handler(request_data, route_context, _handler=base_handler, _action=action):
                payload = dict(request_data or {})
                payload["action"] = _action
                return _handler(payload, route_context)
        interface_registry.register(
            "io.http.route",
            {
                "method": method,
                "pattern": pattern,
                "handler": handler,
                "path_inject": path_inject,
                "sensitive": bool(getattr(handler, "__rumi_route_sensitive__", False)),
                "pre_auth": bool(getattr(handler, "__rumi_route_pre_auth__", False)),
                "local_only": bool(getattr(handler, "__rumi_route_local_only__", False)),
            },
            meta={"_source_component": source_component},
        )
