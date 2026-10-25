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
def captured():
    files = {
        "schema_fixture/__init__.py": b"INITIALIZED = 1\n",
        "schema_fixture/lazy.py": b"from . import INITIALIZED\nANSWER = INITIALIZED + 41\n",
        "schema_fixture/data/value.json": b'{"value":42}\n',
        "schema_fixture-1.0.dist-info/METADATA": b"Metadata-Version: 2.1\nName: schema-fixture\nVersion: 1.0\n\n",
        "schema_fixture-1.0.dist-info/RECORD": b"schema_fixture/data/value.json,,13\n",
    }
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
