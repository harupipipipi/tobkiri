"""Actual captured Loader plus Path binding; portable IO ports, not Wasm proof."""

from __future__ import annotations

import builtins
from contextlib import contextmanager
import importlib
import importlib.machinery
import importlib.util
from pathlib import Path
import sys
from types import ModuleType
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "tools/python313-producer"),
                str(ROOT / "packs/local.ai-tool-bridge-ordered-python")]

from schema_probe.captured_loader import Loader, Tree, install  # noqa: E402
from harness_native.native_module_loader import ModulePathBindings, _ModuleLoader  # noqa: E402
from harness_native.native_path import NativePath, PathBridge  # noqa: E402
from harness_native.value_graph import Registry, Scope, UnsupportedValue  # noqa: E402
import ordered_json_v2 as wire  # noqa: E402


PACKAGE = "native_owner_fixture"
VALIDATION = PACKAGE + ".validation"
EXTRA = PACKAGE + ".extra"
BROKEN = PACKAGE + ".broken"
ORIGIN = "/captured-native-owner"
VALIDATION_SOURCE = b"""import sys
from pathlib import Path
FILE_VALUE = __file__
EAGER = Path(__file__).with_name('schemas')
def lazy_path():
    from pathlib import Path
    return Path(__file__).parent
def lazy_import():
    from . import extra
    return extra.ROOT
def native_resolve():
    return Path(__file__).resolve()
def schema_resource():
    return __loader__._tobkiri_schema_resources(sys.modules[__name__])
"""


class RetiredPort(Exception):
    """Controlled Host-retirement fixture, not actual Store/Join evidence."""


@contextmanager
def bound():
    files = {
        PACKAGE + "/__init__.py": b"PACKAGE_INITIALIZED = True\n",
        PACKAGE + "/validation.py": VALIDATION_SOURCE,
        PACKAGE + "/extra.py": b"from pathlib import Path\nROOT = Path(__file__).parent\n",
        PACKAGE + "/broken.py": b"from pathlib import Path\nEAGER = Path(__file__).parent\nraise ValueError('fixture init failure')\n",
        PACKAGE + "/schemas/a.schema.json": b'{"type":"object"}',
    }
    previous = sys.meta_path[:]
    scope = Scope(1, 2, 3, 4)
    events = []
    state = {"retired": False}
    paths = {"module_0": "/selected-native/app/validation.py",
             "module_1": "/selected-native/app/extra.py",
             "module_2": "/selected-native/app/broken.py"}

    def inspect(payload):
        if state["retired"]:
            raise RetiredPort()
        frame = wire.loads(payload)
        assert frame["scope"] == scope.wire()
        assert frame["operation"] == "resolve" and frame["args"] == [False]
        assert frame["target"]["steps"] == []
        binding = frame["target"]["binding"]
        events.append(binding)
        return wire.dumps({"format": "io.tobkiri.native-inspection-response.v1",
                           "scope": scope.wire(), "result": {
                               "kind": "path", "binding": binding,
                               "value": paths[binding]}})

    finder = install(files, {}, ORIGIN)
    finder.seal()
    bridge = PathBridge(scope, "posix", inspect, os_flags={"O_RDONLY": 0})
    anchors = {name: bridge.bind(binding, paths[binding]) for name, binding in
               ((VALIDATION, "module_0"), (EXTRA, "module_1"), (BROKEN, "module_2"))}
    bindings = ModulePathBindings(bridge, anchors)
    bindings.install()
    try:
        yield finder, bridge, bindings, events, state
    finally:
        bindings.close()
        sys.meta_path[:] = previous
        for name in list(sys.modules):
            if name == PACKAGE or name.startswith(PACKAGE + "."):
                del sys.modules[name]


class OriginalLoaderBindingTests(unittest.TestCase):
    def test_actual_owner_executes_once_and_keeps_original_file_spec_and_resources(self):
        with bound() as (finder, _, _, events, _):
            executions = []
            original = Loader.exec_module

            def observe(owner, module):
                executions.append((owner, module))
                return original(owner, module)

            with patch.object(Loader, "exec_module", observe):
                with patch("builtins.open", side_effect=AssertionError("OS fallback")):
                    module = importlib.import_module(VALIDATION)
                    resource = module.schema_resource()
                    data = (resource / "a.schema.json").read_bytes()
            owner = module.__loader__
            self.assertIs(type(owner), Loader)
            self.assertIs(module.__spec__.loader, owner)
            self.assertIs(owner._executed_module(), module)
            self.assertIs(owner.finder, finder)
            self.assertIs(owner._source_tree, finder.tree)
            self.assertEqual(module.__file__, ORIGIN + "/" + PACKAGE + "/validation.py")
            self.assertEqual(module.__spec__.origin, module.__file__)
            self.assertIs(type(module.FILE_VALUE), str)
            self.assertEqual(module.FILE_VALUE, module.__file__)
            self.assertEqual([value for value in executions if value[1] is module], [(owner, module)])
            self.assertEqual(finder.loaded.count(VALIDATION), 1)
            self.assertEqual(data, b'{"type":"object"}')
            self.assertIs(resource.tree, owner._source_tree)
            self.assertFalse(hasattr(resource, "__fspath__"))
            self.assertIs(type(data), bytes)
            registry = Registry(Scope(1, 2, 3, 4))
            for opaque in (resource, owner):
                with self.assertRaises(UnsupportedValue):
                    registry.retain(opaque)
            self.assertEqual(events, [])

    def test_eager_lazy_and_nested_imports_use_same_native_anchor_and_module_globals(self):
        with bound() as (finder, _, _, events, _):
            original_import = builtins.__import__
            module = importlib.import_module(VALIDATION)
            self.assertIs(type(module.EAGER), NativePath)
            self.assertEqual(str(module.EAGER), "/selected-native/app/schemas")
            self.assertIs(module.lazy_path.__globals__, module.__dict__)
            self.assertEqual(str(module.lazy_path()), "/selected-native/app")
            self.assertNotIn(EXTRA, sys.modules)
            self.assertEqual(str(module.lazy_import()), "/selected-native/app")
            self.assertIs(type(sys.modules[EXTRA].__loader__), Loader)
            self.assertIs(sys.modules[EXTRA].__loader__, sys.modules[EXTRA].__spec__.loader)
            self.assertEqual(finder.loaded.count(EXTRA), 1)
            self.assertEqual(str(module.native_resolve()), "/selected-native/app/validation.py")
            self.assertEqual(events, ["module_0"])
            self.assertIs(builtins.__import__, original_import)
            self.assertIs(Path, importlib.import_module("pathlib").Path)

    def test_forged_module_loader_file_finder_and_source_tree_are_rejected(self):
        with bound() as (finder, _, _, _, _):
            module = importlib.import_module(VALIDATION)
            owner = module.__loader__
            accessor = owner._tobkiri_schema_resources
            forged = ModuleType(VALIDATION)
            forged.__dict__.update(vars(module))
            with self.assertRaises(ImportError):
                accessor(forged)
            for holder, name, replacement in (
                (module, "__loader__", object()),
                (module.__spec__, "loader", object()),
                (module, "__file__", "/unowned/source.py"),
                (finder, "tree", Tree(dict(finder.tree.files))),
            ):
                previous = getattr(holder, name)
                setattr(holder, name, replacement)
                try:
                    with self.assertRaises(ImportError):
                        accessor(module)
                finally:
                    setattr(holder, name, previous)
            sys.modules[VALIDATION] = forged
            try:
                with self.assertRaises(ImportError):
                    accessor(module)
            finally:
                sys.modules[VALIDATION] = module
            sys.meta_path.remove(finder)
            with self.assertRaises(ImportError):
                accessor(module)
            sys.meta_path.insert(1, finder)
            self.assertIs(accessor(module).tree, finder.tree)

    def test_failed_initialization_has_no_execution_witness_and_reimport_is_explicit(self):
        with bound() as (finder, _, bindings, _, _):
            owners = []
            original = Loader.exec_module

            def observe(owner, module):
                if module.__name__ == BROKEN:
                    owners.append((owner, module))
                return original(owner, module)

            with patch.object(Loader, "exec_module", observe):
                for _ in range(2):
                    with self.assertRaisesRegex(ValueError, "fixture init failure"):
                        importlib.import_module(BROKEN)
                    self.assertNotIn(BROKEN, sys.modules)
            self.assertEqual(finder.loaded.count(BROKEN), 2)
            self.assertEqual(len(owners), 2)
            self.assertIsNot(owners[0][0], owners[1][0])
            for owner, module in owners:
                self.assertIsNone(owner._executed_module)
                self.assertIs(module.__loader__, owner)
                self.assertIs(module.__spec__.loader, owner)
                self.assertIs(type(module.EAGER), NativePath)
                with self.assertRaises(ImportError):
                    owner._tobkiri_schema_resources(module)
            self.assertTrue(bindings.installed)

    def test_native_hook_close_preserves_other_owner_and_host_retirement_is_separate(self):
        with bound() as (finder, _, bindings, events, state):
            module = importlib.import_module(VALIDATION)
            owner = module.__loader__
            bindings.close()
            self.assertNotIn(bindings, sys.meta_path)
            self.assertIn(finder, sys.meta_path)
            self.assertIs(sys.modules[VALIDATION], module)
            self.assertEqual((owner._tobkiri_schema_resources(module) / "a.schema.json").read_bytes(), b'{"type":"object"}')
            # close removes the import hook only. It does NOT claim to revoke
            # existing NativePath objects or synthesize Host IO Join/retirement.
            self.assertEqual(str(module.native_resolve()), "/selected-native/app/validation.py")
            self.assertEqual(events, ["module_0"])
            state["retired"] = True
            with self.assertRaises(RetiredPort):
                module.native_resolve()
            self.assertEqual(events, ["module_0"])
            with self.assertRaises(UnsupportedValue):
                bindings.install()
            sys.meta_path.remove(finder)
            with self.assertRaises(ImportError):
                owner._tobkiri_schema_resources(module)

    def test_unknown_origin_hooks_and_get_code_only_loaders_have_no_fallback(self):
        class UnknownOrigin:
            def __str__(self):
                raise AssertionError("must not stringify unknown origin")

            def __fspath__(self):
                raise AssertionError("must not convert unknown origin")

        class CodeOnly:
            def get_code(self, _name):
                raise AssertionError("must not execute fallback code")

        bridge = PathBridge(Scope(1, 2, 3, 4), "posix", lambda _: None,
                            os_flags={"O_RDONLY": 0})
        anchor = bridge.bind("module_0", "/selected-native/module.py")
        spec = importlib.machinery.ModuleSpec("unrepresented", CodeOnly(), origin=UnknownOrigin())
        spec.has_location = True
        with self.assertRaises(UnsupportedValue):
            _ModuleLoader(spec, bridge, anchor)
        spec.origin = "/captured-source/unrepresented.py"
        wrapper = _ModuleLoader(spec, bridge, anchor)
        spec.loader = wrapper
        module = importlib.util.module_from_spec(spec)
        with self.assertRaises(UnsupportedValue):
            wrapper.exec_module(module)


if __name__ == "__main__":
    unittest.main()
