"""Small stdlib-only tests for bounded native observation report storage."""

from __future__ import annotations

import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


_SOURCE = Path(__file__).with_name("native_observation_report.py")
_SPEC = importlib.util.spec_from_file_location(
    "native_observation_report_test", _SOURCE
)
if _SPEC is None or _SPEC.loader is None:
    raise RuntimeError("could not load native observation report module")
report = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(report)


class NativeObservationReportTests(unittest.TestCase):
    def setUp(self):
        self._directory = tempfile.TemporaryDirectory()
        self.root = Path(self._directory.name)

    def tearDown(self):
        self._directory.cleanup()

    def _write(self, directory: Path, name: str = "report.json") -> Path:
        target = directory / name
        report.write_report(
            target,
            {
                "setup": {"text": "日本語 " + "x" * 70, "order": [1, 2]},
                "facade": {"callable": True, "signature": None},
            },
            source={"revision": "abc123", "variant": "candidate"},
            call_boundaries=[{"name": "observe", "bytes": 111}],
        )
        return target

    def _small_limits(self):
        return (
            patch.object(report, "CHUNK_BYTES", 16),
            patch.object(report, "TOTAL_BYTES", 8192),
            patch.object(report, "MANIFEST_BYTES", 4096),
        )

    def test_round_trip_preserves_order_and_complete_observation_bytes(self):
        with self._small_limits()[0], self._small_limits()[1], self._small_limits()[2]:
            path = self._write(self.root)
            manifest, observations = report.read_report(path)

        self.assertEqual(list(observations), ["setup", "facade"])
        self.assertEqual(
            observations["setup"],
            json.dumps(
                {"text": "日本語 " + "x" * 70, "order": [1, 2]},
                ensure_ascii=True,
                separators=(",", ":"),
            ).encode("utf-8"),
        )
        self.assertEqual(
            manifest["source"], {"revision": "abc123", "variant": "candidate"}
        )
        self.assertEqual(
            manifest["call_boundaries"], [{"name": "observe", "bytes": 111}]
        )
        self.assertEqual(
            manifest["scope"]["product_boundary_measurement"]["bytes"], None
        )
        self.assertEqual(
            manifest["scope"]["product_limit"]["status"], "unchanged-not-exercised"
        )
        self.assertGreater(manifest["observations"][0]["chunk_count"], 1)

    def test_empty_observation_mapping_is_a_complete_report(self):
        with self._small_limits()[0], self._small_limits()[1], self._small_limits()[2]:
            path = self.root / "empty.json"
            manifest = report.write_report(path, {}, source={}, call_boundaries=[])
            loaded, observations = report.read_report(path)
        self.assertEqual(manifest, loaded)
        self.assertEqual(observations, {})
        self.assertEqual(loaded["aggregate_json_bytes"], 2)

    def test_tampered_or_missing_chunk_is_rejected(self):
        with self._small_limits()[0], self._small_limits()[1], self._small_limits()[2]:
            tampered_dir = self.root / "tampered"
            tampered_dir.mkdir()
            tampered = self._write(tampered_dir)
            entry = json.loads(tampered.read_text("utf-8"))["observations"][0]
            chunk = tampered.parent / entry["chunks"][0]["path"]
            chunk.write_bytes(b"!" + chunk.read_bytes()[1:])
            with self.assertRaises(report.InvalidReport):
                report.read_report(tampered)

            missing_dir = self.root / "missing"
            missing_dir.mkdir()
            missing = self._write(missing_dir)
            missing_entry = json.loads(missing.read_text("utf-8"))["observations"][0]
            (missing.parent / missing_entry["chunks"][0]["path"]).unlink()
            with self.assertRaises(report.InvalidReport):
                report.read_report(missing)

    def test_reordered_observations_and_extra_sibling_chunk_are_rejected(self):
        with self._small_limits()[0], self._small_limits()[1], self._small_limits()[2]:
            reordered_dir = self.root / "reordered"
            reordered_dir.mkdir()
            reordered = self._write(reordered_dir)
            manifest = json.loads(reordered.read_text("utf-8"))
            manifest["observations"].reverse()
            reordered.write_text(
                json.dumps(manifest, ensure_ascii=True, separators=(",", ":")),
                encoding="utf-8",
            )
            with self.assertRaises(report.InvalidReport):
                report.read_report(reordered)

            extra_dir = self.root / "extra"
            extra_dir.mkdir()
            extra = self._write(extra_dir)
            (extra_dir / "report--999--9999.json.part").write_bytes(b"extra")
            with self.assertRaises(report.InvalidReport):
                report.read_report(extra)

    def test_exact_total_cap_is_allowed_and_one_byte_less_fails(self):
        chunk_limit = patch.object(report, "CHUNK_BYTES", 16)
        manifest_limit = patch.object(report, "MANIFEST_BYTES", 4096)
        total_limit = patch.object(report, "TOTAL_BYTES", 8192)
        with chunk_limit, manifest_limit, total_limit:
            baseline_dir = self.root / "baseline"
            baseline_dir.mkdir()
            baseline = self._write(baseline_dir)
            total = sum(item.stat().st_size for item in baseline_dir.iterdir())

            exact_dir = self.root / "exact"
            exact_dir.mkdir()
            with patch.object(report, "TOTAL_BYTES", total):
                exact = self._write(exact_dir)
                report.read_report(exact)

            too_small_dir = self.root / "too-small"
            too_small_dir.mkdir()
            with patch.object(report, "TOTAL_BYTES", total - 1):
                with self.assertRaises(report.ReportingCapExceeded):
                    self._write(too_small_dir)
                with self.assertRaises(report.ReportingCapExceeded):
                    report.read_report(baseline)
            self.assertEqual(list(too_small_dir.iterdir()), [])

    @unittest.skipUnless(hasattr(os, "link"), "hard links are unavailable")
    def test_hard_linked_manifest_is_rejected(self):
        with self._small_limits()[0], self._small_limits()[1], self._small_limits()[2]:
            path = self._write(self.root)
            os.link(path, self.root / "manifest-alias.json")
            with self.assertRaises(report.InvalidReport):
                report.read_report(path)


if __name__ == "__main__":
    unittest.main()
