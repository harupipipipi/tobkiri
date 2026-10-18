import os
import sys


def run(context):
    pack_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    if pack_root not in sys.path:
        sys.path.insert(0, pack_root)
    interface_registry = context["interface_registry"]
    source_component = context.get("_source_component", "defaultspack:scheduler:scheduler")

    def _lazy(module_path, func_name="run"):
        def handler(request_data, context):
            import importlib

            mod = importlib.import_module(module_path)
            return getattr(mod, func_name)(request_data, context)

        return handler

    routes = [
        ("POST", "/api/scheduler/create", _lazy("blocks.scheduler.create"), {}),
        ("GET", "/api/scheduler/list", _lazy("blocks.scheduler.list"), {}),
        ("PUT", "/api/scheduler/{id}", _lazy("blocks.scheduler.update"), {"id": "job_id"}),
        ("DELETE", "/api/scheduler/{id}", _lazy("blocks.scheduler.delete"), {"id": "job_id"}),
        ("POST", "/api/scheduler/{id}/pause", _lazy("blocks.scheduler.pause"), {"id": "job_id"}),
        ("POST", "/api/scheduler/{id}/resume", _lazy("blocks.scheduler.resume"), {"id": "job_id"}),
        ("POST", "/api/scheduler/{id}/run-now", _lazy("blocks.scheduler.run_now"), {"id": "job_id"}),
        ("POST", "/api/scheduler/tick", _lazy("blocks.scheduler.tick"), {}),
        ("GET", "/api/scheduler/status", _lazy("blocks.scheduler.status"), {}),
    ]
    for method, pattern, handler, path_inject in routes:
        interface_registry.register(
            "io.http.route",
            {"method": method, "pattern": pattern, "handler": handler, "path_inject": path_inject},
            meta={"_source_component": source_component},
        )
    try:
        from domain.scheduler.daemon import start_scheduler_daemon

        start_scheduler_daemon(settings_owner=context.get("_settings_owner_port"))
    except Exception:
        pass
