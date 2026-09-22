import importlib
import json
import sys


from blocks._common import error, ok, timestamp
from bridge.block_adapter import invoke_block
from transport.registry import (
    canonical_http_route_specs,
    compile_http_route_pattern,
    flow_http_output_is_compatible,
    http_route_sort_key,
)


def _route_specs():
    specs = canonical_http_route_specs(include_always_available=True)
    unsupported_handlers = {"_handle_chat_redirect", "_handle_static", "_handle_static_file"}
    return [
        spec
        for _, spec in sorted(
            enumerate(specs),
            key=lambda item: http_route_sort_key(item[1].method, item[1].pattern, item[0]),
        )
        if spec.handler_name not in unsupported_handlers
    ]


def _match_route_spec(method, path):
    for spec in _route_specs():
        if spec.method != method:
            continue
        match = compile_http_route_pattern(spec.pattern).match(path)
        if match is not None:
            return spec, match.groupdict()
    return None, {}


def _legacy_module_name(spec):
    if spec is None:
        return None
    return (
        spec.block_module
        or spec.fallback_block_module
        or spec.flow_id
        or spec.function_name
        or None
    )


def _match_route(method, path):
    spec, path_params = _match_route_spec(method, path)
    if spec is None:
        return None, None, {}
    return spec.pattern, _legacy_module_name(spec), path_params


def _legacy_id_inject_map():
    mapping = {}
    for spec in _route_specs():
        if len(spec.path_inject or {}) != 1:
            continue
        source_key, dest_key = next(iter(spec.path_inject.items()))
        mapping[spec.pattern] = (dest_key, source_key)
    return mapping


_ID_INJECT_MAP = _legacy_id_inject_map()
_ROUTE_MAP = [(spec.method, spec.pattern, _legacy_module_name(spec)) for spec in _route_specs()]


class DefaultsStdioTransport:
    def __init__(self):
        self._running = False

    def start(self):
        self._running = True
        while self._running:
            line = sys.stdin.readline()
            if not line:
                break
            line = line.strip()
            if not line:
                continue
            try:
                request = json.loads(line)
            except json.JSONDecodeError:
                self._send_error("Invalid JSON")
                continue
            result = self._handle_request(request)
            self._send_response(result)

    def stop(self):
        self._running = False

    def _handle_request(self, request):
        method = str(request.get("method", "GET")).upper()
        path = str(request.get("path", ""))
        data = request.get("data", {})
        if not isinstance(data, dict):
            data = {}

        spec, path_params = _match_route_spec(method, path)
        if spec is None:
            return error("not found: " + method + " " + path)

        if spec.pattern == "/api/health" and not _legacy_module_name(spec):
            return ok({"status": "healthy", "pack": "defaultspack", "ts": timestamp()})

        if spec.pattern == "/api/context" and not _legacy_module_name(spec):
            return ok({"pack": "defaultspack", "ts": timestamp()})

        payload = dict(data)
        payload.update(dict(spec.defaults or {}))
        payload["_method"] = method
        for source_key, dest_key in (spec.path_inject or {}).items():
            payload[dest_key] = path_params.get(source_key, "")

        if spec.flow_id:
            return self._invoke_flow_route(
                spec.flow_id,
                payload,
                fallback_block_module=spec.fallback_block_module,
            )
        if spec.function_name:
            return self._invoke_function_route(
                spec.function_name,
                payload,
                fallback_block_module=spec.fallback_block_module,
            )
        if spec.block_module:
            return self._invoke_fallback_block(spec.block_module, payload)
        if spec.handler_name == "_handle_health":
            return ok({"status": "healthy", "pack": "defaultspack", "ts": timestamp()})
        if spec.handler_name == "_handle_context_info":
            return ok({"pack": "defaultspack", "ts": timestamp()})
        return error("handler not available")

    def _invoke_flow_route(self, flow_id, payload, *, fallback_block_module=""):
        context = self._build_context()
        context["flow_id"] = flow_id
        try:
            from domain.flow import FlowEngine

            flow_result = FlowEngine().execute(flow_id, payload, context)
            if flow_result.is_success():
                if flow_http_output_is_compatible(
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
        return self._invoke_fallback_block(fallback_block_module, payload)

    def _invoke_function_route(self, function_name, payload, *, fallback_block_module=""):
        try:
            from domain.function_runtime.bridge import invoke_function

            result = invoke_function(
                function_name, payload, self._build_context(), principal_id="defaultspack"
            )
            if isinstance(result, dict) and result.get("status") != "error":
                return result
            if not fallback_block_module:
                return result
        except Exception as exc:
            if not fallback_block_module:
                return error(str(exc), "FUNCTION_ROUTE_FAILED")
        return self._invoke_fallback_block(fallback_block_module, payload)

    def _invoke_fallback_block(self, module_name, payload):
        if not module_name:
            return error("handler not available")
        try:
            from domain.function_runtime.bridge import invoke_function
            from domain.function_runtime.registry import function_id_for_block_module

            function_id = function_id_for_block_module(module_name)
            if function_id:
                result = invoke_function(
                    f"defaultspack:{function_id}",
                    payload,
                    self._build_context(),
                    principal_id="defaultspack",
                )
                if isinstance(result, dict) and result.get("status") != "error":
                    return result
                error_code = (
                    str((result.get("error") or {}).get("code") or "")
                    if isinstance(result, dict)
                    else ""
                )
                if error_code not in {
                    "FUNCTION_REGISTRY_UNAVAILABLE",
                    "FUNCTION_NOT_FOUND",
                    "CAPABILITY_RUNTIME_UNAVAILABLE",
                    "CAPABILITY_EXECUTION_FAILED",
                }:
                    return result
        except Exception:
            pass

        try:
            mod = importlib.import_module(module_name)
            handler_run = getattr(mod, "run")
        except (ImportError, AttributeError) as exc:
            return error("handler not available: " + str(exc))

        try:
            return invoke_block(module_name, payload, self._build_context())
        except Exception as exc:
            try:
                return handler_run(payload, self._build_context())
            except Exception:
                return error("handler error: " + str(exc))

    def _send_response(self, data):
        line = json.dumps(data, ensure_ascii=False) + "\n"
        sys.stdout.write(line)
        sys.stdout.flush()

    def _send_error(self, message):
        self._send_response(error(message))

    def _build_context(self):
        return {
            "flow_id": "stdio_direct",
            "step_id": "stdio_request",
            "phase": "execute",
            "ts": timestamp(),
            "owner_pack": "defaultspack",
            "inputs": {},
        }
