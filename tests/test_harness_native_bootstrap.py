"""Guest bootstrap/import unit tests, not native authority or Wasm proof."""

from __future__ import annotations

import builtins
import importlib
from pathlib import Path
import sys
import tempfile
from types import ModuleType
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "tools/python313-producer"),
               str(ROOT / "packs/local.ai-tool-bridge-ordered-python")]

from harness_native import bootstrap  # noqa: E402
from harness_native.dispatcher import Dispatcher, OriginalRuntime, PREFIX  # noqa: E402
from harness_native.native_bootstrap import FORMAT, NativeBootstrapInputs  # noqa: E402
from harness_native.native_path import NativePath  # noqa: E402
from harness_native.value_graph import Scope, UnsupportedValue  # noqa: E402
import ordered_json_v2 as wire  # noqa: E402


class NativeBootstrapTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.modules = ("native_binding_fixture_a", "native_binding_fixture_b")
        self.scope = Scope(1, 2, 3, 4)
        self.events = []
        self.bindings = []
        sys.path.insert(0, str(self.root))
        # A separate actual fixture resource location proves Path(__file__) is
        # bound before execution rather than inspecting the guest module file.
        self.native = self.root / "native-resource"
        self.native.mkdir()
        for module in self.modules:
            (self.native / f"{module}.py").write_text("fixture resource", encoding="utf-8")
        self.inventory = {
            "format": FORMAT, "scope": self.scope.wire(),
            "source_index_sha256": "a" * 64, "flavor": "posix",
            "os_flags": {"O_RDONLY": 0}, "cwd_binding": None,
            "paths": [
                {"name": "runtime-root", "binding": "runtime", "value": str(self.native)},
                {"name": "runtime-alias", "binding": "runtime", "value": str(self.native)},
                {"name": "working-root", "binding": "working", "value": str(self.native)},
            ],
            "modules": [
                {"module": module, "binding": f"module_{index}",
                 "value": str(self.native / f"{module}.py")}
                for index, module in enumerate(self.modules)
            ],
        }

    def tearDown(self):
        for binding in self.bindings:
            binding.modules.close()
        for name in self.modules:
            sys.modules.pop(name, None)
        sys.path.remove(str(self.root))
        self.temp.cleanup()

    def port(self, payload):
        frame = wire.loads(payload)
        self.assertEqual(frame["scope"], self.scope.wire())
        self.assertEqual(frame["operation"], "resolve")
        self.assertEqual(frame["args"], [False])
        target = frame["target"]
        self.assertEqual(target["steps"], [])
        index = int(target["binding"].removeprefix("module_"))
        native = self.native / f"{self.modules[index]}.py"
        self.events.append(index)
        return wire.dumps({
            "format": "io.tobkiri.native-inspection-response.v1",
            "scope": self.scope.wire(),
            "result": {"kind": "path", "binding": target["binding"],
                       "value": str(native.resolve())},
        })

    def inputs(self, data=None, port=None):
        result = NativeBootstrapInputs.from_inventory(
            self.scope, wire.dumps(self.inventory if data is None else data),
            self.port if port is None else port,
            index_sha256="a" * 64, path_modules=self.modules,
        )
        self.bindings.append(result)
        return result

    def source(self, index, text):
        (self.root / f"{self.modules[index]}.py").write_text(text, encoding="utf-8")
        importlib.invalidate_caches()

    def test_original_module_constant_uses_bound_path_before_execution(self):
        self.source(0, "from pathlib import Path\n"
                    "RUNTIME_ROOT = Path(__file__).resolve().parents[1]\n")
        inputs = self.inputs()
        original_import = builtins.__import__
        inputs.modules.install()
        module = importlib.import_module(self.modules[0])
        self.assertIs(type(module.RUNTIME_ROOT), NativePath)
        self.assertEqual(str(module.RUNTIME_ROOT), str(self.native.resolve().parent))
        self.assertEqual(module.__file__, str(self.native / f"{self.modules[0]}.py"))
        self.assertEqual(self.events, [0])
        self.assertIs(builtins.__import__, original_import)
        self.assertIs(Path, importlib.import_module("pathlib").Path)

    def test_lazy_module_and_function_imports_keep_same_original_binding(self):
        self.source(0, "def lazy():\n"
                    "    import native_binding_fixture_b as other\n"
                    "    return other.ROOT\n")
        self.source(1, "from pathlib import Path\nROOT = Path(__file__).resolve()\n")
        inputs = self.inputs()
        inputs.modules.install()
        module = importlib.import_module(self.modules[0])
        self.assertEqual(self.events, [])
        result = module.lazy()
        self.assertIs(type(result), NativePath)
        self.assertEqual(str(result), str((self.native / f"{self.modules[1]}.py").resolve()))
        self.assertEqual(self.events, [1])

    def test_missing_extra_duplicate_or_foreign_inventory_is_rejected_before_import(self):
        import copy
        variations = []
        for field, value in [
            ("scope", Scope(1, 2, 3, 5).wire()),
            ("source_index_sha256", "b" * 64),
            ("flavor", []),
            ("modules", self.inventory["modules"][:1]),
            ("modules", self.inventory["modules"] * 2),
        ]:
            data = copy.deepcopy(self.inventory)
            data[field] = value
            variations.append(data)
        data = copy.deepcopy(self.inventory)
        data["modules"][0]["module"] = "not_an_original_module"
        variations.append(data)
        for data in variations:
            with self.assertRaises(UnsupportedValue):
                self.inputs(data)
        self.assertEqual(self.events, [])
        self.assertTrue(all(name not in sys.modules for name in self.modules))

    def test_late_binding_cannot_accept_an_ambient_module_constant(self):
        module = ModuleType(self.modules[0])
        module.ROOT = "unbound prior value"
        sys.modules[module.__name__] = module
        inputs = self.inputs()
        with self.assertRaises(UnsupportedValue):
            inputs.modules.install()
        self.assertEqual(module.ROOT, "unbound prior value")
        self.assertFalse(inputs.modules.installed)

    def test_pure_path_native_constructor_preserves_both_original_flavor_type_checks(self):
        self.source(0, "from pathlib import Path, PurePath, PurePosixPath, PureWindowsPath\n"
                    "BOTH = (isinstance(PurePosixPath('schema.json'), PurePath),\n"
                    "        isinstance(PureWindowsPath('schema.json'), PurePath),\n"
                    "        issubclass(PurePosixPath, PurePath),\n"
                    "        issubclass(PureWindowsPath, PurePath))\n"
                    "NATIVE = Path('schema.json')\n"
                    "WRAPPED = isinstance(NATIVE, PurePath)\n"
                    "LEXICAL = PurePath(NATIVE)\n")
        for flavor, expected in (("windows", "PureWindowsPath"),
                                 ("posix", "PurePosixPath")):
            with self.subTest(flavor=flavor):
                self.inventory["flavor"] = flavor
                inputs = self.inputs()
                inputs.modules.install()
                try:
                    module = importlib.import_module(self.modules[0])
                    self.assertEqual(module.BOTH, (True, True, True, True))
                    self.assertTrue(module.WRAPPED)
                    self.assertEqual(type(module.LEXICAL).__name__, expected)
                    self.assertEqual(str(module.LEXICAL), "schema.json")
                    self.assertEqual(self.events, [])
                finally:
                    inputs.modules.close()
                    sys.modules.pop(self.modules[0], None)

    def test_unrepresented_path_class_cannot_fall_back_to_guest_filesystem(self):
        self.source(0, "from pathlib import PosixPath\nUNSAFE = True\n")
        inputs = self.inputs()
        inputs.modules.install()
        with self.assertRaises(UnsupportedValue):
            importlib.import_module(self.modules[0])
        self.assertNotIn(self.modules[0], sys.modules)
        self.assertEqual(self.events, [])

    def test_native_execution_failure_is_not_normalized_into_a_domain_result(self):
        self.source(0, "from pathlib import Path\nROOT = Path(__file__).resolve()\n")
        failure = MemoryError("original native execution failure")

        def fail(_payload):
            raise failure

        inputs = self.inputs(port=fail)
        inputs.modules.install()
        with self.assertRaises(MemoryError) as raised:
            importlib.import_module(self.modules[0])
        self.assertIs(raised.exception, failure)

    def test_true_aliases_remain_shared_but_equal_roles_stay_distinct(self):
        roots = dict(self.inputs().inputs)
        self.assertIs(roots["runtime-root"], roots["runtime-alias"])
        self.assertIsNot(roots["runtime-root"], roots["working-root"])
        self.assertEqual(roots["runtime-root"], roots["working-root"])

    def test_dispatcher_supplies_validated_scope_before_original_loader_runs(self):
        seen = []

        def load(scope):
            seen.append(scope)
            return OriginalRuntime(object(), (), (), ())

        dispatcher = Dispatcher(load)
        with self.assertRaises(Exception):
            dispatcher.init(wire.dumps({"format": PREFIX + "init.v1", "scope": []}))
        self.assertEqual(seen, [])
        dispatcher.init(wire.dumps({"format": PREFIX + "init.v1", "scope": self.scope.wire()}))
        self.assertEqual(seen, [self.scope])

    def test_real_bootstrap_installs_bindings_before_loading_and_cleans_failed_hook(self):
        inputs = self.inputs()
        failure = ImportError("unavailable actual dependency")

        def original_load(bound):
            self.assertIs(bound, inputs)
            self.assertTrue(bound.modules.installed)
            raise failure

        with patch.object(bootstrap, "installed_inputs", return_value=inputs) as installed:
            with patch.object(bootstrap, "_load_bound_runtime", side_effect=original_load):
                with self.assertRaises(ImportError) as raised:
                    bootstrap.load_original_runtime(self.scope)
        self.assertIs(raised.exception, failure)
        installed.assert_called_once_with(self.scope)
        self.assertFalse(inputs.modules.installed)


if __name__ == "__main__":
    unittest.main()
