"""
blocks/cli/entry.py — CLI entry-point block.

Provides two capabilities:
  1. Direct-mode entry point for kernel launch via io.cli.server
  2. Config GET/PUT handler for HTTP API routes:
     - GET  /api/cli/config  → return current CLI config
     - PUT  /api/cli/config  → update CLI config

The HTTP method is determined by the ``_http_method`` field injected by
the route registration in blocks/dev/setup.py, or defaults to GET.
"""

import os
import json


from blocks._common import ok, error, timestamp


_DEFAULT_CONFIG = {
    "default_model": "default",
    "system_prompt": "",
    "http_host": "127.0.0.1",
    "http_port": 8766,
    "stream": True,
}


def _config_path():
    pack_root = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..")
    return os.path.join(pack_root, "user_data", "cli_config.json")


def _load_config():
    path = _config_path()
    config = dict(_DEFAULT_CONFIG)
    if os.path.isfile(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                stored = json.load(f)
            if isinstance(stored, dict):
                config.update(stored)
        except (json.JSONDecodeError, OSError):
            pass
    return config


def _save_config(config):
    path = _config_path()
    dir_path = os.path.dirname(path)
    if not os.path.isdir(dir_path):
        os.makedirs(dir_path, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=2, ensure_ascii=False)


def run(input_data, context):
    """Handle CLI config read/write.

    Routing:
      - GET  (``_http_method`` == "GET" or absent): return config
      - PUT  (``_http_method`` == "PUT"): merge input_data into config
    """
    method = input_data.get("_http_method", "GET").upper()

    if method == "PUT":
        config = _load_config()
        allowed_keys = set(_DEFAULT_CONFIG.keys())
        updates = {k: v for k, v in input_data.items() if k in allowed_keys}
        if not updates:
            return error("No valid config keys provided. Allowed: " + ", ".join(sorted(allowed_keys)), "INVALID_INPUT")
        config.update(updates)
        _save_config(config)
        return ok(config)

    # GET (default)
    config = _load_config()
    return ok(config)


def run_get(input_data, context):
    """Explicit GET handler for route registration."""
    input_data["_http_method"] = "GET"
    return run(input_data, context)


def run_put(input_data, context):
    """Explicit PUT handler for route registration."""
    input_data["_http_method"] = "PUT"
    return run(input_data, context)
