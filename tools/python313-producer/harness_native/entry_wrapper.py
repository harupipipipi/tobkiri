"""Preinitialize genuine dependencies; retain the original Harness adapters."""

import importlib
import struct
import sys
import sysconfig

from harness_native_captured_loader import install
from harness_native_dependency_data import (
    DISTRIBUTIONS,
    FILES,
    NAMESPACES,
    STDLIB_PREIMPORTS,
)
from harness_native_source_index import MODULES, PATH_MODULES

_FINDER = install(
    FILES,
    DISTRIBUTIONS,
    __file__.rsplit("/", 1)[0],
    NAMESPACES,
    compressed=True,
)
for _module in STDLIB_PREIMPORTS:
    importlib.import_module(_module)

# These are the real locked packages and the actual pinned Wasm extension.
# Their package/resource/metadata bytes remain available after restoration.
import jsonschema  # noqa: E402, F401
import packaging.specifiers  # noqa: E402, F401
import packaging.version  # noqa: E402, F401
import rpds.rpds  # noqa: E402

if (
    sys.version_info[:3] != (3, 13, 16)
    or struct.calcsize("P") != 4
    or "wasm32" not in str(sysconfig.get_config_var("SOABI"))
    or not rpds.rpds.__file__.endswith("/rpds/rpds.abi3.so")
):
    raise RuntimeError("native Harness requires the original Wasm CPython/rpds")

# Re-export the actual classes, without replacing their dispatcher or bodies.
from app import Runtime, Transport  # noqa: E402, F401

if any(name in sys.modules for name in PATH_MODULES) or any(
    name in sys.modules and name.startswith(("core_runtime", "ecosystem."))
    for name, _path, _digest in MODULES
):
    raise RuntimeError("original runtime executed before native binding")

_FINDER.seal()
