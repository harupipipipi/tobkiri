import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from _common import error, ok
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from domain.frontend.command_protocol import CommandProtocolRegistry


def run(input_data, context):
    del context
    registry = CommandProtocolRegistry()
    method = (input_data or {}).get("_method", "GET").upper()
    if method == "GET":
        catalog = registry.catalog()
        return ok(
            {
                "commands": registry.legacy_read_projection(),
                "manifest_errors": catalog["diagnostics"],
                "deprecated": True,
                "replacement": "/api/command-protocol/v1/catalog",
            }
        )
    if method == "POST":
        return error(
            "legacy command execution is removed; use /api/command-protocol/v1/invoke",
            "COMMAND_PROTOCOL_V1_REQUIRED",
        )
    return error("unsupported method", "METHOD_NOT_ALLOWED")
