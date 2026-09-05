import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from blocks._common import error, ok
from domain.frontend.command_protocol import CommandProtocolRegistry


def run(input_data, context):
    del context
    refs = (input_data or {}).get("state_refs", [])
    if not isinstance(refs, list):
        return error("state_refs must be an array", "INVALID_INPUT")
    return ok(CommandProtocolRegistry().query_states(refs))
