"""Exact-byte and malformed-input regressions for static module inventory."""
from __future__ import annotations

from pathlib import Path
import io
import tempfile
import unittest
from unittest.mock import patch

import component_module_inventory as inventory


def _length(value: int) -> bytes:
    result = bytearray()
    while True:
        byte = value & 127
        value >>= 7
        result.append(byte | (128 if value else 0))
        if not value:
            return bytes(result)


def section(kind: int, payload: bytes) -> bytes:
    return bytes([kind]) + _length(len(payload)) + payload


def component(*payloads: bytes) -> bytes:
    return inventory.COMPONENT_HEADER + b"".join(payloads)


class ModuleInventoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)

    def write(self, name: str, data: bytes) -> Path:
        path = self.root / name
        path.write_bytes(data)
        return path

    def test_identical_full_modules_are_distinct_from_changed_bytes(self) -> None:
        module = inventory.MODULE_HEADER + b"\0\1\0"
        left = self.write("a.wasm", component(section(1, module)))
        right = self.write("b.wasm", component(
            section(1, module), section(1, module[:-1] + b"x")))
        report = inventory.inventory([left, right])
        self.assertEqual(len(report["shared_modules"]), 1)
        self.assertEqual(report["shared_core_module_bytes"], len(module))
        self.assertEqual(report["potential_repeated_module_bytes"], len(module))
        self.assertEqual(report["shared_modules"][0]["components"], [0, 1])
        self.assertEqual(report["components"][0]["modules"][0]["offset"], 10)
        self.assertEqual(report["extraction_preserves_abi_state_authority"],
                         "unverified")

    def test_nested_module_offsets_and_paths_are_absolute(self) -> None:
        data = component(section(4, component(
            section(1, inventory.MODULE_HEADER))))
        path = self.write("nested.wasm", data)
        found = inventory.inspect_component(path)["modules"][0]
        self.assertEqual(found["section"], [0, 0])
        self.assertEqual(data[found["offset"]:found["offset"] + found["bytes"]],
                         inventory.MODULE_HEADER)

    def test_repetition_inside_one_input_is_not_cross_pack_sharing(self) -> None:
        path = self.write("a.wasm", component(
            section(1, inventory.MODULE_HEADER),
            section(1, inventory.MODULE_HEADER)))
        self.assertEqual(inventory.inventory([path])["shared_modules"], [])

    def test_report_does_not_relabel_over_limit_catalog_as_admitted(self) -> None:
        left = self.write("a.wasm", component(section(1, inventory.MODULE_HEADER)))
        right = self.write("b.wasm", component(section(1, inventory.MODULE_HEADER)))
        with patch.object(inventory, "CURRENT_CATALOG_INPUT_LIMIT", 20):
            report = inventory.inventory([left, right])
        self.assertFalse(report["within_current_catalog_input_limit"])
        self.assertNotIn("passed", report)
        self.assertIn("no Wasm validation", report["scope"])

    def test_duplicate_component_paths_are_rejected(self) -> None:
        path = self.write("a.wasm", component())
        with self.assertRaisesRegex(ValueError, "duplicate"):
            inventory.inventory([path, self.root / "." / "a.wasm"])

    def test_invalid_headers_and_framing_are_rejected(self) -> None:
        cases = [
            inventory.MODULE_HEADER,
            component(b"\x01"),
            component(b"\x01\x80\x80\x80\x80\x10"),
            component(b"\x01\x09" + inventory.MODULE_HEADER),
            component(section(1, b"short")),
            component(section(1, inventory.COMPONENT_HEADER)),
            component(section(4, inventory.MODULE_HEADER)),
            component(b"\x0c\0"),
        ]
        for index, data in enumerate(cases):
            with self.subTest(index=index):
                path = self.write(f"bad-{index}.wasm", data)
                with self.assertRaises(ValueError):
                    inventory.inspect_component(path)

    def test_file_depth_section_and_module_bounds_are_enforced(self) -> None:
        path = self.write("bound.wasm", component(
            section(1, inventory.MODULE_HEADER),
            section(1, inventory.MODULE_HEADER)))
        for name, limit in [("MAX_COMPONENT_BYTES", 8), ("MAX_MODULES", 1),
                            ("MAX_SECTIONS", 1)]:
            with self.subTest(name=name), patch.object(inventory, name, limit):
                with self.assertRaises(ValueError):
                    inventory.inspect_component(path)
        path.write_bytes(component(section(4, component())))
        with patch.object(inventory, "MAX_DEPTH", 0):
            with self.assertRaises(ValueError):
                inventory.inspect_component(path)

    def test_empty_or_excessive_input_lists_are_rejected(self) -> None:
        with self.assertRaises(ValueError):
            inventory.inventory([])
        path = self.write("a.wasm", component())
        with patch.object(inventory, "MAX_COMPONENTS", 1):
            with self.assertRaises(ValueError):
                inventory.inventory([path, path])

    def test_module_count_is_also_bounded_across_input_files(self) -> None:
        data = component(section(1, inventory.MODULE_HEADER))
        paths = [self.write("a.wasm", data), self.write("b.wasm", data)]
        with patch.object(inventory, "MAX_MODULES", 1):
            with self.assertRaisesRegex(ValueError, "total module count"):
                inventory.inventory(paths)

    def test_hash_reads_use_bounded_chunks(self) -> None:
        class BoundedReader(io.BytesIO):
            def read(self, size: int = -1) -> bytes:
                if not 0 < size <= inventory.CHUNK_BYTES:
                    raise AssertionError("unbounded binary read")
                return super().read(size)

        stream = BoundedReader(b"x" * (inventory.CHUNK_BYTES * 3 + 1))
        digest = inventory._hash_region(stream, 0, len(stream.getvalue()))
        self.assertEqual(len(digest), 64)

    def test_symlink_input_is_not_followed(self) -> None:
        path = self.write("a.wasm", component())
        link = self.root / "link.wasm"
        try:
            link.symlink_to(path)
        except (OSError, NotImplementedError):
            self.skipTest("symlinks unavailable on this test platform")
        with self.assertRaisesRegex(ValueError, "regular file"):
            inventory.inspect_component(link)


if __name__ == "__main__":
    unittest.main()
