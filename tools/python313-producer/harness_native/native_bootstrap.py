"""Consume fixed native inputs before importing the original runtime closure.

Inventory is installed native metadata, not a filesystem grant or a choice of
code supplied by a frame. Actual native owner/target/phase/custody checks remain
mandatory in the typed native import implementation before every operation.
"""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any, Callable

import ordered_json_v2 as wire

from .native_module_loader import ModulePathBindings
from .native_path import NativePath, PathBridge
from .value_graph import InvalidFrame, Scope, UnsupportedValue

FORMAT = "io.tobkiri.native-input-inventory.v1"
_NAME = re.compile(r"[a-z][a-z0-9-]{0,63}")


@dataclass(frozen=True)
class NativeBootstrapInputs:
    """One invocation's actual opaque roots and pre-import resource anchors."""

    bridge: PathBridge
    inputs: tuple[tuple[str, NativePath], ...]
    modules: ModulePathBindings

    @classmethod
    def from_inventory(
        cls, scope: Scope, payload: str, inspect: Callable[[str], str],
        *, index_sha256: str, path_modules: tuple[str, ...],
    ) -> NativeBootstrapInputs:
        try:
            data = wire.loads(payload)
        except wire.OrderedJSONError:
            raise UnsupportedValue("invalid native input inventory") from None
        fields = {"format", "scope", "source_index_sha256", "flavor", "os_flags",
                  "cwd_binding", "paths", "modules"}
        if type(data) is not dict or set(data) != fields or data["format"] != FORMAT:
            raise UnsupportedValue("invalid native input inventory")
        try:
            actual_scope = Scope.from_wire(data["scope"])
        except InvalidFrame:
            raise UnsupportedValue("invalid native input scope") from None
        if actual_scope != scope or data["source_index_sha256"] != index_sha256:
            raise UnsupportedValue("native input scope or source index differs")
        if (type(path_modules) is not tuple or not path_modules
                or len(set(path_modules)) != len(path_modules)
                or type(data["paths"]) is not list or type(data["modules"]) is not list):
            raise UnsupportedValue("invalid native input source contract")
        bridge = PathBridge(
            scope, data["flavor"], inspect, os_flags=data["os_flags"],
            cwd_binding=data["cwd_binding"],
        )
        # Same native-issued object ID/value preserves a true alias. Equal
        # lexical values with different native bindings never merge roles.
        paths: dict[str, NativePath] = {}

        def bind(binding: Any, value: Any) -> NativePath:
            bound = bridge.bind(binding, value)
            previous = paths.get(binding)
            if previous is not None:
                if str(previous) != str(bound):
                    raise UnsupportedValue("native binding has conflicting lexical values")
                return previous
            paths[binding] = bound
            return bound

        inputs = []
        names = set()
        for row in data["paths"]:
            if (type(row) is not dict or set(row) != {"name", "binding", "value"}
                    or type(row["name"]) is not str or _NAME.fullmatch(row["name"]) is None
                    or row["name"] in names):
                raise UnsupportedValue("invalid native input path binding")
            names.add(row["name"])
            inputs.append((row["name"], bind(row["binding"], row["value"])))
        anchors = {}
        expected = set(path_modules)
        for row in data["modules"]:
            if (type(row) is not dict or set(row) != {"module", "binding", "value"}
                    or type(row["module"]) is not str or row["module"] not in expected
                    or row["module"] in anchors):
                raise UnsupportedValue("invalid native module binding")
            anchors[row["module"]] = bind(row["binding"], row["value"])
        if set(anchors) != expected:
            raise UnsupportedValue("native module anchor set is incomplete")
        return cls(bridge, tuple(inputs), ModulePathBindings(bridge, anchors))


def installed_inputs(scope: Scope) -> NativeBootstrapInputs:
    """Use the component's fixed WIT imports and immutable generated index."""
    from harness_native_source_index import INDEX_SHA256, PATH_MODULES
    from wit_world.imports.bindings import inspect, inventory

    return NativeBootstrapInputs.from_inventory(
        scope, inventory(), inspect,
        index_sha256=INDEX_SHA256, path_modules=PATH_MODULES,
    )
