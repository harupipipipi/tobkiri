"""Original JSON Schema packages with fixed DATA IO, evaluated after snapshot."""

import json
import struct
import sys
import sysconfig

from captured_loader import install
from wheel_data import FILES, DISTRIBUTIONS

_FINDER = install(FILES, DISTRIBUTIONS, __file__.rsplit("/", 1)[0])

# Extension imports happen during preinit; observations are never precomputed.
import jsonschema  # noqa: F401 - retain original eager initialization in the snapshot.
import rpds
import wit_world.exports
from exercise import exercise

_TARGET = {
    "python": list(sys.version_info[:3]),
    "pointer_bits": struct.calcsize("P") * 8,
    "soabi": sysconfig.get_config_var("SOABI"),
    "extension_file": rpds.rpds.__file__,
}
_BEFORE = {
    "reads": _FINDER.tree.reads,
    "metadata_queries": _FINDER.metadata_queries,
    "modules": len(_FINDER.loaded),
}


class Probe(wit_world.exports.Probe):
    def run(self) -> str:
        """Use original validation and first late resource/metadata/module accesses."""
        observation = exercise()
        return json.dumps(
            {
                "target": _TARGET,
                "observation": observation,
                "runtime_io": {
                    "resource_reads": _FINDER.tree.reads - _BEFORE["reads"],
                    "metadata_queries": _FINDER.metadata_queries
                    - _BEFORE["metadata_queries"],
                    "lazy_modules": _FINDER.loaded[_BEFORE["modules"] :],
                    "accounted_tree_bytes": _FINDER.tree.accounted_bytes,
                },
            },
            sort_keys=True,
            ensure_ascii=True,
            separators=(",", ":"),
        )
