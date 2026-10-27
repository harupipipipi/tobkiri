"""Bounded immutable storage tests using only inert source/resource fixtures."""

from contextlib import contextmanager
import hashlib
from importlib import metadata, resources
import importlib
from pathlib import Path
import sys
import unittest
from unittest.mock import patch
import zlib

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))

from harness_native_dependencies import compress_sources  # noqa: E402
from schema_probe import captured_loader as loader  # noqa: E402


@contextmanager
def captured(files):
    before, paths = sys.meta_path[:], sys.path[:]
    finder = loader.install(
        compress_sources(files),
        {"compressed-fixture": "compressed_fixture-1.dist-info"},
        "/compressed-fixture",
        ("compressed_fixture.namespace",),
        compressed=True,
    )
    sys.path.insert(0, "/compressed-fixture")
    try:
        yield finder
    finally:
        sys.meta_path[:], sys.path[:] = before, paths
        for name in list(sys.modules):
            if name == "compressed_fixture" or name.startswith("compressed_fixture."):
                del sys.modules[name]


FILES = {
    "compressed_fixture/__init__.py": b"ORIGINAL = 1\n",
    "compressed_fixture/namespace/lazy.py": b"from .. import ORIGINAL\nVALUE = ORIGINAL + 41\n",
    "compressed_fixture/resource.bin": bytes(range(256)) * 16,
    "compressed_fixture/resource.txt": ("元の UTF-8 resource\n" * 200).encode(),
    "compressed_fixture-1.dist-info/METADATA": (
        b"Metadata-Version: 2.1\nName: compressed-fixture\nVersion: 1\n\n"
    ),
    "compressed_fixture-1.dist-info/RECORD": b"compressed_fixture/resource.bin,,4096\n",
}


class CompressedLoaderTests(unittest.TestCase):
    def test_membership_iteration_find_spec_and_directory_index_never_expand(self):
        with captured(FILES) as finder:
            with patch.object(
                finder.tree.files, "_decompress", side_effect=AssertionError("expanded")
            ):
                self.assertEqual(set(finder.tree.files), set(FILES))
                self.assertEqual(len(finder.tree.files), len(FILES))
                self.assertIn("compressed_fixture/resource.bin", finder.tree.files)
                self.assertIsNone(
                    finder.find_spec("compressed_fixture.namespace").loader
                )
                self.assertIsNotNone(
                    finder.find_spec("compressed_fixture.namespace.lazy")
                )
                root = finder.tree.path("compressed_fixture")
                self.assertTrue(root.is_dir())
                self.assertTrue((root / "resource.bin").is_file())
                self.assertEqual(len(list(root.iterdir())), 4)
            self.assertEqual(finder.loaded, [])
            self.assertEqual(finder.tree.reads, 0)

    def test_source_resource_metadata_and_first_late_import_keep_original_behavior(
        self,
    ):
        with captured(FILES) as finder:
            module = importlib.import_module("compressed_fixture")
            self.assertEqual(
                module.__loader__.get_source(module.__name__),
                FILES["compressed_fixture/__init__.py"].decode(),
            )
            # Warm the host stdlib adapters as the real dependency preinit does.
            # The fixture supplies no host stdlib source; subsequent member reads
            # still expand and verify their original bytes afresh after sealing.
            resources.files(module)
            self.assertEqual(metadata.version("compressed-fixture"), "1")
            metadata.distribution("compressed-fixture").files
            finder.seal()
            with patch("builtins.open", side_effect=AssertionError("OS fallback")):
                lazy = importlib.import_module("compressed_fixture.namespace.lazy")
                self.assertEqual(lazy.VALUE, 42)
                root = resources.files(module)
                self.assertEqual(
                    (root / "resource.bin").read_bytes(),
                    FILES["compressed_fixture/resource.bin"],
                )
                self.assertEqual(
                    (root / "resource.txt").read_text(encoding="utf-8"),
                    FILES["compressed_fixture/resource.txt"].decode(),
                )
                self.assertEqual(metadata.version("compressed-fixture"), "1")
                distribution = metadata.distribution("compressed-fixture")
                self.assertEqual(
                    distribution.files[0].locate().read_bytes(),
                    FILES["compressed_fixture/resource.bin"],
                )
                with self.assertRaises(ModuleNotFoundError):
                    importlib.import_module("uncaptured_compressed_fixture")
            self.assertEqual(
                finder.loaded,
                ["compressed_fixture", "compressed_fixture.namespace.lazy"],
            )

    def test_source_hash_is_verified_before_compile_and_original_syntax_errors_survive(
        self,
    ):
        raw = b"def invalid(:\n"
        files = {"syntax_fixture.py": raw}
        failures = []
        for source, compressed in ((files, False), (compress_sources(files), True)):
            finder = loader.Finder(source, {}, "/same-original", compressed=compressed)
            spec = finder.find_spec("syntax_fixture")
            with self.assertRaises(SyntaxError) as failure:
                spec.loader.get_code("syntax_fixture")
            failures.append(
                (
                    failure.exception.filename,
                    failure.exception.lineno,
                    failure.exception.offset,
                    failure.exception.text,
                )
            )
        self.assertEqual(failures[0], failures[1])
        rows = list(compress_sources({"syntax_fixture.py": b"x = 1\n" * 100}))
        row = rows[0]
        rows[0] = (*row[:4], "0" * 64)
        finder = loader.Finder(tuple(rows), {}, "/same-original", compressed=True)
        with patch(
            "builtins.compile", side_effect=AssertionError("executed corrupt source")
        ):
            with self.assertRaisesRegex(ValueError, "hash"):
                finder.find_spec("syntax_fixture").loader.get_code("syntax_fixture")

    def test_corrupt_truncated_extra_concatenated_and_hash_tampering_are_rejected(self):
        raw = b"original\n" * 100
        payload = zlib.compress(raw)
        digest = hashlib.sha256(raw).hexdigest()
        cases = (
            payload[:-1],
            payload + b"extra",
            payload + zlib.compress(b"second"),
            payload[:3] + bytes([payload[3] ^ 255]) + payload[4:],
        )
        for changed in cases:
            with self.subTest(payload=changed[:8]):
                tree = loader.Tree(
                    (("file", "zlib", changed, len(raw), digest),), compressed=True
                )
                with self.assertRaises(ValueError):
                    tree.files["file"]
        for codec, stored in (("identity", raw), ("zlib", payload)):
            tree = loader.Tree(
                (("file", codec, stored, len(raw), "0" * 64),), compressed=True
            )
            with self.assertRaisesRegex(ValueError, "hash"):
                tree.files["file"]

    def test_bomb_stops_at_declared_length_plus_one_without_flush_or_cache(self):
        payload = zlib.compress(b"x" * 100_000)
        tree = loader.Tree(
            (("file", "zlib", payload, 8, hashlib.sha256(b"x" * 8).hexdigest()),),
            compressed=True,
        )

        class Decoder:
            def __init__(self):
                self.actual = zlib.decompressobj()

            def decompress(self, data, maximum):
                self.maximum = maximum
                self.output = self.actual.decompress(data, maximum)
                return self.output

            def __getattr__(self, name):
                if name == "flush":
                    raise AssertionError("unbounded flush")
                return getattr(self.actual, name)

        decoder = Decoder()
        with patch.object(tree.files, "_decompress", return_value=decoder):
            with self.assertRaisesRegex(ValueError, "oversized"):
                tree.files["file"]
        self.assertEqual(decoder.maximum, 9)
        self.assertEqual(len(decoder.output), 9)
        self.assertFalse(hasattr(tree.files, "_cache"))

    def test_every_read_decodes_again_and_no_expanded_bytes_are_retained(self):
        raw = b"independently expanded original bytes\n" * 100
        tree = loader.Tree(compress_sources({"file": raw}), compressed=True)
        with patch.object(
            tree.files, "_decompress", wraps=zlib.decompressobj
        ) as decode:
            first, second = tree.files["file"], tree.files["file"]
        self.assertEqual((first, second), (raw, raw))
        self.assertIsNot(first, second)
        self.assertEqual(decode.call_count, 2)
        with self.assertRaises(TypeError):
            tree.files["file"] = b"replacement"
        self.assertIs(type(tree.files._records["file"]), tuple)

    def test_original_file_read_tree_and_manifest_bounds_remain_closed(self):
        row = compress_sources({"file": b"x" * 100})[0]
        invalid = (
            [row],
            (row, row),
            (row + ("extra",),),
            (("../file", *row[1:]),),
            ((row[0], "other", *row[2:]),),
            ((row[0], row[1], row[2], loader.MAX_READ + 1, row[4]),),
        )
        for rows in invalid:
            with (
                self.subTest(rows=type(rows)),
                self.assertRaises((ValueError, TypeError)),
            ):
                loader.Tree(rows, compressed=True)
        with patch.object(loader, "MAX_FILES", 0), self.assertRaises(ValueError):
            loader.Tree((row,), compressed=True)
        with patch.object(loader, "MAX_READ", 8), self.assertRaises(ValueError):
            loader.Tree((row,), compressed=True)
        with patch.object(loader, "MAX_TREE", 100), self.assertRaises(ValueError):
            loader.Tree((row,), compressed=True)
        with self.assertRaises(ValueError):
            loader.Tree({"file": 1})


if __name__ == "__main__":
    unittest.main()
