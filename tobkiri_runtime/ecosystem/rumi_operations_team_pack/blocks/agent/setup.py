"""Register Operations Company routes from the product pack."""

from __future__ import annotations

import os
import sys


def run(context):
    pack_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    defaultspack_root = os.path.join(os.path.dirname(pack_root), "defaultspack")
    for path in (pack_root, defaultspack_root):
        if path not in sys.path:
            sys.path.insert(0, path)

    interface_registry = context["interface_registry"]
    source_component = context.get(
        "_source_component",
        "rumi_operations_team_pack:agent:operations_company",
    )

    def _lazy(module_path, func_name="run"):
        def handler(request_data, context):
            import importlib

            mod = importlib.import_module(module_path)
            return getattr(mod, func_name)(request_data, context)

        return handler

    routes = [
        ("GET", "/api/agent/company/manifest", _lazy("blocks.agent.company.manifest"), {}),
        ("GET", "/api/agent/company/status", _lazy("blocks.agent.company.status"), {}),
        ("POST", "/api/agent/company/bootstrap", _lazy("blocks.agent.company.bootstrap"), {}),
        ("GET", "/api/agent/mimo-company/manifest", _lazy("blocks.agent.mimo_company.manifest"), {}),
        ("GET", "/api/agent/mimo-company/status", _lazy("blocks.agent.mimo_company.status"), {}),
        ("POST", "/api/agent/mimo-company/bootstrap", _lazy("blocks.agent.mimo_company.bootstrap"), {}),
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
