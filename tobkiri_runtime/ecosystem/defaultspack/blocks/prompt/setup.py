"""
blocks/prompt/setup.py - Prompt component setup phase

Registers prompt-related HTTP routes into the kernel's InterfaceRegistry
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
    source_component = context.get("_source_component", "defaultspack:prompt:prompt")

    def _lazy(module_path, func_name="run", defaults=None, *, sensitive=False, pre_auth=False, local_only=False):
        """Return a lazy handler that imports the module on first call."""
        route_defaults = dict(defaults or {})

        def handler(request_data, context):
            import importlib
            mod = importlib.import_module(module_path)
            fn = getattr(mod, func_name)
            payload = dict(request_data or {})
            payload.update(route_defaults)
            return fn(payload, context)
        try:
            setattr(handler, "__rumi_route_sensitive__", bool(sensitive))
            setattr(handler, "__rumi_route_pre_auth__", bool(pre_auth))
            setattr(handler, "__rumi_route_local_only__", bool(local_only))
        except Exception:
            pass
        return handler

    from transport.registry import (
        prompt_contract_routes_enabled,
        prompt_http_route_specs,
    )

    if not prompt_contract_routes_enabled():
        return {"status": "ok", "registered": []}

    for spec in prompt_http_route_specs():
        module_path = spec.legacy_block_module or spec.block_module or spec.fallback_block_module
        if not module_path:
            continue
        handler = _lazy(
            module_path,
            defaults=spec.defaults,
            sensitive=spec.sensitive,
            pre_auth=spec.pre_auth,
            local_only=spec.local_only,
        )
        interface_registry.register(
            "io.http.route",
            {
                "method": spec.method,
                "pattern": spec.pattern,
                "handler": handler,
                "path_inject": dict(spec.path_inject),
                "sensitive": bool(spec.sensitive),
                "pre_auth": bool(spec.pre_auth),
                "local_only": bool(spec.local_only),
            },
            meta={"_source_component": source_component},
        )
