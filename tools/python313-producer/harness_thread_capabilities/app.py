"""Actual CPython execution-base observations, never product acceptance."""

import contextvars
import json
import struct
import sys
import threading

import wit_world.exports
from wit_world.imports import bindings

from logic import run

# This is a declared, different initialization condition from the late-import
# diagnostic. Threading's own module-level state is also pre-initialized.
# ContextVar/Event/worker construction and operations stay in run() below.
_PREINITIALIZED_STDLIB = (contextvars, struct, sys, threading)


def _checkpoint(stage: str) -> None:
    payload = json.dumps(
        {
            "format": "tobkiri.thread-capabilities.checkpoint.v1",
            "stage": stage,
        },
        separators=(",", ":"),
    )
    if bindings.inspect(payload) != "{}":
        raise RuntimeError("diagnostic checkpoint refused")


class Probe(wit_world.exports.Probe):
    """Observe real worker operations with explicitly pre-initialized modules."""

    def run(self) -> str:
        result = run(_checkpoint)
        result["stdlib_initialization"] = {
            "phase": "componentize-preinit",
            "modules": [module.__name__ for module in _PREINITIALIZED_STDLIB],
            "operations_phase": "guest-runtime",
            "late_import_parity": False,
        }
        return json.dumps(result, separators=(",", ":"))

    def ping(self) -> str:
        """A second sync export for a distinct ABI-constraint observation."""
        return '{"format":"tobkiri.thread-capabilities.ping.v1","entered":true}'
