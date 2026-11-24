"""Freeze genuine dependencies and the original pure App projection, not results."""
import importlib
import struct
import sys
import sysconfig

from projection_loader import install
from projection_data import DISTRIBUTIONS, FILES, NAMESPACES

_FINDER = install(FILES, DISTRIBUTIONS, __file__.rsplit("/", 1)[0],
                  NAMESPACES, compressed=True)

_schema = importlib.import_module("jsonschema")
rpds = importlib.import_module("rpds")
_exports = importlib.import_module("projection_export")
Projection = _exports.Projection

if (sys.version_info[:3] != (3, 13, 16) or struct.calcsize("P") != 4
        or "wasm32" not in sysconfig.get_config_var("SOABI")
        or not rpds.rpds.__file__.endswith("/rpds/rpds.abi3.so")):
    raise RuntimeError("original Python 3.13/Wasm and genuine rpds extension required")

# The ordinary world exports Lifecycle; the comparison world has no resources.
if hasattr(_exports, "Lifecycle"):
    Lifecycle = _exports.Lifecycle

_FINDER.seal()
