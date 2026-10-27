"""Actual CPython execution-base observations, never product acceptance."""

import json

import wit_world.exports
from wit_world.imports import bindings

from logic import run


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
    """Keep thread creation out of the componentizer's pre-init snapshot."""

    def run(self) -> str:
        return json.dumps(run(_checkpoint), separators=(",", ":"))

    def ping(self) -> str:
        """A second sync export for a distinct ABI-constraint observation."""
        return '{"format":"tobkiri.thread-capabilities.ping.v1","entered":true}'
