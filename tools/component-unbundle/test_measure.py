"""Framing and original-byte binding regressions; no worker or guest executes."""
import hashlib
import io
import struct
import unittest

from measure import INPUT_LIMIT, MAX_MODULES, inspect_wire


class ExtractionBindingTests(unittest.TestCase):
    def wire(self, component=b"component", modules=(b"original-module",)):
        return (
            b"TBUNB001" + struct.pack("<II", len(modules), len(component))
            + component + b"".join(struct.pack("<I", len(m)) + m for m in modules)
        )

    def expected(self, modules=(b"original-module",)):
        return [{"bytes": len(m), "sha256": hashlib.sha256(m).hexdigest()}
                for m in modules]

    def test_exact_module_binding_and_stable_import_order(self):
        modules = (b"first-module", b"second-module", b"first-module")
        result = inspect_wire(io.BytesIO(self.wire(modules=modules)), self.expected(modules))
        self.assertEqual([m["import"] for m in result["modules"]],
                         ["unbundled-module0", "unbundled-module1", "unbundled-module2"])
        self.assertEqual(result["modules"][0]["sha256"],
                         result["modules"][2]["sha256"])

    def test_changed_module_with_same_length_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "original bytes"):
            inspect_wire(io.BytesIO(self.wire(modules=(b"tampered-module",))), self.expected())

    def test_wrong_count_and_oversized_count_are_rejected(self):
        for count in (0, 2, MAX_MODULES + 1):
            raw = b"TBUNB001" + struct.pack("<II", count, 8)
            with self.assertRaisesRegex(ValueError, "count"):
                inspect_wire(io.BytesIO(raw), self.expected())

    def test_wrapper_and_module_sizes_are_bounded_before_read(self):
        for raw in (
            b"TBUNB001" + struct.pack("<II", 1, INPUT_LIMIT + 1),
            b"TBUNB001" + struct.pack("<II", 1, 8) + b"12345678"
            + struct.pack("<I", INPUT_LIMIT + 1),
        ):
            with self.assertRaisesRegex(ValueError, "per-input bound"):
                inspect_wire(io.BytesIO(raw), self.expected())

    def test_truncated_payload_and_nonempty_trailer_are_rejected(self):
        for raw in (self.wire()[:-1], self.wire() + b"trailer"):
            with self.assertRaises(ValueError):
                inspect_wire(io.BytesIO(raw), self.expected())

    def test_missing_module_and_duplicate_module_cannot_replace_original_order(self):
        modules = (b"first-module", b"second-module")
        for actual in ((modules[0],), (modules[0], modules[0]), modules[::-1]):
            with self.assertRaises(ValueError):
                inspect_wire(io.BytesIO(self.wire(modules=actual)), self.expected(modules))

    def test_unknown_version_and_truncated_header_are_rejected(self):
        for raw in (b"TBUNB002" + self.wire()[8:], b"TBUNB001", b"bad"):
            with self.assertRaises(ValueError):
                inspect_wire(io.BytesIO(raw), self.expected())


if __name__ == "__main__":
    unittest.main()
