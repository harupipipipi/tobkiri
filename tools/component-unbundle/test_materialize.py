"""Private publication/framing tests; fake worker output is not runtime proof."""
import hashlib
import io
import json
from pathlib import Path
import struct
import tempfile
import unittest
from unittest.mock import patch

import materialize
from measure import inspect_wire


class PublicationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.worker = self.root / "worker"
        self.worker.write_bytes(b"trusted-worker-fixture")
        self.sources = [self.root / "first.wasm", self.root / "second.wasm"]
        for source in self.sources:
            source.write_bytes(b"original-component-fixture")
        self.destination = self.root / "published"

    def extract(self, _worker, source, destination):
        code = b"original-shared-code"
        wrapper = b"transformed-wrapper"
        wire = (b"TBUNB001" + struct.pack("<II", 1, len(wrapper)) + wrapper
                + struct.pack("<I", len(code)) + code)
        expected = [{"bytes": len(code), "sha256": hashlib.sha256(code).hexdigest()}]
        result = inspect_wire(io.BytesIO(wire), expected, destination)
        return {"source": str(source), "source_bytes": source.stat().st_size,
                "source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
                "source_module_count": 1, "component": result}

    def test_published_bytes_and_receipt_match_without_guest_execution(self):
        with patch.object(materialize, "extract", side_effect=self.extract):
            receipt = materialize.materialize(self.worker, self.sources, self.destination)
        self.assertEqual(json.loads((self.destination / "extraction.json").read_text()), receipt)
        self.assertFalse(receipt["guest_execution_performed"])
        self.assertFalse(receipt["production_enabled"])
        self.assertEqual(receipt["unique_modules"], 1)
        self.assertEqual(receipt["catalog_source_bytes"],
                         2 * len(b"transformed-wrapper") + len(b"original-shared-code"))
        for index in range(2):
            self.assertEqual((self.destination / str(index) / "component.wasm").read_bytes(),
                             b"transformed-wrapper")
            self.assertEqual((self.destination / str(index) / "module-0.wasm").read_bytes(),
                             b"original-shared-code")

    def test_partial_worker_failure_never_publishes(self):
        calls = 0
        def fail_second(*args):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise ValueError("worker failure after first artifact")
            return self.extract(*args)
        with patch.object(materialize, "extract", side_effect=fail_second):
            with self.assertRaisesRegex(ValueError, "worker failure"):
                materialize.materialize(self.worker, self.sources, self.destination)
        self.assertFalse(self.destination.exists())
        self.assertFalse(list(self.root.glob(".unbundle-*")))

    def test_existing_destination_and_symlink_source_are_preserved(self):
        self.destination.mkdir()
        marker = self.destination / "keep"
        marker.write_bytes(b"existing")
        with self.assertRaises(ValueError):
            materialize.materialize(self.worker, self.sources, self.destination)
        self.assertEqual(marker.read_bytes(), b"existing")
        link = self.root / "link.wasm"
        link.symlink_to(self.sources[0])
        with self.assertRaises((ValueError, OSError)):
            materialize.materialize(self.worker, [link], self.root / "linked")
        self.assertFalse((self.root / "linked").exists())

    def test_catalog_limit_failure_cleans_private_output(self):
        with patch.object(materialize, "extract", side_effect=self.extract), \
             patch.object(materialize, "OUTPUT_LIMIT", 8):
            with self.assertRaisesRegex(ValueError, "catalog"):
                materialize.materialize(self.worker, self.sources, self.destination)
        self.assertFalse(self.destination.exists())
        self.assertFalse(list(self.root.glob(".unbundle-*")))

    def test_duplicate_inputs_do_not_enter_worker(self):
        with patch.object(materialize, "extract") as worker:
            with self.assertRaisesRegex(ValueError, "duplicate"):
                materialize.materialize(self.worker, [self.sources[0]] * 2, self.destination)
        worker.assert_not_called()

    def test_total_source_module_bound_cleans_private_output(self):
        with patch.object(materialize, "extract", side_effect=self.extract), \
             patch.object(materialize, "MAX_MODULES", 1):
            with self.assertRaisesRegex(ValueError, "source module count"):
                materialize.materialize(self.worker, self.sources, self.destination)
        self.assertFalse(self.destination.exists())
        self.assertFalse(list(self.root.glob(".unbundle-*")))


if __name__ == "__main__":
    unittest.main()
