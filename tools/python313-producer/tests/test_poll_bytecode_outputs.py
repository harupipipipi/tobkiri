"""Synthetic headers only; never import, unmarshal or execute a cache."""

import copy
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import poll_bytecode_outputs as caches
from poll_reference_driver import stage_inventory


def fixture(root: Path, stamp: int = 1000):
    stage = root / "stage"
    stage.mkdir(mode=0o700, parents=True)
    for source in caches.CACHES.values():
        p = stage / source
        p.write_bytes(b"VALUE = 1\n")
        p.chmod(0o600)
        os.utime(p, (stamp, stamp))
    before = stage_inventory(stage)
    (stage / "__pycache__").mkdir(mode=0o700)
    for cache, source in caches.CACHES.items():
        p = stage / cache
        data = (
            caches.MAGIC
            + (0).to_bytes(4, "little")
            + stamp.to_bytes(4, "little")
            + (stage / source).stat().st_size.to_bytes(4, "little")
            + b"synthetic body, never parsed as code"
        )
        p.write_bytes(data)
        p.chmod(0o600)
    return stage, before, stage_inventory(stage)


class BytecodeOutputTests(unittest.TestCase):
    def test_records_two_outputs_and_source_headers_without_claiming_body_proof(self):
        with tempfile.TemporaryDirectory() as tmp:
            stage, before, after = fixture(Path(tmp))
            result = caches.inspect_outputs(stage, before, after)
            self.assertEqual(set(result), set(caches.CACHES))
            self.assertTrue(all(r["source_header_checked"] for r in result.values()))
            self.assertTrue(all(r["kind"] == "timestamp" for r in result.values()))
            self.assertTrue(
                all(
                    r["bytecode_body_independently_executed_or_validated"] is False
                    for r in result.values()
                )
            )

    def test_same_sources_may_produce_different_valid_timestamp_outputs(self):
        with tempfile.TemporaryDirectory() as tmp:
            a, before_a, after_a = fixture(Path(tmp) / "a", 1000)
            b, before_b, after_b = fixture(Path(tmp) / "b", 2000)
            self.assertEqual(before_a, before_b)
            old = caches.inspect_outputs(a, before_a, after_a)
            new = caches.inspect_outputs(b, before_b, after_b)
            self.assertNotEqual(old, new)

    def test_missing_changed_input_or_unknown_output_cannot_be_ignored(self):
        for change in ("changed-source", "missing-source", "extra-cache"):
            with self.subTest(change=change), tempfile.TemporaryDirectory() as tmp:
                stage, before, after = fixture(Path(tmp))
                modified = copy.deepcopy(after)
                if change == "changed-source":
                    modified["app.py"]["sha256"] = "f" * 64
                elif change == "missing-source":
                    del modified["app.py"]
                else:
                    modified["__pycache__/foreign.cpython-313.pyc"] = modified[
                        next(iter(caches.CACHES))
                    ]
                with self.assertRaises(ValueError):
                    caches.inspect_outputs(stage, before, modified)

    def test_bad_magic_flags_time_size_and_truncation_are_rejected(self):
        for change in ("magic", "flags", "time", "size", "truncated"):
            with self.subTest(change=change), tempfile.TemporaryDirectory() as tmp:
                stage, before, _ = fixture(Path(tmp))
                p = stage / next(iter(caches.CACHES))
                raw = bytearray(p.read_bytes())
                if change == "magic":
                    raw[0] ^= 1
                elif change == "flags":
                    raw[4:8] = (4).to_bytes(4, "little")
                elif change == "time":
                    raw[8:12] = (999).to_bytes(4, "little")
                elif change == "size":
                    raw[12:16] = (999).to_bytes(4, "little")
                else:
                    raw = raw[:15]
                p.write_bytes(raw)
                with self.assertRaises(ValueError):
                    caches.inspect_outputs(stage, before, stage_inventory(stage))

    def test_hash_headers_require_the_pinned_host_before_final_acceptance(self):
        with tempfile.TemporaryDirectory() as tmp:
            stage, before, _ = fixture(Path(tmp))
            for name in caches.CACHES:
                path = stage / name
                raw = bytearray(path.read_bytes())
                raw[4:8] = (1).to_bytes(4, "little")
                path.write_bytes(raw)
            after = stage_inventory(stage)
            with patch.object(caches.sys, "version_info", (3, 10, 0)):
                observed = caches.inspect_outputs(stage, before, after)
                self.assertTrue(
                    all(
                        row["source_header_checked"] is False
                        for row in observed.values()
                    )
                )
                with self.assertRaises(ValueError):
                    caches.inspect_outputs(
                        stage, before, after, require_native_hash=True
                    )


if __name__ == "__main__":
    unittest.main()
