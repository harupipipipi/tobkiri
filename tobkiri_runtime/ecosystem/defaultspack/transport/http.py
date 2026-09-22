from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from blocks._common import ok, error, timestamp

import base64
import hashlib
import hmac
import json
import logging
import queue
import re
import signal
import threading
import http.server
import urllib.parse
from pathlib import Path
from typing import Any, Callable, NoReturn

from core_runtime.api.safe_headers import (
    RESERVED_REQUEST_CONTEXT_KEYS,
    sanitized_forwarded_headers,
    strip_reserved_request_context,
)
from core_runtime.host_contract import host_contract_value

from bridge.block_adapter import invoke_block
from domain.safety.local_guard import (
    METHOD_SENSITIVE_CODING_PATHS,
    SENSITIVE_CODING_PATHS,
    is_loopback_request as _local_is_loopback_request,
    is_sensitive_coding_path as _local_is_sensitive_coding_path,
    origin_allowed as _local_origin_allowed,
    require_local_guard,
)
from transport.registry import (
    build_always_available_http_routes,
    build_fallback_http_routes,
    compile_http_route_pattern,
    flow_http_output_is_compatible,
    http_route_sort_key,
)

logger = logging.getLogger(__name__)


def _removed_authority_boundary() -> NoReturn:
    """Enter the retired authority boundary, which always fails closed."""
    from core_runtime.legacy_runtime_removed import removed_authority_service

    removed_authority_service()
    raise RuntimeError("retired authority boundary unexpectedly returned")


def _mapping_or_empty(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


_SAFE_GET_FALLBACK_BLOCKS = {
    "blocks.ai.catalog",
    "blocks.ai.models",
    "blocks.ai.profiles",
    "blocks.ai.providers",
    "blocks.chat.get_conversation",
    "blocks.chat.list_conversations",
    "blocks.external.templates",
    "blocks.connections.codex",
    "blocks.tool.list",
    "blocks.ui.catalog",
    "blocks.ui.commands",
    "blocks.ui.conversation_preview",
    "blocks.ui.provider_health",
    "blocks.ui.settings",
}

_LONG_RUNNING_FALLBACK_BLOCKS = {
    "blocks.ambient.event_submit",
    "blocks.agent.run_subagent",
    "blocks.agent.scheduler.trigger",
    "blocks.chat.send",
    "blocks.integrations.line",
    "blocks.webhooks.inbound",
}
_LONG_RUNNING_FALLBACK_TIMEOUT_SECONDS = 300.0
_SCHEDULE_TRIGGER_FALLBACK_TIMEOUT_SECONDS = 1800.0

_GRANT_DENIED_DIRECT_FALLBACK_BLOCKS = {
    "blocks.agent.run_subagent",
}

_DIRECT_HTTP_COMPATIBILITY_BLOCKS = {
    "blocks.agent.run_subagent",
}

_IN_PROCESS_HTTP_FALLBACK_BLOCKS = {
    "blocks.sandbox.api",
}

_DIRECT_SAFE_GET_FALLBACK_BLOCKS = {
    "blocks.ui.catalog",
    "blocks.ui.conversation_preview",
    "blocks.ui.provider_health",
    "blocks.ui.settings",
}
# The initial shell requests the catalog, settings, profiles, and command
# protocol together.  On a cold runtime those read-only projections contend
# for the same registries and can legitimately take a little over ten seconds.
# Keep the fallback bounded, but allow the loading screen to wait for the
# canonical startup data instead of turning a healthy cold start into a
# BOOTSTRAP_API_TIMEOUT.
_DIRECT_SAFE_GET_FALLBACK_TIMEOUT_SECONDS = 30.0

_CHAT_TURN_HTTP_FALLBACKS = {
    ("POST", "/v1/chat/completions"): ("defaultspack.chat_turn", "blocks.chat.send"),
    (
        "POST",
        "/api/chat/conversations/{id}/messages",
    ): ("defaultspack.chat_turn", "blocks.chat.send"),
    (
        "POST",
        "/api/chat/conversations/{id}/stream",
    ): ("defaultspack.chat_stream_turn", "blocks.chat.stream"),
}


def _platform_release():
    if sys.platform != "darwin":
        return ""
    try:
        import subprocess

        result = subprocess.run(
            ["sw_vers", "-productVersion"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        return result.stdout.strip() or "unknown"
    except Exception:
        return "unknown"


class DefaultsHttpServer:
    def __init__(self, facade):
        # Legacy kernel facades expose live InterfaceRegistry state.  The v4
        # server accepts only the activation snapshot installed in DI.
        del facade
        self.facade = None
        self.host = os.environ.get("DEFAULTS_HTTP_HOST", "127.0.0.1")
        self.port = int(os.environ.get("DEFAULTS_HTTP_PORT", "8766"))
        self._server: http.server.ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._routes: list[Any] = []
        self._load_runtime_secrets()
        self._setup_routes()

    def _load_runtime_secrets(self):
        try:
            from domain.integrations.secrets import load_integration_secrets_into_env

            load_integration_secrets_into_env()
        except Exception:
            pass

    def _setup_routes(self):
        """Build the route table.

        If the kernel facade is available and components have registered
        ``io.http.route`` entries via InterfaceRegistry, those are used.
        Otherwise the hard-coded fallback list is used for backward
        compatibility.

        Each entry in ``self._routes`` is a 6-tuple:
            (method, compiled_regex, handler, source, path_inject, pattern)

        *source* is ``"registry"`` or ``"fallback"``.
        *path_inject* is a dict mapping URL param names to request_data keys
        (only meaningful for registry routes).
        """
        registry_routes = []

        # ---- Attempt to collect routes from InterfaceRegistry ----
        if self.facade is not None:
            try:
                raw = self.facade.get_interface("io.http.route", strategy="all")
                if raw and isinstance(raw, list):
                    route_entries: list[tuple[Any, Any, Callable[..., Any], Any, Any, dict[str, Any]]] = []
                    for index, entry in enumerate(raw):
                        if not isinstance(entry, dict):
                            continue
                        method = entry.get("method")
                        pattern = entry.get("pattern")
                        handler = entry.get("handler")
                        flow_id = str(entry.get("flow_id") or "").strip()
                        function_name = str(
                            entry.get("function_name")
                            or entry.get("qualified_name")
                            or entry.get("function")
                            or entry.get("function_id")
                            or ""
                        ).strip()
                        block_module = str(entry.get("block_module") or "").strip()
                        fallback_block_module = str(
                            entry.get("fallback_block_module")
                            or entry.get("fallback_block")
                            or block_module
                            or ""
                        ).strip()
                        path_inject = entry.get("path_inject", {})
                        defaults = entry.get("defaults")
                        if not isinstance(defaults, dict):
                            defaults = entry.get("default_args")
                        route_defaults = dict(defaults) if isinstance(defaults, dict) else {}
                        route_authority: dict[str, Any] = {
                            "permission_id": str(entry.get("permission_id") or "").strip(),
                            "owner_pack_id": str(entry.get("owner_pack_id") or entry.get("pack_id") or "defaultspack").strip(),
                            "provider_id": str(entry.get("provider_id") or "").strip(),
                            "frontend_id": str(entry.get("frontend_id") or "").strip(),
                            "function_id": str(entry.get("function_id") or "").strip(),
                            "audience": str(entry.get("audience") or "kernel_api").strip(),
                            "resource_template": dict(entry.get("resource_template") or {})
                            if isinstance(entry.get("resource_template"), dict)
                            else {},
                        }
                        if entry.get("core_only", False):
                            route_authority["core_only"] = True
                        route_authority = {
                            key: value
                            for key, value in route_authority.items()
                            if value not in ("", {}, None)
                        }
                        route_sensitive = bool(entry.get("sensitive"))
                        route_pre_auth = bool(entry.get("pre_auth"))
                        route_local_only = bool(entry.get("local_only"))
                        method_key = str(method or "").upper()
                        pattern_key = str(pattern or "")
                        if pattern_key.startswith("/api/prompts"):
                            route_sensitive = True
                            route_local_only = True
                        mapped_flow = _CHAT_TURN_HTTP_FALLBACKS.get((method_key, pattern_key))
                        if mapped_flow and not flow_id:
                            flow_id, fallback_block_module = mapped_flow
                        if method and pattern and flow_id:

                            def _flow_handler(
                                request_data,
                                path_params,
                                *,
                                route_flow_id=flow_id,
                                route_fallback_block_module=fallback_block_module,
                                route_path_inject=dict(path_inject)
                                if isinstance(path_inject, dict)
                                else {},
                                route_defaults=dict(route_defaults),
                                route_method=str(method or "").upper(),
                            ):
                                payload = dict(request_data or {})
                                payload.update(route_defaults)
                                payload["_method"] = route_method
                                return self._invoke_flow_route(
                                    route_flow_id,
                                    payload,
                                    path_params,
                                    route_path_inject,
                                    fallback_block_module=route_fallback_block_module,
                                )

                            setattr(_flow_handler, "_defaultspack_flow_route_handler", True)
                            try:
                                setattr(_flow_handler, "__rumi_route_sensitive__", route_sensitive)
                                setattr(_flow_handler, "__rumi_route_pre_auth__", route_pre_auth)
                                setattr(_flow_handler, "__rumi_route_local_only__", route_local_only)
                            except Exception:
                                pass
                            route_entries.append(
                                (method, pattern, _flow_handler, path_inject, index, route_authority)
                            )
                        elif method and pattern and function_name:

                            def _function_handler(
                                request_data,
                                path_params,
                                *,
                                route_function_name=function_name,
                                route_fallback_block_module=fallback_block_module,
                                route_path_inject=dict(path_inject)
                                if isinstance(path_inject, dict)
                                else {},
                                route_defaults=dict(route_defaults),
                                route_method=str(method or "").upper(),
                            ):
                                payload = dict(request_data or {})
                                payload.update(route_defaults)
                                payload["_method"] = route_method
                                return self._invoke_function_route(
                                    route_function_name,
                                    payload,
                                    path_params,
                                    route_path_inject,
                                    fallback_block_module=route_fallback_block_module,
                                )

                            setattr(_function_handler, "_defaultspack_flow_route_handler", True)
                            try:
                                setattr(_function_handler, "__rumi_route_sensitive__", route_sensitive)
                                setattr(_function_handler, "__rumi_route_pre_auth__", route_pre_auth)
                                setattr(_function_handler, "__rumi_route_local_only__", route_local_only)
                            except Exception:
                                pass
                            route_entries.append(
                                (method, pattern, _function_handler, path_inject, index, route_authority)
                            )
                        elif method and pattern and callable(handler):
                            try:
                                setattr(
                                    handler,
                                    "__rumi_route_sensitive__",
                                    bool(
                                        route_sensitive
                                        or getattr(handler, "__rumi_route_sensitive__", False)
                                    ),
                                )
                                setattr(
                                    handler,
                                    "__rumi_route_pre_auth__",
                                    bool(
                                        route_pre_auth
                                        or getattr(handler, "__rumi_route_pre_auth__", False)
                                    ),
                                )
                                setattr(
                                    handler,
                                    "__rumi_route_local_only__",
                                    bool(
                                        route_local_only
                                        or getattr(handler, "__rumi_route_local_only__", False)
                                    ),
                                )
                            except Exception:
                                pass
                            route_entries.append((method, pattern, handler, path_inject, index, route_authority))
                    for method, pattern, handler, path_inject, index, route_authority in sorted(
                        route_entries,
                        key=lambda item: http_route_sort_key(item[0], item[1], item[4]),
                    ):
                        compiled = compile_http_route_pattern(pattern)
                        target = getattr(handler, "__func__", handler)
                        try:
                            setattr(target, "__rumi_route_pattern__", pattern)
                            setattr(target, "__rumi_route_authority__", route_authority)
                        except Exception:
                            pass
                        registry_routes.append(
                            (method, compiled, handler, "registry", path_inject, pattern)
                        )
            except Exception as exc:
                print(
                    "[defaults] WARNING: failed to collect io.http.route from "
                    "InterfaceRegistry – " + str(exc)
                )

        if registry_routes:
            self._routes = registry_routes + build_always_available_http_routes(self)
            print(
                "[defaults] Route registry: loaded "
                + str(len(registry_routes))
                + " routes from InterfaceRegistry"
            )
            return

        # ---- Fallback: registry-defined compatibility routes ----
        print("[defaults] Route registry: no routes found, using fallback")
        self._routes.extend(build_fallback_http_routes(self))

    def start(self):
        _RequestHandler.server_ref = self
        self._server = http.server.ThreadingHTTPServer((self.host, self.port), _RequestHandler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=False)
        self._thread.start()
        print("[defaults] HTTP server started on " + self.host + ":" + str(self.port))

    def stop(self):
        if self._server is not None:
            self._server.shutdown()
            self._server = None
            self._thread = None

    def _match_route(self, method, path):
        """Match *method* + *path* against the route table.

        Returns ``(handler, path_params, source, path_inject, pattern)`` or
        ``(None, None, None, None, None)`` when nothing matches.
        """
        for route in self._routes:
            if len(route) >= 6:
                route_method, compiled, handler, source, path_inject, pattern = route[:6]
            else:
                route_method, compiled, handler, source, path_inject = route
                pattern = getattr(
                    handler, "__rumi_route_pattern__", getattr(compiled, "pattern", path)
                )
            if route_method != method:
                continue
            m = compiled.match(path)
            if m is not None:
                return handler, m.groupdict(), source, path_inject, pattern
        if self._is_root_shell_chunk_compat_route(method, path):
            return self._handle_static_file, {"path": str(path or "").lstrip("/")}, "fallback", {}, ""
        if self._is_spa_shell_fallback_route(method, path):
            return self._handle_static, {}, "fallback", {}, ""
        return None, None, None, None, None

    @staticmethod
    def _is_root_shell_chunk_compat_route(method, path):
        if str(method or "").upper() != "GET":
            return False
        request_path = str(path or "")
        if "/" in request_path.lstrip("/"):
            return False
        return re.fullmatch(r"/shell-[A-Za-z0-9._-]+\.js", request_path) is not None

    @staticmethod
    def _is_spa_shell_fallback_route(method, path):
        if str(method or "").upper() != "GET":
            return False
        request_path = str(path or "")
        if not request_path.startswith("/"):
            return False
        if request_path == "/" or request_path.startswith(("/api", "/static/")):
            return False
        leaf = request_path.rsplit("/", 1)[-1]
        if "." in leaf:
            return False
        first_segment = request_path.strip("/").split("/", 1)[0]
        return first_segment in {"chat", "coding", "calendar", "desktops", "prompts", "defaultspack", "pack"}

    def _active_profile_policy(self):
        try:
            from core_runtime.resolved_profile_scope import persisted_resolved_profile
        except Exception:
            return None, {}
        plan = persisted_resolved_profile()
        if plan is None:
            return None, {}
        # Route authority is enforced by v4 contract routing/Authority Kernel;
        # mutable Profile YAML policy is deliberately not consulted here.
        return str(plan.profile_id), {}

    def _route_allowed_by_active_profile(self, method, pattern):
        profile_id, policy = self._active_profile_policy()
        if not profile_id or not isinstance(policy, dict):
            return True
        if not bool(policy.get("enforce_api_route_allowlist", False)):
            return True
        allowlist = policy.get("api_route_allowlist")
        if isinstance(allowlist, str):
            allowlist = [item.strip() for item in allowlist.split(",") if item.strip()]
        if not isinstance(allowlist, list):
            allowlist = []
        route_key = f"{str(method or '').upper()} {str(pattern or '').strip()}"
        return route_key in {str(item).strip() for item in allowlist if str(item or "").strip()}

    def _record_profile_blocked_route(self, method, pattern):
        profile_id, _policy = self._active_profile_policy()
        if not profile_id:
            return
        try:
            from core_runtime.ai_input_trace_store import AiInputTraceStore

            AiInputTraceStore().append_blocked_event(
                profile_id,
                {
                    "event": "api_route_blocked",
                    "method": str(method or "").upper(),
                    "route": str(pattern or "").strip(),
                    "reason": "not_in_api_route_allowlist",
                    "source": "defaultspack.transport.http",
                },
            )
        except Exception:
            pass

    def _build_context(self):
        context = {
            "flow_id": "transport_direct",
            "step_id": "http_request",
            "phase": "execute",
            "ts": timestamp(),
            "owner_pack": "defaultspack",
            "inputs": {},
        }
        try:
            from core_runtime.di_container import get_container

            context["interface_registry"] = get_container().get(
                "interface_registry"
            )
        except Exception:
            # Standalone compatibility mode has no kernel registry.
            pass
        return context

    def _invoke_registry_handler(self, handler, request_data, path_params):
        if getattr(handler, "_defaultspack_flow_route_handler", False):
            return handler(request_data, path_params or {})
        context = self._build_context()
        context["_facade"] = self.facade
        _apply_authenticated_principal_context(context, request_data)
        _apply_ambient_browser_qa_context(context, request_data)
        _apply_defaultspack_local_ui_context(context, request_data)
        return handler(request_data, context)

    def _invoke_fallback_block(self, module_name, request_data, path_params, inject=None):
        payload = dict(request_data or {})
        for source_key, dest_key in (inject or {}).items():
            payload[dest_key] = path_params.get(source_key, "")
        context = self._build_context()
        _apply_authenticated_principal_context(context, payload)
        _apply_ambient_browser_qa_context(context, payload)
        _apply_defaultspack_local_ui_context(context, payload)
        if module_name == "blocks.chat.send":
            from core_runtime.di_container import get_container
            from core_runtime.global_contract_dispatch import invoke_global_contract

            session = get_container().get_or_none("v4_dispatch_session")
            if session is None:
                return error(
                    "Captured Pack v4 session is unavailable",
                    "V4_SESSION_UNAVAILABLE",
                )
            request = {
                key: payload[key]
                for key in (
                    "model",
                    "messages",
                    "tools",
                    "params",
                    "context",
                    "runtime_context",
                    "timezone",
                )
                if key in payload
            }
            if not isinstance(request.get("messages"), list):
                content = str(payload.get("content") or payload.get("message") or "")
                if content:
                    request["messages"] = [{"role": "user", "content": content}]
            try:
                return invoke_global_contract(
                    session,
                    "conversation.turn.v1",
                    "complete",
                    request,
                )
            except Exception as exc:
                return error(str(exc), "V4_CONVERSATION_FAILED")
        if module_name.startswith("blocks.chat."):
            return error(
                "Chat operation is absent from the captured Pack v4 catalog",
                "V4_OPERATION_UNAVAILABLE",
            )
        if module_name in _IN_PROCESS_HTTP_FALLBACK_BLOCKS:
            context["_defaultspack_http_route_adapter"] = True
            return invoke_block(module_name, payload, context)
        if module_name in _DIRECT_HTTP_COMPATIBILITY_BLOCKS:
            context["_defaultspack_http_route_adapter"] = True
            return invoke_block(module_name, payload, context)
        if (
            module_name in _DIRECT_SAFE_GET_FALLBACK_BLOCKS
            and self._safe_get_fallback_allowed(module_name, payload)
        ):
            context["_defaultspack_http_route_adapter"] = True
            return self._invoke_safe_get_fallback_block(module_name, payload, context)
        # Standalone live-server scripts start transport with no kernel facade.
        # In that mode, capability bridge resolution can block while trying to
        # discover runtime services that do not exist. Call the block directly.
        if hasattr(self, "facade") and self.facade is None:
            return invoke_block(module_name, payload, context)
        return error(
            "Operation is absent from the captured Pack v4 catalog",
            "V4_OPERATION_UNAVAILABLE",
        )

    def _invoke_flow_route(
        self,
        flow_id,
        request_data,
        path_params,
        inject=None,
        *,
        fallback_block_module="",
    ):
        # A live SSE iterator cannot cross the subprocess function boundary:
        # serializing it either stringifies the generator or waits for the
        # complete agent run before the HTTP response starts. Keep the
        # declarative flow available to non-HTTP callers, while the HTTP
        # adapter uses the compatibility block that preserves incremental
        # tool-selection, tool-call, and assistant events.
        if flow_id in {
            "defaultspack.chat_turn",
            "defaultspack.chat_stream_turn",
        } and fallback_block_module in {
            "blocks.chat.send",
            "blocks.chat.stream",
        }:
            return self._invoke_fallback_block(
                fallback_block_module,
                request_data,
                path_params,
                inject,
            )
        payload = dict(request_data or {})
        for source_key, dest_key in (inject or {}).items():
            payload[dest_key] = path_params.get(source_key, "")
        context = self._build_context()
        _apply_authenticated_principal_context(context, payload)
        context["flow_id"] = flow_id
        context["_defaultspack_http_route_adapter"] = True
        try:
            from domain.flow import FlowEngine

            flow_result = FlowEngine().execute(flow_id, payload, context)
            if flow_result.is_success():
                if self._flow_http_output_is_compatible(
                    flow_id,
                    flow_result.output,
                    fallback_block_module=fallback_block_module,
                ):
                    return flow_result.output
                if not fallback_block_module:
                    return flow_result.output
            elif not fallback_block_module:
                return flow_result.output
        except Exception as exc:
            if not fallback_block_module:
                return error(str(exc), "FLOW_ROUTE_FAILED")
        return self._invoke_fallback_block(
            fallback_block_module,
            request_data,
            path_params,
            inject,
        )

    def _invoke_function_route(
        self,
        function_name,
        request_data,
        path_params,
        inject=None,
        *,
        fallback_block_module="",
    ):
        payload = dict(request_data or {})
        for source_key, dest_key in (inject or {}).items():
            payload[dest_key] = path_params.get(source_key, "")
        context = self._build_context()
        _apply_authenticated_principal_context(context, payload)
        context["flow_id"] = "transport_function_route"
        context["_defaultspack_http_route_adapter"] = True
        _apply_ambient_browser_qa_context(context, payload)
        _apply_defaultspack_local_ui_context(context, payload)
        if context.get("_tool_server_approved") is True:
            logger.info(
                "defaultspack function route approved by local UI context: function=%s source=%s approval_id=%s",
                function_name,
                context.get("source"),
                context.get("approval_id"),
            )
        if fallback_block_module and hasattr(self, "facade") and self.facade is None:
            return self._invoke_fallback_block(
                fallback_block_module,
                request_data,
                path_params,
                inject,
            )
        if fallback_block_module in _IN_PROCESS_HTTP_FALLBACK_BLOCKS:
            return invoke_block(fallback_block_module, payload, context)
        if fallback_block_module in _DIRECT_HTTP_COMPATIBILITY_BLOCKS:
            return invoke_block(fallback_block_module, payload, context)
        if fallback_block_module and self._safe_get_fallback_allowed(fallback_block_module, payload):
            return self._invoke_safe_get_fallback_block(fallback_block_module, payload, context)
        return error(
            f"Operation {function_name!r} is absent from the captured v4 catalog",
            "V4_OPERATION_UNAVAILABLE",
        )

    @staticmethod
    def _flow_http_output_is_compatible(flow_id, output, *, fallback_block_module=""):
        return flow_http_output_is_compatible(
            flow_id,
            output,
            fallback_block_module=fallback_block_module,
        )

    @staticmethod
    def _fallback_function_timeout_seconds(module_name, payload):
        explicit = payload.get("timeout_seconds") if isinstance(payload, dict) else None
        if explicit not in (None, ""):
            try:
                return max(1.0, float(str(explicit)))
            except (TypeError, ValueError):
                pass
        env_value = os.environ.get("RUMI_DEFAULTSPACK_HTTP_FALLBACK_TIMEOUT_SECONDS", "").strip()
        if env_value:
            try:
                return max(1.0, float(env_value))
            except ValueError:
                pass
        if module_name in _LONG_RUNNING_FALLBACK_BLOCKS:
            if module_name == "blocks.agent.scheduler.trigger":
                return _SCHEDULE_TRIGGER_FALLBACK_TIMEOUT_SECONDS
            return _LONG_RUNNING_FALLBACK_TIMEOUT_SECONDS
        return None

    @staticmethod
    def _payload_with_fallback_timeout(payload, timeout_seconds):
        if timeout_seconds is None:
            return payload
        if not isinstance(payload, dict) or "timeout_seconds" in payload:
            return payload
        enriched = dict(payload)
        enriched["timeout_seconds"] = timeout_seconds
        return enriched

    @staticmethod
    def _safe_get_fallback_timeout_seconds():
        env_value = os.environ.get("RUMI_DEFAULTSPACK_SAFE_GET_TIMEOUT_SECONDS", "").strip()
        if env_value:
            try:
                return max(0.1, float(env_value))
            except ValueError:
                pass
        return _DIRECT_SAFE_GET_FALLBACK_TIMEOUT_SECONDS

    def _invoke_safe_get_fallback_block(self, module_name, payload, context):
        timeout_seconds = self._safe_get_fallback_timeout_seconds()
        result_queue: queue.Queue[tuple[str, Any]] = queue.Queue(maxsize=1)

        def _target():
            try:
                result_queue.put(("ok", invoke_block(module_name, payload, context)))
            except Exception as exc:
                result_queue.put(("error", exc))

        worker = threading.Thread(
            target=_target,
            name=f"defaultspack-safe-get-{str(module_name).rsplit('.', 1)[-1]}",
            daemon=True,
        )
        worker.start()
        try:
            status, result = result_queue.get(timeout=timeout_seconds)
        except queue.Empty:
            logger.warning(
                "defaultspack safe GET fallback timed out: module=%s timeout_seconds=%s",
                module_name,
                timeout_seconds,
            )
            return error(
                f"bootstrap API timed out after {timeout_seconds:.1f}s",
                "BOOTSTRAP_API_TIMEOUT",
            )
        if status == "error":
            return error(str(result), "BOOTSTRAP_API_FAILED")
        return result

    def _safe_get_fallback_allowed(self, module_name, payload):
        actual_method = str(payload.get("_actual_method") or "").upper()
        return actual_method == "GET" and module_name in _SAFE_GET_FALLBACK_BLOCKS

    # ---- Chat Handlers (fallback) ----

    def _handle_chat_send(self, request_data, path_params):
        return self._invoke_fallback_block("blocks.chat.send", request_data, path_params)

    def _handle_chat_create(self, request_data, path_params):
        return self._invoke_fallback_block(
            "blocks.chat.create_conversation", request_data, path_params
        )

    def _handle_chat_list(self, request_data, path_params):
        return self._invoke_fallback_block(
            "blocks.chat.list_conversations", request_data, path_params
        )

    def _handle_chat_get(self, request_data, path_params):
        return self._invoke_fallback_block(
            "blocks.chat.get_conversation",
            request_data,
            path_params,
            {"id": "conversation_id"},
        )

    def _handle_chat_update(self, request_data, path_params):
        return self._invoke_fallback_block(
            "blocks.chat.update_conversation",
            request_data,
            path_params,
            {"id": "conversation_id"},
        )

    def _handle_chat_delete(self, request_data, path_params):
        return self._invoke_fallback_block(
            "blocks.chat.delete_conversation",
            request_data,
            path_params,
            {"id": "conversation_id"},
        )

    def _handle_chat_send_message(self, request_data, path_params):
        return self._invoke_fallback_block(
            "blocks.chat.send",
            request_data,
            path_params,
            {"id": "conversation_id"},
        )

    def _handle_chat_stream(self, request_data, path_params):
        return self._invoke_fallback_block(
            "blocks.chat.stream",
            request_data,
            path_params,
            {"id": "conversation_id"},
        )

    def _handle_chat_export(self, request_data, path_params):
        return self._invoke_fallback_block(
            "blocks.chat.export_conversation",
            request_data,
            path_params,
            {"id": "conversation_id"},
        )

    def _handle_chat_summarize(self, request_data, path_params):
        return self._invoke_fallback_block(
            "blocks.chat.summarize_and_trim",
            request_data,
            path_params,
            {"id": "conversation_id"},
        )

    def _handle_chat_auto_trim(self, request_data, path_params):
        return self._invoke_fallback_block(
            "blocks.chat.auto_trim",
            request_data,
            path_params,
            {"id": "conversation_id"},
        )

    # ---- Agent Handlers (fallback) ----

    def _handle_agent_execute(self, request_data, path_params):
        return self._invoke_fallback_block("blocks.agent.execute", request_data, path_params)

    def _handle_agent_approve(self, request_data, path_params):
        return self._invoke_fallback_block(
            "blocks.agent.approve",
            request_data,
            path_params,
            {"id": "execution_id"},
        )

    def _handle_agent_reject(self, request_data, path_params):
        return self._invoke_fallback_block(
            "blocks.agent.reject",
            request_data,
            path_params,
            {"id": "execution_id"},
        )

    def _handle_agent_cancel(self, request_data, path_params):
        return self._invoke_fallback_block(
            "blocks.agent.cancel",
            request_data,
            path_params,
            {"id": "execution_id"},
        )

    def _handle_agent_status(self, request_data, path_params):
        return self._invoke_fallback_block(
            "blocks.agent.status",
            request_data,
            path_params,
            {"id": "execution_id"},
        )

    # ---- Multi-Agent Handlers (fallback, Group 8) ----

    def _handle_multi_execute(self, request_data, path_params):
        return self._invoke_fallback_block("blocks.agent.multi_execute", request_data, path_params)

    def _handle_multi_status(self, request_data, path_params):
        return self._invoke_fallback_block(
            "blocks.agent.multi_status",
            request_data,
            path_params,
            {"id": "session_id"},
        )

    def _handle_multi_message(self, request_data, path_params):
        return self._invoke_fallback_block(
            "blocks.agent.multi_message",
            request_data,
            path_params,
            {"id": "session_id"},
        )

    # ---- Instruction Handler (fallback, Group 8) ----

    def _handle_agent_instruct(self, request_data, path_params):
        return self._invoke_fallback_block(
            "blocks.agent.add_instruction",
            request_data,
            path_params,
            {"id": "execution_id"},
        )

    # ---- Consent Handlers (fallback, Group 8) ----

    def _handle_consent_check(self, request_data, path_params):
        return self._invoke_fallback_block("blocks.tool.consent_check", request_data, path_params)

    def _handle_consent_confirm(self, request_data, path_params):
        return self._invoke_fallback_block(
            "blocks.tool.consent_confirm",
            request_data,
            path_params,
            {"id": "consent_id"},
        )

    # ---- Knowledge Handlers (fallback, Group 9a) ----

    def _handle_knowledge_create(self, request_data, path_params):
        return self._invoke_fallback_block("blocks.knowledge.create", request_data, path_params)

    def _handle_knowledge_list(self, request_data, path_params):
        return self._invoke_fallback_block("blocks.knowledge.list", request_data, path_params)

    def _handle_knowledge_search(self, request_data, path_params):
        return self._invoke_fallback_block("blocks.knowledge.search", request_data, path_params)

    def _handle_knowledge_get(self, request_data, path_params):
        return self._invoke_fallback_block(
            "blocks.knowledge.get",
            request_data,
            path_params,
            {"id": "id"},
        )

    def _handle_knowledge_update(self, request_data, path_params):
        return self._invoke_fallback_block(
            "blocks.knowledge.update",
            request_data,
            path_params,
            {"id": "id"},
        )

    def _handle_knowledge_delete(self, request_data, path_params):
        return self._invoke_fallback_block(
            "blocks.knowledge.delete",
            request_data,
            path_params,
            {"id": "id"},
        )

    # ---- Dynamic Tool Handlers (fallback) ----

    def _handle_tool_create(self, request_data, path_params):
        return self._invoke_fallback_block("blocks.tool.create", request_data, path_params)

    def _handle_tool_update(self, request_data, path_params):
        return self._invoke_fallback_block(
            "blocks.tool.update",
            request_data,
            path_params,
            {"name": "name"},
        )

    def _handle_tool_delete(self, request_data, path_params):
        return self._invoke_fallback_block(
            "blocks.tool.delete",
            request_data,
            path_params,
            {"name": "name"},
        )

    def _handle_tool_export(self, request_data, path_params):
        return self._invoke_fallback_block(
            "blocks.tool.export",
            request_data,
            path_params,
            {"name": "name"},
        )

    # ---- Dev Tool Handlers (fallback, P1-1) ----

    def _handle_dev_inspect(self, request_data, path_params):
        return self._invoke_fallback_block("blocks.dev.inspect", request_data, path_params)

    def _handle_dev_prompt_history(self, request_data, path_params):
        return self._invoke_fallback_block("blocks.dev.prompt_history", request_data, path_params)

    def _handle_dev_edit_prompt(self, request_data, path_params):
        return self._invoke_fallback_block("blocks.dev.edit_prompt_live", request_data, path_params)

    def _handle_dev_replay(self, request_data, path_params):
        return self._invoke_fallback_block("blocks.dev.replay", request_data, path_params)

    # ---- System Handlers (fallback) ----

    def _handle_desktop_system_info(self, request_data, path_params):
        del request_data, path_params
        if sys.platform == "darwin":
            try:
                from ecosystem.defaultspack.domain.host_bridge.viewer_broker_client import (
                    ViewerBrokerClient,
                )

                client = ViewerBrokerClient.from_environment()
                if client.available():
                    payload = client.permissions()
                    permissions = payload.get("permissions")
                    if isinstance(permissions, list):
                        host_broker = payload.get("host_broker") or {
                            "enabled": True,
                            "available": True,
                            "status": "running",
                        }
                        reliable = (
                            bool(host_broker.get("available"))
                            if isinstance(host_broker, dict)
                            else False
                        )
                        return ok(
                            {
                                "source": "viewer_broker",
                                "reliable": reliable,
                                "app_name": "Rumi AI",
                                "display_version": "",
                                "viewer_version": "",
                                "build_channel": "beta",
                                "platform": sys.platform,
                                "platform_release": _platform_release(),
                                "permission_subject": str(
                                    payload.get("permission_subject") or "Rumi Viewer"
                                ),
                                "host_broker": host_broker,
                                "host_permissions": (
                                    payload.get("host_permissions")
                                    if isinstance(payload.get("host_permissions"), list)
                                    else []
                                ),
                                "permissions": permissions,
                            }
                        )
            except Exception:
                pass

            return ok(
                {
                    "source": "fallback",
                    "reliable": False,
                    "app_name": "Rumi AI",
                    "display_version": "",
                    "viewer_version": "",
                    "build_channel": "beta",
                    "platform": sys.platform,
                    "platform_release": _platform_release(),
                    "permission_subject": "Rumi Viewer",
                    "host_broker": {
                        "enabled": False,
                        "available": False,
                        "status": "unavailable",
                        "recovery": "Open Rumi Viewer and grant macOS permissions there.",
                    },
                    "host_permissions": [],
                    "permissions": [],
                }
            )

        permissions = []
        permissions.append(
            {
                "id": "macos_privacy",
                "label": "macOS Privacy",
                "status": "unsupported",
                "granted": None,
                "detail": "macOS permission checks are only available on macOS.",
                "settings_hint": "",
            }
        )

        return ok(
            {
                "source": "fallback",
                "reliable": False,
                "app_name": "Rumi AI",
                "display_version": "",
                "viewer_version": "",
                "build_channel": "beta",
                "platform": sys.platform,
                "platform_release": _platform_release(),
                "permission_subject": "Rumi Viewer",
                "host_broker": {
                    "enabled": False,
                    "available": False,
                    "status": "unsupported",
                },
                "host_permissions": [],
                "permissions": permissions,
            }
        )

    @staticmethod
    def _authority_http_error(result, default_code="AUTHORITY_ERROR"):
        response = error(str(result.get("error") or "authority request failed"), default_code)
        response["_http_status"] = int(result.get("status_code") or 400)
        return response

    def _handle_authority_requests(self, request_data, path_params):
        del request_data, path_params
        try:
            _removed_authority_boundary()
        except Exception as exc:
            return error("authority service unavailable: " + str(exc), "AUTHORITY_UNAVAILABLE")

    def _handle_authority_request(self, request_data, path_params):
        del request_data, path_params
        try:
            _removed_authority_boundary()
        except Exception as exc:
            return error("authority service unavailable: " + str(exc), "AUTHORITY_UNAVAILABLE")

    def _handle_authority_test_request(self, request_data, path_params):
        del path_params
        from core_runtime.host_contract import host_contract_value

        if host_contract_value("authority_test_endpoint").strip().lower() not in {
            "1",
            "true",
            "yes",
        }:
            response = error("authority test endpoint is disabled", "AUTHORITY_TEST_DISABLED")
            response["_http_status"] = 404
            return response

        try:
            _removed_authority_boundary()
        except Exception as exc:
            return error("authority service unavailable: " + str(exc), "AUTHORITY_UNAVAILABLE")

    def _handle_authority_browser_exchange(self, request_data, path_params):
        del path_params
        legacy = _legacy_browser_credential_error(request_data)
        if legacy:
            return legacy
        return _http_error(
            "browser approval exchange is unavailable; use the native approval window",
            "AUTHORITY_BROWSER_TEST_DISABLED",
            404,
        )

    def _handle_authority_browser_exchange_revoke(self, request_data, path_params):
        del path_params
        legacy = _legacy_browser_credential_error(request_data)
        if legacy:
            return legacy
        return _http_error(
            "browser approval exchange is unavailable; use the native approval window",
            "AUTHORITY_BROWSER_TEST_DISABLED",
            404,
        )

    def _handle_authority_browser_ui_operator(self, request_data, path_params):
        del path_params
        legacy = _legacy_browser_credential_error(request_data)
        if legacy:
            return legacy
        return _http_error(
            "browser ui_operator minting is unavailable; use the native approval window",
            "AUTHORITY_BROWSER_TEST_DISABLED",
            404,
        )

    def _handle_authority_approve(self, request_data, path_params):
        del request_data, path_params
        try:
            _removed_authority_boundary()
        except Exception as exc:
            return error("authority service unavailable: " + str(exc), "AUTHORITY_UNAVAILABLE")

    def _handle_authority_challenge(self, request_data, path_params):
        del request_data, path_params
        try:
            _removed_authority_boundary()
        except Exception as exc:
            return error("authority service unavailable: " + str(exc), "AUTHORITY_UNAVAILABLE")

    def _handle_authority_deny(self, request_data, path_params):
        del request_data, path_params
        try:
            _removed_authority_boundary()
        except Exception as exc:
            return error("authority service unavailable: " + str(exc), "AUTHORITY_UNAVAILABLE")

    def _handle_health(self, request_data, path_params):
        return ok(
            {
                "status": "healthy",
                "pack": "defaultspack",
                "ts": timestamp(),
            }
        )

    def _handle_context_info(self, request_data, path_params):
        interfaces = {}
        if self.facade is not None:
            try:
                interfaces = self.facade.list_interfaces()
            except Exception:
                interfaces = {}
        return ok(
            {
                "pack": "defaultspack",
                "interfaces": interfaces,
                "ts": timestamp(),
            }
        )

    # ---- Static Handlers (fallback) ----

    def _handle_chat_redirect(self, request_data, path_params):
        query = urllib.parse.urlencode(
            {
                key: value
                for key, value in (request_data or {}).items()
                if not str(key).startswith("_")
            }
        )
        location = "/chat" + (("?" + query) if query else "")
        return {"_redirect": True, "location": location, "status_code": 302}

    def _handle_static(self, request_data, path_params):
        shell_path = os.path.join(os.path.dirname(__file__), "..", "ui", "shell.html")
        if os.path.isfile(shell_path):
            with open(shell_path, "r", encoding="utf-8") as f:
                body = f.read()
            ui_dir = os.path.dirname(shell_path)
            for asset_name in ("shell-app.css", "shell-app.js"):
                asset_path = os.path.join(ui_dir, asset_name)
                if os.path.isfile(asset_path):
                    version = str(int(os.path.getmtime(asset_path)))
                    body = body.replace(
                        f"/static/{asset_name}", f"/static/{asset_name}?v={version}"
                    )
            return {"_static": True, "content_type": "text/html; charset=utf-8", "body": body}
        return {
            "_static": True,
            "content_type": "text/html; charset=utf-8",
            "body": "<!DOCTYPE html><html><body><h1>defaults pack</h1><p>shell.html not found</p></body></html>",
        }

    def _handle_static_file(self, request_data, path_params):
        rel_path = path_params.get("path", "")
        safe_path = os.path.normpath(str(rel_path or "").replace("\\", os.sep))
        if (
            safe_path in ("", ".")
            or safe_path == ".."
            or safe_path.startswith(".." + os.sep)
            or os.path.isabs(safe_path)
        ):
            return error("invalid path")
        pack_root = os.path.normpath(os.path.join(os.path.dirname(__file__), ".."))
        candidate_paths = [os.path.join(pack_root, "ui", safe_path)]
        if (
            safe_path == "assets" or safe_path.startswith("assets" + os.sep)
        ):
            candidate_paths.append(os.path.join(pack_root, safe_path))
        candidate_paths.append(os.path.join(pack_root, "webapp", "public", safe_path))
        file_path = next(
            (candidate for candidate in candidate_paths if os.path.isfile(candidate)),
            "",
        )
        if not os.path.isfile(file_path):
            return error("file not found: " + rel_path)
        ext = os.path.splitext(file_path)[1].lower()
        content_types = {
            ".html": "text/html; charset=utf-8",
            ".css": "text/css; charset=utf-8",
            ".js": "application/javascript; charset=utf-8",
            ".json": "application/json; charset=utf-8",
            ".png": "image/png",
            ".jpg": "image/jpeg",
            ".jpeg": "image/jpeg",
            ".gif": "image/gif",
            ".svg": "image/svg+xml",
            ".ico": "image/x-icon",
            ".wasm": "application/wasm",
        }
        ct = content_types.get(ext, "application/octet-stream")
        body: str | bytes
        if ct.startswith("text/") or ct.startswith("application/j"):
            with open(file_path, "r", encoding="utf-8") as f:
                body = f.read()
        else:
            with open(file_path, "rb") as f:
                body = f.read()
        return {"_static": True, "content_type": ct, "body": body}


_SENSITIVE_CODING_PATHS = set(SENSITIVE_CODING_PATHS) | set(METHOD_SENSITIVE_CODING_PATHS)

_SENSITIVE_INTEGRATION_PATHS = {
    "/api/integrations/secrets",
    "/api/integrations/p2p/events",
    "/api/external/tokens",
    "/api/external/sources",
    "/api/recording/devices",
    "/api/recording/capture",
}
_SENSITIVE_INTEGRATION_METHOD_PATHS = {
    "/api/ai/provider-key": {"POST"},
    "/api/connections/codex": {"POST"},
    "/api/connections/import": {"POST"},
    "/api/ambient/events": {"POST"},
    "/api/ambient/monitor/start": {"POST"},
    "/api/external/templates": {"POST", "PUT", "DELETE"},
}
_SENSITIVE_INTEGRATION_PREFIXES = (
    "/api/p2p",
    "/api/webhooks/endpoints",
    "/api/webhooks/public-urls",
)
_SENSITIVE_CHAT_PATH_RE = re.compile(
    r"^/v1/conversations/[^/]+/run-results/[^/]+/browser-screenshots$"
)
_SENSITIVE_HUMAN_OPERATOR_PATH_RE = re.compile(
    r"^/api/human-operator/conversations/[^/]+/sessions/[^/]+(?:/messages)?$"
)
_COMPOSER_TRANSCRIPTION_PATH = "/api/ambient/transcriptions"
# A 25 MiB recording is approximately 33.4 MiB when base64 encoded.  Leave a
# small envelope for JSON fields while rejecting large bodies before buffering
# or decoding them in the HTTP handler.
_COMPOSER_TRANSCRIPTION_MAX_REQUEST_BYTES = 36 * 1024 * 1024
_AMBIENT_BROWSER_QA_CONTEXT_FLAG = "_ambient_browser_qa_pre_auth_approved"
_LOCAL_UI_APPROVAL_CONTEXT_FLAG = "_defaultspack_local_ui_pre_auth_approved"
_LOCAL_UI_AUTH_CONTEXT_FLAG = "_defaultspack_local_ui_authenticated"
_LOCAL_UI_APPROVAL_METHOD_PATHS = {
    "/api/ai/provider-key": {"POST"},
    "/api/agent/subagent": {"POST"},
    "/api/connections/codex": {"POST"},
    "/api/connections/import": {"POST"},
    "/api/ambient/events": {"POST"},
    "/api/ambient/monitor/start": {"POST"},
    "/api/runtime/ensure": {"POST"},
    "/api/runtime/update": {"POST"},
    "/api/runtime/uninstall": {"POST"},
    "/api/desktops": {"POST"},
    "/api/onboarding/apply": {"POST"},
    "/api/ui/capability/invoke": {"POST"},
}
_LOCAL_UI_APPROVAL_METHOD_PATTERNS = (
    (re.compile(r"^/api/prompts(?:/.*)?$"), {"POST", "PUT", "DELETE"}),
    (re.compile(r"^/api/runtime/operations/[^/]+/cancel$"), {"POST"}),
    (re.compile(r"^/api/desktops/[^/]+$"), {"DELETE"}),
    (re.compile(r"^/api/desktops/[^/]+/(?:start|restart|stop|input|ai-input|rules)$"), {"POST"}),
    (re.compile(r"^/api/desktops/[^/]+/access-requests/[^/]+/grant$"), {"POST"}),
    (re.compile(r"^/api/desktops/[^/]+/control/(?:acquire|renew|release)$"), {"POST"}),
)

_LOCAL_ORIGIN_HOSTS = {"127.0.0.1", "localhost", "::1"}


def _is_sensitive_coding_path(path):
    return _local_is_sensitive_coding_path(path)


def _matches_sensitive_prefix(path):
    return any(
        path == prefix or path.startswith(prefix + "/")
        for prefix in _SENSITIVE_INTEGRATION_PREFIXES
    )


def _requires_sensitive_http_auth(method, path):
    method = str(method or "").upper()
    if path in _SENSITIVE_INTEGRATION_PATHS:
        return True
    if method in _SENSITIVE_INTEGRATION_METHOD_PATHS.get(path, set()):
        return True
    if _matches_sensitive_prefix(path):
        return True
    if _SENSITIVE_CHAT_PATH_RE.match(path) is not None:
        return True
    return False


def _is_sensitive_http_path(path):
    return (
        _is_sensitive_coding_path(path)
        or path in _SENSITIVE_INTEGRATION_PATHS
        or path in _SENSITIVE_INTEGRATION_METHOD_PATHS
        or _matches_sensitive_prefix(path)
        or _SENSITIVE_CHAT_PATH_RE.match(path) is not None
        or _SENSITIVE_HUMAN_OPERATOR_PATH_RE.match(path) is not None
    )


def _is_allowed_sensitive_origin(origin):
    return _local_origin_allowed(origin)


def _header_value(headers, name):
    if not headers:
        return ""
    try:
        value = headers.get(name, "")
    except AttributeError:
        value = ""
    if value:
        return str(value)
    lowered = str(name).lower()
    try:
        items = headers.items()
    except AttributeError:
        return ""
    for key, value in items:
        if str(key).lower() == lowered:
            return str(value)
    return ""


def _is_browser_accessible_api_path(path):
    normalized = str(path or "")
    return (
        normalized == "/api"
        or normalized.startswith("/api/")
        or normalized == "/v1"
        or normalized.startswith("/v1/")
    )


def _browser_api_origin_error(method, path, headers, client_address=None):
    del method, client_address
    if not _is_browser_accessible_api_path(path):
        return None
    origin = _header_value(headers, "Origin")
    if origin and not _is_allowed_sensitive_origin(origin):
        return (403, "origin not allowed for local defaultspack API", "ORIGIN_DENIED")
    return None


def _normalized_host_and_port(value, *, scheme="http"):
    """Parse a Host header or Origin and reject ambiguous authority values."""
    raw_value = str(value or "").strip()
    if not raw_value or raw_value.startswith("//"):
        return None
    try:
        parsed = urllib.parse.urlsplit(raw_value if "://" in raw_value else "//" + raw_value)
        port = parsed.port
    except (TypeError, ValueError):
        return None
    if (
        parsed.username is not None
        or parsed.password is not None
        or not parsed.hostname
        or parsed.path
        or parsed.query
        or parsed.fragment
    ):
        return None
    normalized_scheme = str(parsed.scheme or scheme).lower()
    if normalized_scheme != "http":
        return None
    hostname = str(parsed.hostname).lower()
    if not _local_origin_allowed(f"http://{parsed.netloc or raw_value}"):
        return None
    return hostname, port if port is not None else 80


def _composer_transcription_request_error(headers, client_address):
    """Authorize the narrow, unauthenticated same-origin transcription route.

    This is deliberately separate from privileged integration authorization:
    the static composer needs to work when opened directly from the local
    server, but a page hosted on a different localhost port must not be able to
    consume CPU by submitting recordings.  A direct TCP caller must also be
    loopback and the Host/Origin pair must be the exact same HTTP origin.
    """
    if not _local_is_loopback_request(
        {str(key): str(value) for key, value in getattr(headers, "items", lambda: [])()},
        client_address,
    ):
        return (403, "composer transcription requires a loopback client", "LOCAL_ONLY_REQUIRED")
    origin = _header_value(headers, "Origin")
    host = _header_value(headers, "Host")
    origin_parts = _normalized_host_and_port(origin)
    host_parts = _normalized_host_and_port(host)
    if not origin_parts or not host_parts:
        return (
            403,
            "composer transcription requires a valid loopback same-origin request",
            "ORIGIN_DENIED",
        )
    if origin_parts != host_parts:
        return (
            403,
            "composer transcription origin does not match the local server",
            "ORIGIN_DENIED",
        )
    return None


def _is_websocket_upgrade(headers):
    upgrade = _header_value(headers, "Upgrade").strip().lower()
    connection = _header_value(headers, "Connection").strip().lower()
    return upgrade == "websocket" and "upgrade" in connection


def _websocket_auth_error(headers, client_address=None):
    if not _is_websocket_upgrade(headers):
        return None
    if _local_is_loopback_request(
        {str(key): str(value) for key, value in getattr(headers, "items", lambda: [])()},
        client_address,
    ):
        return None
    if not _configured_local_auth_tokens():
        return (403, "websocket auth token is not configured", "AUTH_REQUIRED")
    if not _local_auth_token_authorized(headers):
        return (401, "websocket auth token required", "AUTH_REQUIRED")
    return None


def _configured_local_auth_tokens():
    tokens = []
    seen = set()

    def add_token(value):
        token = str(value or "").strip()
        if token and token not in seen:
            seen.add(token)
            tokens.append(token)

    add_token(host_contract_value("desktop_api_token"))
    for path in _local_auth_token_file_candidates():
        try:
            add_token(path.read_text(encoding="utf-8"))
        except OSError:
            continue
    return tokens


def _configured_local_auth_token():
    tokens = _configured_local_auth_tokens()
    return tokens[0] if tokens else ""


def _local_auth_token_file_candidates():
    candidates = []

    def add_candidate(path):
        if path and path not in candidates:
            candidates.append(path)

    for env_key in ("RUMI_APP_DIR", "RUMI_HOME"):
        value = os.environ.get(env_key, "").strip()
        if value:
            add_candidate(Path(value).expanduser() / ".desktop_api_token")
    user_data = os.environ.get("RUMI_USER_DATA", "").strip()
    if user_data:
        add_candidate(Path(user_data).expanduser().parent / ".desktop_api_token")
    return candidates


def _bearer_token(headers):
    auth_header = headers.get("Authorization", "")
    if not auth_header.lower().startswith("bearer "):
        return ""
    return auth_header[7:].strip()


def _local_auth_token_authorized(headers):
    provided = _bearer_token(headers)
    if not provided:
        return False
    return any(hmac.compare_digest(provided, expected) for expected in _configured_local_auth_tokens())


def _browser_exchange_transport_error(headers):
    """Require the authenticated HTTP origin that owns the local server."""
    if not _configured_local_auth_tokens():
        return (403, "local auth token is not configured", "AUTH_REQUIRED")
    if not _local_auth_token_authorized(headers):
        return (401, "local auth token required", "AUTH_REQUIRED")
    origin = _strict_local_origin(_header_value(headers, "Origin"))
    host_value = _header_value(headers, "Host").strip()
    try:
        host = urllib.parse.urlsplit("//" + host_value)
        origin_parts = urllib.parse.urlsplit(origin)
        host_port = host.port
        origin_port = origin_parts.port
    except (TypeError, ValueError):
        return (403, "browser exchange origin is invalid", "ORIGIN_DENIED")
    if (
        not origin
        or origin_parts.scheme != "http"
        or not host.hostname
        or host.username is not None
        or host.password is not None
        or origin_parts.hostname != host.hostname
        or origin_port != host_port
    ):
        return (
            403,
            "browser exchange origin does not match the local server",
            "ORIGIN_DENIED",
        )
    if not _header_value(headers, "X-Rumi-CSRF").strip():
        return (403, "CSRF header required", "CSRF_REQUIRED")
    return None


_BROWSER_QA_TOKEN_KEYS = (
    "browser_approval_token",
    "approval_browser_token",
    "browserApprovalToken",
)


def _legacy_browser_url_without_credentials(parsed_url):
    try:
        pairs = urllib.parse.parse_qsl(
            parsed_url.query, keep_blank_values=True, strict_parsing=False
        )
    except (TypeError, ValueError):
        return None
    if not any(key in _BROWSER_QA_TOKEN_KEYS for key, _value in pairs):
        return None
    clean_query = urllib.parse.urlencode(
        [(key, value) for key, value in pairs if key not in _BROWSER_QA_TOKEN_KEYS],
        doseq=True,
    )
    return urllib.parse.urlunsplit(
        ("", "", parsed_url.path or "/", clean_query, "")
    )


def _http_error(message, code, status):
    response = error(message, code)
    response["_http_status"] = int(status)
    return response


def _legacy_browser_credential_present(headers=None, payload=None):
    if _header_value(headers, "X-Rumi-Approval-Browser-Token").strip():
        return True
    return bool(_browser_qa_token_from_payload(payload))


def _legacy_browser_credential_error(payload):
    headers = payload.get("_headers") if isinstance(payload, dict) else {}
    if not _legacy_browser_credential_present(headers, payload):
        return None
    return _http_error(
        "legacy browser approval credentials have been revoked",
        "LEGACY_BROWSER_APPROVAL_REVOKED",
        410,
    )


def _strict_local_origin(value):
    origin = str(value or "").strip()
    if not origin or origin.startswith("//"):
        return ""
    try:
        parsed = urllib.parse.urlsplit(origin)
        port = parsed.port
    except (TypeError, ValueError):
        return ""
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path
        or parsed.query
        or parsed.fragment
        or not _is_allowed_sensitive_origin(origin)
    ):
        return ""
    host = f"[{parsed.hostname}]" if ":" in parsed.hostname else parsed.hostname
    return f"{parsed.scheme}://{host}{f':{port}' if port is not None else ''}"


def _bounded_browser_exchange_field(payload, key, *, maximum=256):
    value = str(payload.get(key) or "").strip()
    return value if 0 < len(value) <= maximum else ""


def _browser_exchange_audience(payload):
    from domain.safety.browser_approval_exchange import BrowserApprovalAudience

    if not isinstance(payload, dict):
        return None, _http_error("invalid exchange request", "INVALID_INPUT", 400)
    request_id = _bounded_browser_exchange_field(payload, "request_id")
    device_id = _bounded_browser_exchange_field(payload, "device_id", maximum=128)
    window_id = _bounded_browser_exchange_field(payload, "window_id", maximum=128)
    nonce = _bounded_browser_exchange_field(payload, "nonce", maximum=256)
    claimed_origin = _strict_local_origin(payload.get("origin"))
    headers = payload.get("_headers") if isinstance(payload.get("_headers"), dict) else {}
    header_origin = _strict_local_origin(_header_value(headers, "Origin"))
    subject = payload.get("_authority_subject")
    principal_id = (
        str(subject.get("principal_id") or "").strip()
        if isinstance(subject, dict)
        else ""
    )
    if not all((request_id, device_id, window_id, nonce, principal_id)):
        return None, _http_error(
            "request_id, device_id, window_id, nonce and authenticated principal are required",
            "BROWSER_EXCHANGE_BINDING_REQUIRED",
            400,
        )
    if not claimed_origin or not header_origin or claimed_origin != header_origin:
        return None, _http_error(
            "browser exchange origin is invalid or does not match",
            "BROWSER_EXCHANGE_ORIGIN_MISMATCH",
            403,
        )
    return BrowserApprovalAudience(
        request_id=request_id,
        principal_id=principal_id,
        device_id=device_id,
        origin=claimed_origin,
        window_id=window_id,
        nonce=nonce,
    ), None


def _browser_exchange_settlement_error(reason):
    normalized = str(reason or "invalid")
    if normalized in {"expired", "revoked"}:
        code = (
            "BROWSER_EXCHANGE_EXPIRED"
            if normalized == "expired"
            else "BROWSER_EXCHANGE_REVOKED"
        )
        return _http_error(f"browser approval exchange is {normalized}", code, 410)
    if normalized == "consumed":
        return _http_error(
            "browser approval exchange was already consumed",
            "BROWSER_EXCHANGE_REPLAYED",
            409,
        )
    if normalized == "audience_mismatch":
        return _http_error(
            "browser approval exchange audience does not match",
            "BROWSER_EXCHANGE_AUDIENCE_MISMATCH",
            403,
        )
    return _http_error(
        "browser approval exchange is invalid", "BROWSER_EXCHANGE_INVALID", 403
    )


def _browser_qa_token_from_payload(payload):
    if not isinstance(payload, dict):
        return ""
    for key in _BROWSER_QA_TOKEN_KEYS:
        value = str(payload.get(key) or "").strip()
        if value:
            return value
    return ""


def _local_ui_approval_route_authorized(method, path, headers, request_data=None):
    normalized_method = str(method or "").upper()
    normalized_path = str(path or "")
    allowed_methods = _LOCAL_UI_APPROVAL_METHOD_PATHS.get(normalized_path, set())
    if normalized_method not in allowed_methods and not any(
        normalized_method in pattern_methods and pattern.match(normalized_path)
        for pattern, pattern_methods in _LOCAL_UI_APPROVAL_METHOD_PATTERNS
    ):
        return False
    return _local_auth_token_authorized(headers) or _browser_qa_token_authorized(method, path, headers, request_data)


def _browser_qa_token_authorized(method, path, headers, request_data=None):
    del method, path, headers, request_data
    return False


def _ambient_browser_test_token_authorized(method, path, headers, request_data=None):
    return _browser_qa_token_authorized(method, path, headers, request_data)


def _apply_ambient_browser_qa_context(context, payload):
    if not isinstance(context, dict) or not isinstance(payload, dict):
        return
    if payload.pop(_AMBIENT_BROWSER_QA_CONTEXT_FLAG, False) is not True:
        return
    context["_tool_server_approved"] = True
    context["source"] = "ambient_browser_qa"
    context["approval_id"] = "ambient_browser_qa"


def _apply_defaultspack_local_ui_context(context, payload):
    if not isinstance(context, dict) or not isinstance(payload, dict):
        return
    if payload.pop(_LOCAL_UI_AUTH_CONTEXT_FLAG, False) is True:
        context[_LOCAL_UI_AUTH_CONTEXT_FLAG] = True
    if payload.pop(_LOCAL_UI_APPROVAL_CONTEXT_FLAG, False) is not True:
        return
    # This flag is only injected after a local bearer token has been verified
    # for a narrow, sensitive UI route.  Mark it with the unforgeable internal
    # sentinel as well: downstream approval checks deliberately reject a bare
    # client-visible boolean.
    from domain.tool_policy.internal_context import mark_tool_server_approval_context

    mark_tool_server_approval_context(context)
    context["source"] = "defaultspack_local_ui"
    context["approval_id"] = "defaultspack_local_ui"
    _apply_mimo_company_profile_authority_context(context, payload)


def _apply_mimo_company_profile_authority_context(context, payload):
    if not isinstance(context, dict) or not isinstance(payload, dict):
        return
    if context.get("_tool_server_approved") is not True:
        return
    metadata = _mapping_or_empty(payload.get("metadata"))
    params = _mapping_or_empty(payload.get("params"))
    tool_policy = _mapping_or_empty(payload.get("tool_policy"))
    profile_id = str(
        payload.get("profile_id")
        or metadata.get("profile_id")
        or params.get("profile_id")
        or tool_policy.get("profile_id")
        or ""
    ).strip()
    company_id = str(
        payload.get("company_id")
        or metadata.get("company_id")
        or params.get("company_id")
        or ""
    ).strip()
    if profile_id != "defaultspack.mimo_coding_company" or company_id != "mimo-coding-company":
        return
    principal_id = "profile:" + profile_id
    context["profile_id"] = profile_id
    context["authority_principal_id"] = principal_id
    context["principal_id"] = principal_id


def _allow_local_pairing_start_without_token(method, path, headers):
    if str(method or "").upper() != "POST" or path != "/api/p2p/pairing/start":
        return False
    origin = _header_value(headers, "Origin")
    if not origin or not _is_allowed_sensitive_origin(origin):
        return False
    csrf = _header_value(headers, "X-Rumi-CSRF")
    return bool(csrf.strip())


def _apply_authenticated_principal_context(context, payload):
    if not isinstance(context, dict) or not isinstance(payload, dict):
        return
    device_id = str(payload.get("_authenticated_device_id") or "").strip()
    scopes = payload.get("_authenticated_scopes")
    if device_id:
        context["_authenticated_device_id"] = device_id
    if isinstance(scopes, list):
        context["_authenticated_scopes"] = [str(scope) for scope in scopes if str(scope or "").strip()]
    principal = payload.get("_authenticated_principal")
    if not isinstance(principal, dict):
        return
    principal_payload = dict(principal)
    context["_authenticated_principal"] = principal_payload
    subject = payload.get("_authority_subject")
    context["_authority_subject"] = dict(subject) if isinstance(subject, dict) else dict(principal_payload)
    principal_id = str(principal_payload.get("principal_id") or "").strip()
    profile_id = str(principal_payload.get("profile_id") or "").strip()
    if profile_id:
        context["profile_id"] = profile_id
    if principal_id:
        context["authority_principal_id"] = principal_id
        if not bool(principal_payload.get("core_role")):
            context["principal_id"] = principal_id


def _function_principal_from_context(context, default="defaultspack"):
    candidate = ""
    if isinstance(context, dict):
        principal = context.get("_authenticated_principal")
        if isinstance(principal, dict) and not bool(principal.get("core_role")):
            candidate = str(principal.get("principal_id") or "").strip()
            if candidate:
                return candidate
            candidate = str(context.get("principal_id") or "").strip()
        if candidate:
            return candidate
    return default


class _RequestHandler(http.server.BaseHTTPRequestHandler):
    server_ref: DefaultsHttpServer | None = None
    protocol_version = "HTTP/1.1"

    def do_GET(self):
        self._handle_request("GET")

    def do_POST(self):
        self._handle_request("POST")

    def do_PUT(self):
        self._handle_request("PUT")

    def do_PATCH(self):
        self._handle_request("PATCH")

    def do_DELETE(self):
        self._handle_request("DELETE")

    def do_OPTIONS(self):
        self.send_response(204)
        self._send_cors_headers()
        self.end_headers()

    def _handle_request(self, method):
        try:
            parsed_url = urllib.parse.urlsplit(self.path)
            path = parsed_url.path
            origin_error = _browser_api_origin_error(
                method, path, self.headers, self.client_address
            )
            if origin_error:
                self._send_json(origin_error[0], error(origin_error[1], origin_error[2]))
                return
            if method == "POST" and path == _COMPOSER_TRANSCRIPTION_PATH:
                transcription_error = _composer_transcription_request_error(
                    self.headers, self.client_address
                )
                if transcription_error:
                    # Do not drain an attacker-controlled multi-megabyte body
                    # after rejecting it.  Closing prevents its bytes from
                    # being treated as a pipelined follow-up request.
                    self.close_connection = True
                    self._send_json(
                        transcription_error[0],
                        error(transcription_error[1], transcription_error[2]),
                    )
                    return
            if _header_value(
                self.headers, "X-Rumi-Approval-Browser-Token"
            ).strip():
                self._send_json(
                    410,
                    error(
                        "legacy browser approval credentials have been revoked",
                        "LEGACY_BROWSER_APPROVAL_REVOKED",
                    ),
                )
                return
            browser_exchange_path = path in {
                "/api/authority/browser-exchange",
                "/api/authority/browser-exchange/revoke",
                "/api/authority/browser-ui-operator",
            }
            if browser_exchange_path and parsed_url.query:
                self._send_json(
                    400,
                    error(
                        "browser approval exchanges do not accept URL parameters",
                        "BROWSER_EXCHANGE_URL_FORBIDDEN",
                    ),
                )
                return
            clean_legacy_url = _legacy_browser_url_without_credentials(parsed_url)
            if method == "GET" and clean_legacy_url is not None:
                self._send_empty(
                    303,
                    {
                        "Location": clean_legacy_url,
                        "Referrer-Policy": "no-referrer",
                        "Cache-Control": "no-store",
                    },
                )
                return
            websocket_error = _websocket_auth_error(self.headers, self.client_address)
            if websocket_error:
                self._send_json(websocket_error[0], error(websocket_error[1], websocket_error[2]))
                return
            query_params = {
                key: values[-1]
                for key, values in urllib.parse.parse_qs(
                    parsed_url.query, keep_blank_values=True
                ).items()
                if values and str(key) not in RESERVED_REQUEST_CONTEXT_KEYS
            }
            request_data: dict[str, Any] = dict(query_params)
            server_context: dict[str, Any] = {
                "_path": path,
                "_query_params": dict(query_params),
                "_headers": sanitized_forwarded_headers(self.headers),
            }
            if browser_exchange_path:
                bearer = _bearer_token(self.headers)
                if bearer and _local_auth_token_authorized(self.headers):
                    server_context["_authority_subject"] = {
                        "principal_id": "local-ui:"
                        + hashlib.sha256(bearer.encode("utf-8")).hexdigest()
                    }
            if method in ("POST", "PUT", "PATCH"):
                try:
                    content_length = int(self.headers.get("Content-Length", 0))
                except (TypeError, ValueError):
                    self._send_json(400, error("invalid Content-Length", "INVALID_CONTENT_LENGTH"))
                    return
                if content_length < 0:
                    self._send_json(400, error("invalid Content-Length", "INVALID_CONTENT_LENGTH"))
                    return
                if (
                    method == "POST"
                    and path == _COMPOSER_TRANSCRIPTION_PATH
                    and content_length > _COMPOSER_TRANSCRIPTION_MAX_REQUEST_BYTES
                ):
                    self.close_connection = True
                    self._send_json(
                        413,
                        error(
                            "recorded audio request is too large to transcribe",
                            "AUDIO_PAYLOAD_TOO_LARGE",
                        ),
                    )
                    return
                if content_length > 0:
                    raw_body = self.rfile.read(content_length)
                    raw_text = raw_body.decode("utf-8", errors="replace")
                    if not browser_exchange_path:
                        server_context["_raw_body"] = raw_text
                        server_context["_raw_body_base64"] = base64.b64encode(
                            raw_body
                        ).decode("ascii")
                    content_type = str(self.headers.get("Content-Type", "")).lower()
                    if "application/x-www-form-urlencoded" in content_type:
                        body_data = {
                            key: values[-1]
                            for key, values in urllib.parse.parse_qs(
                                raw_text, keep_blank_values=True
                            ).items()
                            if values
                        }
                    else:
                        try:
                            body_data = json.loads(raw_text)
                        except (json.JSONDecodeError, UnicodeDecodeError):
                            self._send_json(400, error("invalid JSON body"))
                            return
                        if not isinstance(body_data, dict):
                            self._send_json(400, error("JSON body must be an object"))
                            return
                    request_data.update(strip_reserved_request_context(body_data))
            request_data.update(server_context)
            request_data["_method"] = method
            request_data["_actual_method"] = method

            server = self.server_ref
            if server is None:
                self._send_json(503, error("HTTP server is not initialized", "SERVER_UNAVAILABLE"))
                return
            handler, path_params, source, path_inject, route_pattern = server._match_route(
                method, path
            )
            if handler is None:
                self._send_json(404, error("not found: " + method + " " + path))
                return
            if route_pattern and not server._route_allowed_by_active_profile(
                method, route_pattern
            ):
                server._record_profile_blocked_route(method, route_pattern)
                self._send_json(
                    403,
                    error(
                        "API route is blocked by the active profile policy: "
                        + method
                        + " "
                        + str(route_pattern),
                        "API_ROUTE_NOT_ALLOWED",
                    ),
                )
                return
            sensitive_error = self._sensitive_request_error(method, path, request_data)
            if sensitive_error:
                self._send_json(sensitive_error[0], error(sensitive_error[1], sensitive_error[2]))
                return
            request_data.pop(_AMBIENT_BROWSER_QA_CONTEXT_FLAG, None)
            request_data.pop(_LOCAL_UI_APPROVAL_CONTEXT_FLAG, None)
            request_data.pop(_LOCAL_UI_AUTH_CONTEXT_FLAG, None)
            if _local_auth_token_authorized(self.headers):
                request_data[_LOCAL_UI_AUTH_CONTEXT_FLAG] = True
            if _ambient_browser_test_token_authorized(method, path, self.headers, request_data):
                request_data[_AMBIENT_BROWSER_QA_CONTEXT_FLAG] = True
            if _local_ui_approval_route_authorized(method, path, self.headers, request_data):
                request_data[_LOCAL_UI_APPROVAL_CONTEXT_FLAG] = True

            if source == "registry":
                # Inject path parameters into request_data per route config
                if path_inject and path_params:
                    for url_param, data_key in path_inject.items():
                        if str(data_key) in RESERVED_REQUEST_CONTEXT_KEYS:
                            continue
                        request_data[data_key] = path_params.get(url_param, "")
                request_data["_method"] = method
                request_data["_actual_method"] = method
                result = server._invoke_registry_handler(
                    handler,
                    request_data,
                    path_params or {},
                )
            else:
                request_data["_method"] = method
                request_data["_actual_method"] = method
                # Fallback: original handler signature (request_data, path_params)
                result = handler(request_data, path_params)

            if isinstance(result, dict) and result.get("_binary"):
                self._send_binary(
                    int(result.get("status_code", 200)),
                    result.get("content_type", "application/octet-stream"),
                    result.get("body", b""),
                    result.get("headers") if isinstance(result.get("headers"), dict) else None,
                )
            elif isinstance(result, dict) and result.get("_empty"):
                self._send_empty(
                    int(result.get("status_code", 204)),
                    result.get("headers") if isinstance(result.get("headers"), dict) else None,
                )
            elif isinstance(result, dict) and result.get("_static"):
                self._send_static(
                    200, result.get("content_type", "text/html"), result.get("body", "")
                )
            elif isinstance(result, dict) and result.get("_redirect"):
                self._send_redirect(
                    int(result.get("status_code", 302)),
                    str(result.get("location") or "/chat"),
                )
            elif self._sse_events_from_result(result) is not None:
                self._send_sse(self._sse_events_from_result(result))
            else:
                status_code = 200
                if isinstance(result, dict) and result.get("status") == "error":
                    status_code = int(result.pop("_http_status", 400))
                self._send_json(status_code, result)
        except Exception as exc:
            self._send_json(500, error("internal server error: " + str(exc)))

    @staticmethod
    def _sse_events_from_result(result):
        if isinstance(result, dict) and result.get("_sse"):
            events = result.get("events", [])
            return None if isinstance(events, (str, bytes)) else events
        if (
            isinstance(result, dict)
            and result.get("status") == "ok"
            and isinstance(result.get("data"), dict)
            and result["data"].get("_sse")
        ):
            events = result["data"].get("events", [])
            return None if isinstance(events, (str, bytes)) else events
        return None

    def _send_json(self, status_code, data):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status_code)
        self._send_cors_headers()
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except BrokenPipeError:
            pass

    def _send_sse(self, events):
        self.send_response(200)
        self._send_cors_headers()
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache, no-transform")
        self.send_header("Connection", "close")
        self.end_headers()
        try:
            for event in events:
                if isinstance(event, bytes):
                    payload = event
                else:
                    payload = ("data: " + json.dumps(event, ensure_ascii=False) + "\n\n").encode(
                        "utf-8"
                    )
                self.wfile.write(payload)
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            self.close_connection = True

    def _send_redirect(self, status_code, location):
        self.send_response(status_code)
        self._send_cors_headers()
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _send_static(self, status_code, content_type, body):
        if isinstance(body, str):
            body_bytes = body.encode("utf-8")
        else:
            body_bytes = body
        self.send_response(status_code)
        self._send_cors_headers()
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body_bytes)))
        self.end_headers()
        try:
            self.wfile.write(body_bytes)
        except BrokenPipeError:
            pass

    def _send_binary(self, status_code, content_type, body, headers=None):
        if isinstance(body, str):
            body_bytes = body.encode("utf-8")
        else:
            body_bytes = body or b""
        self.send_response(status_code)
        self._send_cors_headers()
        self.send_header("Content-Type", str(content_type or "application/octet-stream"))
        self.send_header("Cache-Control", "no-store")
        for key, value in (headers or {}).items():
            if str(key).lower() in {"content-type", "content-length"}:
                continue
            self.send_header(str(key), str(value))
        self.send_header("Content-Length", str(len(body_bytes)))
        self.end_headers()
        try:
            self.wfile.write(body_bytes)
        except BrokenPipeError:
            pass

    def _send_empty(self, status_code=204, headers=None):
        self.send_response(status_code)
        self._send_cors_headers()
        for key, value in (headers or {}).items():
            if str(key).lower() in {"content-type", "content-length"}:
                continue
            self.send_header(str(key), str(value))
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _sensitive_request_error(self, method, path, request_data=None):
        route_sensitive, route_local_only = self._route_metadata_flags(method, path)
        if path in {
            "/api/authority/browser-exchange",
            "/api/authority/browser-exchange/revoke",
            "/api/authority/browser-ui-operator",
        }:
            exchange_error = _browser_exchange_transport_error(self.headers)
            if exchange_error:
                return exchange_error
        coding_error = require_local_guard(
            path,
            method,
            {str(key): str(value) for key, value in self.headers.items()},
            self.client_address,
        )
        if coding_error:
            return coding_error
        if _is_sensitive_coding_path(path):
            return None
        if not (route_sensitive or _requires_sensitive_http_auth(method, path)):
            return None
        if route_local_only and not _local_is_loopback_request(
            {str(key): str(value) for key, value in self.headers.items()},
            self.client_address,
        ):
            return (403, "sensitive local route requires a loopback client", "LOCAL_ONLY_REQUIRED")
        origin = self.headers.get("Origin", "")
        if not _is_allowed_sensitive_origin(origin):
            return (403, "origin not allowed for sensitive integration route", "ORIGIN_DENIED")
        if _browser_qa_token_authorized(method, path, self.headers, request_data):
            if (
                method.upper() in {"POST", "PUT", "DELETE"}
                and origin
                and not self.headers.get("X-Rumi-CSRF", "").strip()
            ):
                return (403, "CSRF header required for sensitive integration mutation", "CSRF_REQUIRED")
            return None
        if not _configured_local_auth_tokens():
            if str(method or "").upper() == "POST" and path == "/api/p2p/pairing/start":
                if not _local_is_loopback_request(
                    {str(key): str(value) for key, value in self.headers.items()},
                    self.client_address,
                ):
                    return (403, "sensitive local route requires a loopback client", "LOCAL_ONLY_REQUIRED")
                if not self.headers.get("X-Rumi-CSRF", "").strip():
                    return (403, "CSRF header required for sensitive integration mutation", "CSRF_REQUIRED")
                return None
            if _allow_local_pairing_start_without_token(method, path, self.headers):
                return None
            return (403, "local auth token is not configured", "AUTH_REQUIRED")
        if not _local_auth_token_authorized(self.headers):
            return (401, "local auth token required", "AUTH_REQUIRED")
        if (
            method.upper() in {"POST", "PUT", "PATCH", "DELETE"}
            and origin
            and not self.headers.get("X-Rumi-CSRF", "").strip()
        ):
            return (403, "CSRF header required for sensitive integration mutation", "CSRF_REQUIRED")
        return None

    def _route_metadata_sensitive(self, method, path):
        return self._route_metadata_flags(method, path)[0]

    def _route_metadata_flags(self, method, path):
        method = str(method or "").upper()
        path = str(path or "")
        server_ref = getattr(self, "server_ref", None)
        for entry in getattr(server_ref, "_routes", []):
            try:
                route_method, compiled, handler = entry[0], entry[1], entry[2]
            except Exception:
                continue
            if str(route_method or "").upper() != method:
                continue
            try:
                matched = compiled.match(path)
            except Exception:
                matched = None
            if not matched:
                continue
            sensitive = bool(
                getattr(handler, "__rumi_route_sensitive__", False)
                or getattr(handler, "__rumi_route_pre_auth__", False)
            )
            local_only = bool(getattr(handler, "__rumi_route_local_only__", False))
            if path.startswith("/api/prompts"):
                sensitive = True
                local_only = True
            return sensitive, local_only
        if path.startswith("/api/prompts"):
            return True, True
        return False, False

    def _send_cors_headers(self):
        path = self.path.split("?")[0]
        origin = _header_value(self.headers, "Origin")
        if _is_sensitive_http_path(path) or _is_browser_accessible_api_path(path):
            if _is_allowed_sensitive_origin(origin):
                if origin:
                    self.send_header("Access-Control-Allow-Origin", origin)
                    self.send_header("Vary", "Origin")
            self.send_header(
                "Access-Control-Allow-Methods", "GET, POST, PUT, PATCH, DELETE, OPTIONS"
            )
            self.send_header(
                "Access-Control-Allow-Headers",
                "Content-Type, Authorization, X-Rumi-CSRF, X-Rumi-Approval",
            )
            return
        if origin and _is_allowed_sensitive_origin(origin):
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")
        elif not origin:
            self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, PUT, PATCH, DELETE, OPTIONS")
        self.send_header(
            "Access-Control-Allow-Headers",
            "Content-Type, Authorization, X-Rumi-CSRF, X-Rumi-Approval",
        )

    def log_message(self, format, *args):
        pass


def _wait_for_signal():
    """Block the main thread until interrupted (cross-platform)."""
    try:
        while True:
            signal.pause()
    except AttributeError:
        # Windows does not have signal.pause(); poll instead.
        import time

        while True:
            time.sleep(86400)


def start_http_server(facade):
    """Start the HTTP transport and block until interrupted.

    The kernel's app.py calls ``http_server(facade)`` and then returns from
    ``main()``.  If we don't block here the process exits immediately because
    there would be no non-daemon threads keeping it alive.

    Strategy:
      * The server thread is started as **non-daemon** so the process stays
        alive even if main() returns without blocking.
      * We additionally call ``_wait_for_signal()`` so that Ctrl-C is caught
        cleanly and the server is shut down in an orderly fashion.
    """
    server = DefaultsHttpServer(facade)
    server.start()
    try:
        _wait_for_signal()
    except KeyboardInterrupt:
        print("\n[defaults] Shutting down HTTP server...")
    finally:
        server.stop()
    return server
