"""Small synthetic standard-API tests; no real package or Wasm execution."""

from contextlib import contextmanager
from importlib import metadata, resources
import importlib
import importlib.util
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

HERE = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "captured_loader", HERE / "schema_probe/captured_loader.py"
)
loader = importlib.util.module_from_spec(spec)
spec.loader.exec_module(loader)


@contextmanager
def captured(extra_files=None):
    files = {
        "schema_fixture/__init__.py": b"INITIALIZED = 1\n",
        "schema_fixture/lazy.py": b"from . import INITIALIZED\nANSWER = INITIALIZED + 41\n",
        "schema_fixture/data/value.json": b'{"value":42}\n',
        "schema_fixture-1.0.dist-info/METADATA": b"Metadata-Version: 2.1\nName: schema-fixture\nVersion: 1.0\n\n",
        "schema_fixture-1.0.dist-info/RECORD": b"schema_fixture/data/value.json,,13\n",
    }
    files.update(extra_files or {})
    before = sys.meta_path[:]
    paths = sys.path[:]
    finder = loader.install(
        files, {"schema-fixture": "schema_fixture-1.0.dist-info"}, "/captured-fixture"
    )
    sys.path.insert(0, "/captured-fixture")
    try:
        yield finder
    finally:
        sys.meta_path[:] = before
        sys.path[:] = paths
        for name in list(sys.modules):
            if name == "schema_fixture" or name.startswith("schema_fixture."):
                del sys.modules[name]


class CapturedLoaderTests(unittest.TestCase):
    def test_schema_accessor_retains_actual_execution_and_resource_owner(self):
        with captured({
            "schema_fixture/validation.py": b"LOADED = True\n",
            "schema_fixture/schemas/a.schema.json": b'{"type":"object"}',
        }) as finder:
            module = importlib.import_module("schema_fixture.validation")
            root = module.__loader__._tobkiri_schema_resources(module)
            self.assertIs(root.tree, finder.tree)
            with patch("builtins.open", side_effect=AssertionError("OS fallback")):
                self.assertEqual((root / "a.schema.json").read_bytes(), b'{"type":"object"}')
            self.assertFalse(hasattr(root, "__fspath__"))

    def test_schema_accessor_rejects_forged_module_and_removed_finder(self):
        from types import ModuleType
        with captured({
            "schema_fixture/validation.py": b"LOADED = True\n",
            "schema_fixture/schemas/a.schema.json": b"{}",
        }) as finder:
            module = importlib.import_module("schema_fixture.validation")
            actual = module.__loader__
            forged = ModuleType(module.__name__)
            forged.__dict__.update(vars(module))
            with self.assertRaisesRegex(ImportError, "owner"):
                actual._tobkiri_schema_resources(forged)
            sys.meta_path.remove(finder)
            with self.assertRaisesRegex(ImportError, "owner"):
                actual._tobkiri_schema_resources(module)

    def test_schema_accessor_refuses_missing_resources_and_other_modules(self):
        with captured({"schema_fixture/validation.py": b"LOADED = True\n"}):
            module = importlib.import_module("schema_fixture.validation")
            with self.assertRaisesRegex(FileNotFoundError, "schemas"):
                module.__loader__._tobkiri_schema_resources(module)
            other = importlib.import_module("schema_fixture.lazy")
            with self.assertRaisesRegex(ImportError, "owner"):
                other.__loader__._tobkiri_schema_resources(other)

    def test_explicit_original_namespaces_need_no_initializer_or_filesystem(self):
        files = {"namespace_fixture/domain/value.py": b"ANSWER = 42\n"}
        before, paths = sys.meta_path[:], sys.path[:]
        try:
            finder = loader.install(
                files, {}, "/captured-namespace",
                ("namespace_fixture", "namespace_fixture.domain"),
            )
            finder.seal()
            self.assertIsNone(finder.find_spec("namespace_fixture").loader)
            with patch("builtins.open", side_effect=AssertionError("OS fallback")):
                value = importlib.import_module("namespace_fixture.domain.value")
            self.assertEqual(value.ANSWER, 42)
            self.assertIsNone(sys.modules["namespace_fixture"].__file__)
            self.assertEqual(finder.loaded, ["namespace_fixture.domain.value"])
            self.assertEqual(dict(finder.tree.files), files)
        finally:
            sys.meta_path[:], sys.path[:] = before, paths
            for name in list(sys.modules):
                if name == "namespace_fixture" or name.startswith("namespace_fixture."):
                    del sys.modules[name]
        for namespaces in (("missing",), ("bad/name",), ("namespace_fixture",) * 2):
            with self.subTest(namespaces=namespaces), self.assertRaises(ValueError):
                loader.Finder(files, {}, "/captured-namespace", namespaces)
        with self.assertRaisesRegex(ValueError, "conflicts"):
            loader.Finder({"namespace_fixture/__init__.py": b""}, {},
                          "/captured-namespace", ("namespace_fixture",))

    def test_original_module_and_first_lazy_import_use_the_closed_source(self):
        with captured() as finder:
            module = importlib.import_module("schema_fixture")
            self.assertEqual(module.INITIALIZED, 1)
            self.assertNotIn("schema_fixture.lazy", sys.modules)
            with patch("builtins.open", side_effect=AssertionError("OS fallback")):
                lazy = importlib.import_module("schema_fixture.lazy")
            self.assertEqual(lazy.ANSWER, 42)
            self.assertEqual(lazy.__package__, "schema_fixture")
            self.assertEqual(lazy.__spec__.name, "schema_fixture.lazy")
            self.assertEqual(finder.loaded, ["schema_fixture", "schema_fixture.lazy"])
            self.assertEqual(
                module.__spec__.loader.get_source(module.__name__), "INITIALIZED = 1\n"
            )

    def test_standard_resource_api_reads_and_lists_without_an_os_path(self):
        with captured() as finder:
            module = importlib.import_module("schema_fixture")
            root = resources.files(module)
            with patch("builtins.open", side_effect=AssertionError("OS fallback")):
                data = root.joinpath("data", "value.json")
                self.assertTrue(root.is_dir())
                self.assertTrue(data.is_file())
                self.assertEqual(
                    [p.name for p in (root / "data").iterdir()], ["value.json"]
                )
                self.assertEqual(data.read_bytes(), b'{"value":42}\n')
                self.assertEqual(data.read_text(encoding="utf-8"), '{"value":42}\n')
            self.assertFalse(hasattr(data, "__fspath__"))
            self.assertEqual(finder.tree.reads, 2)

    def test_missing_resources_and_writes_fail_instead_of_falling_back(self):
        with captured() as finder:
            root = finder.tree.path("schema_fixture")
            missing = root / "missing"
            self.assertFalse(missing.is_file())
            self.assertFalse(missing.is_dir())
            with self.assertRaises(FileNotFoundError):
                missing.read_bytes()
            with self.assertRaises(FileNotFoundError):
                list(missing.iterdir())
            with self.assertRaises(IsADirectoryError):
                root.read_bytes()
            with self.assertRaises(NotADirectoryError):
                list((root / "__init__.py").iterdir())
            with self.assertRaises(PermissionError):
                (root / "__init__.py").open("w")
            for path in ("../outside", "/outside", "a//b", "a\\b", "a:b", "a\0b"):
                with self.subTest(path=path), self.assertRaises(ValueError):
                    root.joinpath(path)

    def test_metadata_uses_original_bytes_and_record_files_with_normalized_names(self):
        with captured() as finder:
            self.assertEqual(metadata.version("schema_fixture"), "1.0")
            # Warm only stdlib metadata parsers, then require a fresh read without OS IO.
            with patch("builtins.open", side_effect=AssertionError("OS fallback")):
                distribution = metadata.distribution("Schema.Fixture")
                self.assertEqual(distribution.metadata["Name"], "schema-fixture")
                files = distribution.files
                self.assertEqual(
                    [str(p) for p in files], ["schema_fixture/data/value.json"]
                )
                self.assertEqual(files[0].locate().read_bytes(), b'{"value":42}\n')
            self.assertGreater(finder.metadata_queries, 1)
            context = metadata.DistributionFinder.Context(path=["/not-captured"])
            self.assertEqual(list(finder.find_distributions(context)), [])

    def test_tree_bounds_collisions_and_immutability(self):
        for files in ({"a": b"x", "a/b": b"y"}, {"../x": b"x"}, {"x": "not bytes"}):
            with self.subTest(files=files), self.assertRaises(ValueError):
                loader.Tree(files)
        with patch.object(loader, "MAX_READ", 1):
            with self.assertRaises(ValueError):
                loader.Tree({"x": b"xx"})
        with captured() as finder:
            with self.assertRaises(TypeError):
                finder.tree.files["new"] = b"x"
            self.assertGreater(
                finder.tree.accounted_bytes, sum(map(len, finder.tree.files.values()))
            )

    def test_sealed_unknown_import_refuses_before_filesystem_fallback(self):
        with captured() as finder:
            finder.seal()
            with patch("os.stat", side_effect=AssertionError("filesystem fallback")):
                with self.assertRaisesRegex(ModuleNotFoundError, "source closure"):
                    importlib.import_module("not_a_captured_dependency_fixture")
                self.assertEqual(
                    importlib.import_module("schema_fixture.lazy").ANSWER, 42
                )

    def test_full_prefix_bytes_are_charged_before_index_expansion(self):
        path = "/".join(["x" * 64] * 32) + "/value"
        with patch.object(loader, "MAX_TREE", 8192):
            with self.assertRaisesRegex(ValueError, "index"):
                loader.Tree({path: b"x"})
        with self.assertRaisesRegex(ValueError, "metadata"):
            loader.parts("x" * (loader.MAX_PATH_BYTES + 1))
        with captured() as finder:
            with self.assertRaisesRegex(ValueError, "metadata"):
                finder.tree.path().joinpath(*(["x"] * (loader.MAX_DEPTH + 1)))


if __name__ == "__main__":
    unittest.main()
