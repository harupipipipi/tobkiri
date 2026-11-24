"""Inactive CI-only raw C probe, separate from the cold restricted schema app."""

import json
import struct
import sys

import _tobkiri_poll_oracle
import wit_world.exports

_TARGET = {
    "python": list(sys.version_info[:3]),
    "pointer_bits": struct.calcsize("P") * 8,
    "extension_file": _tobkiri_poll_oracle.__file__,
}


class Probe(wit_world.exports.Probe):
    """Run actual libc calls only after snapshotting, under the separate oracle."""

    def run(self) -> str:
        """Keep genuine raw results and non-comparable clock metrics separate."""
        result = _tobkiri_poll_oracle.observe()
        timing = result.pop("timing")
        return json.dumps(
            {
                "target": _TARGET,
                "behavior": result,
                "timing": timing,
                "scope": "CI-only libc ABI oracle; no product or GUI parity",
                "negative_timeout": "not executed; excluded by exact source condition",
                "asynchronous_cancellation": "unsupported target; not a runtime pass",
                "production_enabled": False,
            },
            ensure_ascii=True,
            separators=(",", ":"),
        )
