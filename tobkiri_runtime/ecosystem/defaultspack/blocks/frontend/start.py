import sys, os
import importlib.util

from blocks._common import ok, error, not_implemented, timestamp, gen_id


def _load_start_http_server():
    pack_root = os.path.join(os.path.dirname(__file__), "..", "..")
    module_path = os.path.join(pack_root, "transport", "http.py")
    module_name = "rumi_defaultspack_transport_http_runtime"
    spec = importlib.util.spec_from_file_location(module_name, module_path)
    if spec is None or spec.loader is None:
        raise RuntimeError("failed to load transport/http.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module.start_http_server


def run(input_data, context):
    facade = input_data.get("facade")
    if facade is None:
        return error("facade is required")
    start_http_server = _load_start_http_server()
    server = start_http_server(facade)
    return ok({
        "message": "HTTP server started",
        "host": server.host,
        "port": server.port,
    })
