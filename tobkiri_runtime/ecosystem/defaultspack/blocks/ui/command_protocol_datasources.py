import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from blocks._common import ok
from domain.frontend.command_protocol import CommandProtocolRegistry
from tobkiri_protocol.settings_state import SettingsOwnerPort


def run(input_data, context, *, settings_owner: SettingsOwnerPort | None = None):
    """Run with a trusted caller-supplied owner, never one from request data."""
    del context
    registry = CommandProtocolRegistry(settings_owner=settings_owner)
    return ok(registry.query_datasource(input_data or {}))
