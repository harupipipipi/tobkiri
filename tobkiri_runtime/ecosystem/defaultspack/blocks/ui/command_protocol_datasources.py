import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from blocks._common import ok
from domain.frontend.command_protocol import CommandProtocolRegistry


def run(input_data, context):
    del context
    return ok(CommandProtocolRegistry().query_datasource(input_data or {}))
