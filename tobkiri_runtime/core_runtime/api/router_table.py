from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Optional
from urllib.parse import unquote

from ._helpers import _SAFE_ERROR_MSG, _log_internal_error
from .api_response import APIResponse
from .request_authorizer import authorize_route
from .route_errors import api_route_function_error_status, api_route_function_public_error
from .route_handlers import _compile_template_path, _is_safe_path_param
from .safe_headers import RESERVED_REQUEST_CONTEXT_KEYS, strip_reserved_request_context
from ..validation import HANDLER_NAME_RE


logger = logging.getLogger(__name__)

class APIRouteTableMixin:
    @classmethod
    def _is_pack_approved_for_runtime_routes(cls, pack_id: str) -> bool:
        normalized_pack_id = str(pack_id or "").strip()
        if not normalized_pack_id:
            return False
        manager = getattr(cls, "approval_manager", None)
        if manager is None:
            try:
                from ..approval_manager import get_approval_manager

                manager = get_approval_manager()
            except Exception as exc:
                logger.warning(
                    "Skipping runtime routes for pack '%s': approval manager unavailable: %s",
                    normalized_pack_id,
                    exc,
                )
                return False
        try:
            result = manager.is_pack_approved_and_verified(normalized_pack_id)
        except Exception as exc:
            logger.warning(
                "Skipping runtime routes for pack '%s': approval verification failed: %s",
                normalized_pack_id,
                exc,
            )
            return False
        if isinstance(result, tuple):
            approved = bool(result[0])
            reason = result[1] if len(result) > 1 else None
        else:
            approved = bool(result)
            reason = None
        if not approved:
            logger.warning(
                "Skipping runtime routes for unapproved pack '%s': %s",
                normalized_pack_id,
                reason or "not approved",
            )
        return approved

    @staticmethod
    def _pack_root_hint(pack_info: Any) -> Optional[Any]:
        for attr in ("subdir", "path", "pack_dir"):
            value = getattr(pack_info, attr, None)
            if isinstance(value, (str, Path)):
                return value
        return None

    @classmethod
    def _pack_allows_in_process_api_metadata(
        cls,
        pack_id: str,
        pack_info: Any = None,
    ) -> bool:
        from ..pack_function_runtime import is_pack_function_in_process_allowed

        hint = cls._pack_root_hint(pack_info) if pack_info is not None else None
        if pack_info is not None and hint is None:
            return False
        return is_pack_function_in_process_allowed(pack_id, hint)

    @classmethod
    def _register_api_routes_from_manifest(
        cls,
        pack_id: str,
        ecosystem: dict[str, Any],
        pack_info: Any = None,
    ) -> int:
        routes = ecosystem.get("api_routes")
        if not routes or not isinstance(routes, list):
            return 0
        route_count = 0
        for route in routes:
            if not isinstance(route, dict):
                continue
            method = route.get("method", "").upper()
            handler_name = route.get("handler", "")
            function_id = route.get("function_id", route.get("function", ""))
            if not method or not (handler_name or function_id):
                continue
            if function_id and not cls._pack_allows_in_process_api_metadata(pack_id, pack_info):
                logger.warning(
                    "Ignoring function api_route from non-first-party pack: %s:%s",
                    pack_id,
                    function_id,
                )
                continue
            if handler_name and not HANDLER_NAME_RE.match(handler_name):
                logger.warning("Invalid handler name in api_routes: %s", handler_name)
                continue
            entry = {
                "handler": handler_name,
                "function_id": function_id,
                "pack_id": pack_id,
                "owner_pack_id": str(route.get("owner_pack_id") or pack_id),
                "permission_id": str(route.get("permission_id") or ""),
                "provider_id": str(route.get("provider_id") or ""),
                "frontend_id": str(route.get("frontend_id") or ""),
                "audience": str(route.get("audience") or "kernel_api"),
                "core_only": bool(route.get("core_only", False)),
                "resource_template": dict(route.get("resource_template") or {}),
                "auth_mode": str(route.get("auth_mode") or ""),
                "principal": str(route.get("principal") or ""),
                "csrf_origin_required": bool(route.get("csrf_origin_required", False)),
                "rate_limit": str(route.get("rate_limit") or ""),
                "audit_category": str(route.get("audit_category") or ""),
                "legacy_until": route.get("legacy_until"),
                "pass_body": route.get("pass_body", False),
                "pass_query": route.get("pass_query", False),
                "response_mode": route.get("response_mode", "result"),
                "args": dict(route.get("args") or {}),
                "path_param_map": dict(route.get("path_param_map") or {}),
            }
            if "path_pattern" in route:
                compiled = _compile_template_path(route["path_pattern"])
                if compiled is not None:
                    pattern, param_names = compiled
                    cls._api_route_patterns.append((method, pattern, param_names, entry))
                    route_count += 1
            elif "path" in route:
                cls._api_route_exact[(method, route["path"])] = entry
                route_count += 1
        return route_count

    @classmethod
    def load_builtin_core_api_routes(cls, core_pack_dir: Path | None = None) -> int:
        root = core_pack_dir or (Path(__file__).resolve().parent.parent / "core_pack")
        count = 0
        for ecosystem_path in sorted(root.glob("*/ecosystem.json")):
            try:
                ecosystem = json.loads(ecosystem_path.read_text(encoding="utf-8"))
            except Exception:
                logger.warning(
                    "Failed to load builtin core api routes from %s",
                    ecosystem_path,
                    exc_info=True,
                )
                continue
            pack_id = str(ecosystem.get("pack_id") or "").strip()
            if not pack_id:
                continue
            count += cls._register_api_routes_from_manifest(pack_id, ecosystem)
        return count

    @classmethod
    def load_api_routes(
        cls,
        registry,
        pack_ids: Optional[set[str]] = None,
        *,
        include_builtin_core_control_panel: bool = False,
    ) -> int:
        cls._api_route_exact = {}
        cls._api_route_patterns = []
        count = 0
        if registry is not None:
            for pack_id, pack_info in registry.packs.items():
                if pack_ids is not None and pack_id not in pack_ids:
                    continue
                if not cls._is_pack_approved_for_runtime_routes(pack_id):
                    continue
                count += cls._register_api_routes_from_manifest(
                    pack_id,
                    pack_info.ecosystem,
                    pack_info,
                )
        if include_builtin_core_control_panel:
            count += cls.load_builtin_core_api_routes()
        logger.info("Loaded %d api_route entries", count)
        return count

    def _dispatch_api_route(
        self,
        method: str,
        path: str,
        body: Optional[dict[str, Any]] = None,
        query: Optional[dict[str, Any]] = None,
    ) -> bool:
        method_upper = method.upper()
        entry = self._api_route_exact.get((method_upper, path))
        path_params = {}

        if entry is None:
            normalized = path.rstrip("/")
            if not normalized.startswith("/"):
                normalized = "/" + normalized
            for tmpl_method, pattern, param_names, route_entry in self._api_route_patterns:
                if tmpl_method != method_upper:
                    continue
                match = pattern.match(normalized)
                if match is None:
                    continue
                safe = True
                params = {}
                for name in param_names:
                    decoded = unquote(match.group(name))
                    if not _is_safe_path_param(decoded):
                        safe = False
                        break
                    params[name] = decoded
                if safe:
                    entry = route_entry
                    path_params = params
                    break

        if entry is None:
            return False

        entry_for_auth = dict(entry)
        entry_for_auth["resource_template"] = self._resolve_api_route_resource_template(
            entry.get("resource_template") if isinstance(entry.get("resource_template"), dict) else {},
            path_params=path_params,
            body=body,
            query=query,
        )
        principal = getattr(self, "_authenticated_principal", None)
        if principal is not None:
            if entry.get("core_only") and not principal.core_role:
                self._send_response(APIResponse(False, error="Forbidden"), 403)
                return True
            authorization = authorize_route(
                principal=principal,
                method=method_upper,
                path=path,
                route_entry=entry_for_auth,
            )
            if not authorization.allowed:
                self._send_response(
                    APIResponse(False, error=authorization.reason or "Forbidden"),
                    authorization.status_code,
                )
                return True

        pack_id = entry.get("pack_id", "")
        if not self._is_pack_approved_for_runtime_routes(pack_id):
            self._send_response(
                APIResponse(False, error=f"Pack not approved: {pack_id}"),
                403,
            )
            return True

        handler_name = entry["handler"]
        pass_body = entry.get("pass_body", False)
        pass_query = entry.get("pass_query", False)
        response_mode = entry.get("response_mode", "result")

        for param_val in path_params.values():
            if not self._is_safe_id(param_val):
                self._send_response(APIResponse(False, error="Invalid path parameter"), 400)
                return True

        try:
            if entry.get("function_id"):
                from ..capability_executor import get_capability_executor

                if not type(self)._pack_allows_in_process_api_metadata(entry["pack_id"]):
                    logger.warning(
                        "Rejecting function api_route dispatch from non-first-party pack: %s:%s",
                        entry["pack_id"],
                        entry["function_id"],
                    )
                    self._send_response(
                        APIResponse(False, error="Pack function route is not allowed"),
                        403,
                    )
                    return True

                call_args = strip_reserved_request_context(body) if pass_body else {}
                if pass_query:
                    call_args.update(strip_reserved_request_context(query))
                call_args.update(strip_reserved_request_context(entry.get("args") or {}))
                param_map = entry.get("path_param_map") or {}
                if param_map:
                    for target_key, source_key in param_map.items():
                        if str(target_key) in RESERVED_REQUEST_CONTEXT_KEYS:
                            continue
                        if source_key in path_params:
                            call_args[target_key] = path_params[source_key]
                else:
                    for target_key, value in path_params.items():
                        if str(target_key) in RESERVED_REQUEST_CONTEXT_KEYS:
                            continue
                        call_args[target_key] = value
                route_context = {
                    "pack_id": entry["pack_id"],
                    "method": method_upper,
                    "path": path,
                    "_api_route": True,
                }
                execution_principal = entry["pack_id"]
                if principal is not None:
                    principal_payload = principal.to_dict()
                    route_context["_authenticated_principal"] = principal_payload
                    to_subject = getattr(principal, "to_internal_subject", None)
                    route_context["_authority_subject"] = (
                        to_subject(
                            owner_pack_id=str(
                                entry.get("owner_pack_id") or entry.get("pack_id") or ""
                            ),
                            provider_id=str(entry.get("provider_id") or ""),
                            frontend_id=str(entry.get("frontend_id") or ""),
                        )
                        if callable(to_subject)
                        else principal_payload
                    )
                    if not principal.core_role:
                        execution_principal = principal.principal_id
                response = get_capability_executor().execute(
                    execution_principal,
                    {
                        "type": "function.call",
                        "qualified_name": f"{entry['pack_id']}:{entry['function_id']}",
                        "args": call_args,
                        "request_id": f"api-route:{method_upper}:{path}",
                        "context": route_context,
                    },
                )
                if not getattr(response, "success", False):
                    error_type = str(getattr(response, "error_type", "") or "")
                    status = api_route_function_error_status(error_type)
                    if status is None:
                        return False
                    error_value = api_route_function_public_error(
                        error_type,
                        getattr(response, "error", None),
                        _SAFE_ERROR_MSG,
                    )
                    self._send_response(
                        APIResponse(False, error=error_value),
                        status,
                    )
                    return True
                result = getattr(response, "output", None)
            else:
                handler = getattr(self, handler_name, None)
                if handler is None:
                    logger.error("api_route handler not found: %s", handler_name)
                    self._send_response(APIResponse(False, error=_SAFE_ERROR_MSG), 500)
                    return True

                args: list[Any] = []
                if path_params:
                    args.extend(path_params.values())
                if pass_body:
                    args.append(body if body is not None else {})
                elif pass_query:
                    args.append(dict(query or {}))
                result = handler(*args)

            if entry.get("function_id"):
                result = self._unwrap_defaultspack_function_envelope(result)
            sse_events = self._sse_events_from_result(result)
            if sse_events is not None:
                self._send_sse(sse_events)
            elif response_mode == "raw":
                status_code = 200
                if isinstance(result, dict) and "status_code" in result:
                    try:
                        status_code = int(result["status_code"])
                    except (TypeError, ValueError):
                        status_code = 200
                self._send_raw_json(result, status=status_code)
            else:
                self._send_result(result)
        except Exception as exc:
            _log_internal_error(f"api_route:{handler_name}", exc)
            self._send_response(APIResponse(False, error=_SAFE_ERROR_MSG), 500)

        return True

    @staticmethod
    def _resolve_api_route_resource_template(
        template: dict[str, Any],
        *,
        path_params: dict[str, str],
        body: Optional[dict[str, Any]],
        query: Optional[dict[str, Any]],
    ) -> dict[str, Any]:
        sources = {
            "path": path_params or {},
            "params": path_params or {},
            "body": body if isinstance(body, dict) else {},
            "query": query if isinstance(query, dict) else {},
        }
        resolved: dict[str, Any] = {}
        for key, value in template.items():
            if not str(key).strip():
                continue
            if isinstance(value, str) and value.startswith("{") and value.endswith("}"):
                ref = value[1:-1].strip()
                source_name, _, source_key = ref.partition(".")
                if source_key and source_name in sources:
                    resolved[str(key)] = sources[source_name].get(source_key, "")
                    continue
                if ref in path_params:
                    resolved[str(key)] = path_params.get(ref, "")
                    continue
            resolved[str(key)] = value
        return resolved
