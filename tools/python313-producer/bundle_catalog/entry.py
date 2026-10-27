"""Freeze genuine dependencies, original sources, and their schema resources."""
import importlib
import struct
import sys
import sysconfig

from bundle_catalog_loader import install
from bundle_catalog_data import DISTRIBUTIONS, FILES, NAMESPACES

_FINDER = install(
    FILES,
    DISTRIBUTIONS,
    __file__.rsplit("/", 1)[0],
    NAMESPACES,
    compressed=True,
)

_schema = importlib.import_module("jsonschema")
rpds = importlib.import_module("rpds")
_exports = importlib.import_module("bundle_catalog_export")
if hasattr(_exports, "Projection"):
    Projection = _exports.Projection
else:
    Lifecycle = _exports.Lifecycle
    Catalog = _exports.Catalog

if (sys.version_info[:3] != (3, 13, 16) or struct.calcsize("P") != 4
        or "wasm32" not in sysconfig.get_config_var("SOABI")
        or not rpds.rpds.__file__.endswith("/rpds/rpds.abi3.so")):
    raise RuntimeError("original Python 3.13/Wasm and genuine rpds extension required")

_FINDER.seal()
