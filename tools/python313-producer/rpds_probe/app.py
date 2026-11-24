"""Minimal UI dependency probe: real extension operations after snapshotting."""

import json
import struct
import sys
import sysconfig

import rpds
import wit_world.exports

# componentize-py has no runtime module filesystem. Import the real extension
# and target sysconfig during preinit, but do not precompute the operation test.
_TARGET = {
    "python": list(sys.version_info[:3]),
    "pointer_bits": struct.calcsize("P") * 8,
    "soabi": sysconfig.get_config_var("SOABI"),
    "extension_file": rpds.__file__,
}


class Probe(wit_world.exports.Probe):
    """Expose no Profile, Gate, filesystem, network or credential operation."""

    def run(self) -> str:
        """Exercise persistent map/set operations used by referencing at runtime."""
        original = rpds.HashTrieMap({"before": 1})
        extended = original.insert("after", 2)
        removed = extended.remove("before")
        values = rpds.HashTrieSet(["one"]).insert("two").discard("one")
        return json.dumps(
            {
                "target": _TARGET,
                "original": dict(original),
                "extended": dict(extended),
                "removed": dict(removed),
                "missing_default": original.get("absent", 7),
                "set_values": sorted(values),
            },
            sort_keys=True,
        )
