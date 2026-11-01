"""Bounded fixture transport tests, never guest or native runtime acceptance."""
import hashlib
import json
import lzma
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts/wasm"))
import stage_preserved_ordered_python as staging


def sha(data):
    return hashlib.sha256(data).hexdigest()


class DecompressionTests(unittest.TestCase):
    def decode(self, data, expected=b"bounded structural bytes", **kwargs):
        return staging.decompress_verified(
            data, compressed_sha=kwargs.get("compressed_sha", sha(data)),
            artifact_sha=kwargs.get("artifact_sha", sha(expected)),
            artifact_bytes=kwargs.get("artifact_bytes", len(expected)))

    def test_exact_single_stream(self):
        original = b"bounded structural bytes"
        self.assertEqual(self.decode(lzma.compress(original)), original)

    def test_wrong_compressed_or_output_hash(self):
        data = lzma.compress(b"bounded structural bytes")
        for key in ("compressed_sha", "artifact_sha"):
            with self.subTest(key=key), self.assertRaises(ValueError):
                self.decode(data, **{key: "0" * 64})

    def test_output_expansion_is_capped(self):
        with self.assertRaises(ValueError):
            self.decode(lzma.compress(b"x" * 10000), artifact_bytes=10)

    def test_truncated_trailing_and_concatenated_streams_rejected(self):
        data = lzma.compress(b"bounded structural bytes")
        for changed in (data[:-1], data + b"trailing", data + data):
            with self.subTest(length=len(changed)), self.assertRaises(ValueError):
                self.decode(changed)

    def test_invalid_output_bound_rejected(self):
        for bound in (0, -1, 128 * 1024 * 1024 + 1):
            with self.subTest(bound=bound), self.assertRaises(ValueError):
                self.decode(b"", artifact_bytes=bound)


class ActualFixtureTests(unittest.TestCase):
    def test_real_fixture_materializes_without_execution(self):
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / "candidate"
            result = staging.stage_preserved(output)
            self.assertFalse(result["core_runtime_acceptance"])
            self.assertFalse(result["production_enabled"])
            self.assertEqual(result["case_count"], 33)
            self.assertEqual(result["artifact_sha256"], staging.ARTIFACT_SHA)
            self.assertEqual(result["receipt_sha256"], staging.RECEIPT_SHA)
            manifest = (output / "pack/pack.json").read_bytes()
            self.assertEqual(sha(manifest), result["pack_manifest_sha256"])
            self.assertEqual(json.loads(manifest)["runtime"]["kind"], "wasm")
            expected = {"component.wasm", "component.build.json", "config.json",
                        "wit/pack.wit", "wit/deps/core.wit", "wit/bridge.shape.json"}
            self.assertEqual({item["path"] for item in json.loads(manifest)["files"]},
                             expected)
            self.assertEqual({str(path.relative_to(output / "pack"))
                              for path in (output / "pack").rglob("*")
                              if path.is_file()}, expected | {"pack.json"})

    def test_existing_destination_is_not_modified(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp)
            sentinel = path / "retained"
            sentinel.write_bytes(b"keep")
            with self.assertRaises(ValueError):
                staging.stage_preserved(path)
            self.assertEqual(sentinel.read_bytes(), b"keep")

    def test_receipt_tampering_fails_before_destination_creation(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            fixture = root / "fixture"
            fixture.mkdir()
            (fixture / "component.wasm.xz").write_bytes(
                (staging.FIXTURE / "component.wasm.xz").read_bytes())
            (fixture / "component.build.json").write_bytes(b"{}")
            with patch.object(staging, "FIXTURE", fixture):
                with self.assertRaises(ValueError):
                    staging.stage_preserved(root / "output")
            self.assertFalse((root / "output").exists())


if __name__ == "__main__":
    unittest.main()
