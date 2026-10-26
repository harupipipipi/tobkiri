"""Preinitialize genuine dependencies; retain the original Harness adapters."""

import importlib
import struct
import sys
import sysconfig
from types import FunctionType

from harness_native_captured_loader import install
from harness_native_dependency_data import (
    DISTRIBUTIONS,
    FILES,
    NAMESPACES,
    STDLIB_PREIMPORTS,
)
from harness_native_source_index import MODULES, PATH_MODULES


def _preload_native_bindings():
    """Retain only this world's generated runtime shims, before sealing.

    The producer's IDE-only bindings contain NotImplementedError bodies. Its
    real componentize pass generates /world separately. Check the resolved
    module/body layout, without calling either native operation. This is build
    provenance checking, never native operation authorization.
    """
    bindings = importlib.import_module("wit_world.imports.bindings")
    origin = "/world/wit_world/imports/bindings.py"
    runtime = sys.modules.get("componentize_py_runtime")
    if (
        type(bindings) is not type(sys)
        or bindings.__name__ != "wit_world.imports.bindings"
        or bindings.__file__ != origin
        or runtime is None
    ):
        raise RuntimeError("native Harness requires this world's runtime bindings")
    for name, arguments in (("inventory", 0), ("inspect", 1)):
        function = vars(bindings).get(name)
        if (
            type(function) is not FunctionType
            or function.__module__ != bindings.__name__
            or function.__code__.co_name != name
            or function.__code__.co_filename != origin
            or function.__code__.co_argcount != arguments
            or function.__code__.co_kwonlyargcount != 0
            or function.__code__.co_flags & 0x0C
            or function.__code__.co_names != ("componentize_py_runtime", "call_import")
            or function.__globals__.get("componentize_py_runtime") is not runtime
        ):
            raise RuntimeError("native Harness refused IDE or foreign binding code")
    return bindings


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

# Their later imports must retrieve these SAME module/callable objects. Sealed
# capture deliberately has no fallback for uncaptured generated modules.
_NATIVE_BINDINGS = _preload_native_bindings()

if any(name in sys.modules for name in PATH_MODULES) or any(
    name in sys.modules and name.startswith(("core_runtime", "ecosystem."))
    for name, _path, _digest in MODULES
):
    raise RuntimeError("original runtime executed before native binding")

_FINDER.seal()
