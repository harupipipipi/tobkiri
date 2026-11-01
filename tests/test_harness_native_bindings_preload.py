"""Inert generated-module fixtures; no original runtime, guest or native IO."""
from __future__ import annotations

import ast
from contextlib import contextmanager
import importlib
import importlib.machinery
from pathlib import Path
import sys
from types import FunctionType, ModuleType
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools/python313-producer"))
from schema_probe import captured_loader  # noqa: E402

ORIGIN = "/world/wit_world/imports/bindings.py"
WRAPPER = ROOT / "tools/python313-producer/harness_native/entry_wrapper.py"
RUNTIME_SHIM_FIXTURE = (
    "import componentize_py_runtime\n"
    "def inventory():\n"
    "    return componentize_py_runtime.call_import(0, [])\n"
    "def inspect(payload):\n"
    "    return componentize_py_runtime.call_import(1, [payload])\n"
)


class GeneratedFixture:
    """Synthetic producer layout; callable results are never host evidence."""

    def __init__(self, source):
        self.source = source
        self.requests = []

    def find_spec(self, fullname, path=None, target=None):
        self.requests.append(fullname)
        if fullname not in {
            "wit_world", "wit_world.imports", "wit_world.imports.bindings",
            "wit_world.imports.unrequested",
        }:
            return None
        package = fullname in {"wit_world", "wit_world.imports"}
        origin = "/world/" + fullname.replace(".", "/")
        origin += "/__init__.py" if package else ".py"
        spec = importlib.machinery.ModuleSpec(
            fullname, self, origin=origin, is_package=package,
        )
        spec.has_location = True
        return spec

    def create_module(self, spec):
        return None

    def exec_module(self, module):
        if module.__name__ == "wit_world.imports.bindings":
            exec(compile(self.source, module.__file__, "exec"), vars(module))


@contextmanager
def environment(source=RUNTIME_SHIM_FIXTURE):
    """Restore only the synthetic modules and the import hooks we own."""
    def selected(name):
        return (name == "componentize_py_runtime" or name == "wit_world"
                or name.startswith("wit_world."))

    modules = {name: value for name, value in sys.modules.items() if selected(name)}
    paths, meta = sys.path[:], sys.meta_path[:]
    for name in modules:
        del sys.modules[name]
    calls = []
    runtime = ModuleType("componentize_py_runtime")
    runtime.call_import = lambda index, args: calls.append((index, args)) or "fixture"
    sys.modules[runtime.__name__] = runtime
    finder = captured_loader.install({}, {}, "/inert-captured-native-fixture")
    generated = GeneratedFixture(source)
    sys.meta_path.insert(1, generated)
    try:
        yield finder, generated, calls
    finally:
        sys.path[:], sys.meta_path[:] = paths, meta
        for name in list(sys.modules):
            if selected(name):
                del sys.modules[name]
        sys.modules.update(modules)


def preload():
    """Execute only the actual pure preload helper, excluding wrapper imports."""
    tree = ast.parse(WRAPPER.read_text())
    function = next(node for node in tree.body if isinstance(node, ast.FunctionDef)
                    and node.name == "_preload_native_bindings")
    namespace = {"importlib": importlib, "sys": sys, "FunctionType": FunctionType}
    exec(compile(ast.Module(body=[function], type_ignores=[]), str(WRAPPER), "exec"),
         namespace)
    return namespace[function.name]()


class NativeBindingPreloadTests(unittest.TestCase):
    def test_unpreloaded_binding_is_refused_by_the_same_sealed_loader(self):
        with environment() as (finder, generated, calls):
            finder.seal()
            with self.assertRaises(ModuleNotFoundError):
                preload()
            self.assertEqual(generated.requests, [])
            self.assertEqual(calls, [])

    def test_preload_retains_identity_without_calling_or_opening_unknown_imports(self):
        with environment() as (finder, generated, calls):
            bindings = preload()
            self.assertEqual(calls, [])
            self.assertEqual(bindings.__file__, ORIGIN)
            inventory, inspect = bindings.inventory, bindings.inspect
            finder.seal()
            requests = generated.requests[:]
            again = importlib.import_module("wit_world.imports.bindings")
            self.assertIs(again, bindings)
            self.assertIs(again.inventory, inventory)
            self.assertIs(again.inspect, inspect)
            with self.assertRaises(ModuleNotFoundError):
                importlib.import_module("wit_world.imports.unrequested")
            self.assertEqual(generated.requests, requests)
            self.assertEqual(calls, [])
            self.assertEqual(again.inventory(), "fixture")
            self.assertEqual(again.inspect("input"), "fixture")
            self.assertEqual(calls, [(0, []), (1, ["input"])])

    def test_ide_stub_at_the_same_path_is_not_runtime_binding_proof(self):
        source = ("def inventory():\n    raise NotImplementedError\n"
                  "def inspect(payload):\n    raise NotImplementedError\n")
        with environment(source) as (_finder, _generated, calls):
            with self.assertRaisesRegex(RuntimeError, "IDE or foreign"):
                preload()
            self.assertEqual(calls, [])

    def test_current_wrapper_preloads_before_sealing_and_rechecks_original_modules(self):
        tree = ast.parse(WRAPPER.read_text())
        positions = {}
        for index, node in enumerate(tree.body):
            if isinstance(node, ast.Assign) and any(
                isinstance(target, ast.Name) and target.id == "_NATIVE_BINDINGS"
                for target in node.targets
            ):
                positions["preload"] = index
            elif isinstance(node, ast.If):
                positions["original_guard"] = index
            elif (isinstance(node, ast.Expr) and isinstance(node.value, ast.Call)
                  and ast.unparse(node.value.func) == "_FINDER.seal"):
                positions["seal"] = index
        self.assertLess(positions["preload"], positions["original_guard"])
        self.assertLess(positions["original_guard"], positions["seal"])


if __name__ == "__main__":
    unittest.main()
