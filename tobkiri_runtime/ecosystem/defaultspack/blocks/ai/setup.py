"""
blocks/ai/setup.py - AI client component setup phase

Registers AI-related HTTP routes into the kernel's InterfaceRegistry
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
    source_component = context.get("_source_component", "defaultspack:ai_client:ai_client")
    try:
        from capability_bindings import register_defaultspack_binding_handlers
        register_defaultspack_binding_handlers(interface_registry)
    except Exception as exc:
        print(
            "[defaultspack.ai] setup: failed to register capability bindings - "
            + str(exc),
            file=sys.stderr,
        )

    def _lazy(module_path, func_name="run"):
        """Return a lazy handler that imports the module on first call."""
        def handler(request_data, ctx):
            import importlib
            mod = importlib.import_module(module_path)
            fn = getattr(mod, func_name)
            return fn(request_data, ctx)
        return handler

    routes = [
        # --- Catalog / provider / model routes ---
        ("GET", "/api/ai/catalog", _lazy("blocks.ai.catalog"), {}),
        ("GET", "/api/ai/strategies", _lazy("blocks.ai.strategies"), {}),
        ("GET", "/api/ai/providers", _lazy("blocks.ai.providers"), {}),
        ("GET", "/api/ai/models", _lazy("blocks.ai.models"), {}),
        ("POST", "/api/ai/models/search", _lazy("blocks.ai.search_models"), {}),
        ("POST", "/api/ai/models/capabilities", _lazy("blocks.ai.get_model_capabilities"), {}),
        ("POST", "/api/ai/models/recommend", _lazy("blocks.ai.recommend_model"), {}),
        ("POST", "/api/ai/models/route", _lazy("blocks.ai.route_model"), {}),
        ("GET", "/api/ai/profiles", _lazy("blocks.ai.profiles"), {}),
        ("GET", "/api/ai/provider-key", _lazy("blocks.ai.provider_key"), {}),
        ("POST", "/api/ai/provider-key", _lazy("blocks.ai.provider_key"), {}),
        ("GET", "/api/ai/oauth", _lazy("blocks.ai.oauth"), {}),
        ("POST", "/api/ai/oauth", _lazy("blocks.ai.oauth"), {}),
        ("GET", "/api/ai/oauth/{provider_id}/callback", _lazy("blocks.ai.oauth"), {"provider_id": "provider_id"}),
        # --- Routing: analyze ---
        ("POST", "/api/ai/routing/analyze", _lazy("blocks.ai.routing.analyze"), {}),
        # --- Routing: route ---
        ("POST", "/api/ai/routing/route", _lazy("blocks.ai.routing.route"), {}),
        # --- Routing: profiles ---
        ("GET", "/api/ai/routing/profiles", _lazy("blocks.ai.routing.profiles"), {}),
        ("POST", "/api/ai/routing/profiles", _lazy("blocks.ai.routing.profiles"), {}),
        ("PUT", "/api/ai/routing/profiles/{name}", _lazy("blocks.ai.routing.profiles"), {"name": "name"}),
        ("DELETE", "/api/ai/routing/profiles/{name}", _lazy("blocks.ai.routing.profiles"), {"name": "name"}),
        # --- Routing: rules ---
        ("GET", "/api/ai/routing/rules", _lazy("blocks.ai.routing.rules"), {}),
        ("POST", "/api/ai/routing/rules", _lazy("blocks.ai.routing.rules"), {}),
        ("DELETE", "/api/ai/routing/rules/{id}", _lazy("blocks.ai.routing.rules"), {"id": "id"}),
        # --- Routing: log ---
        ("GET", "/api/ai/routing/log", _lazy("blocks.ai.routing.log"), {}),
    ]

    for method, pattern, handler, path_inject in routes:
        interface_registry.register(
            "io.http.route",
            {
                "method": method,
                "pattern": pattern,
                "handler": handler,
                "path_inject": path_inject,
            },
            meta={"_source_component": source_component},
        )
