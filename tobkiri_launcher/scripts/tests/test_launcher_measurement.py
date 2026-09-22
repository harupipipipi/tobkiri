from __future__ import annotations

import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPT_DIR = Path(__file__).resolve().parents[1]


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


bundle = load_module("measure_launcher_bundle", SCRIPT_DIR / "measure_launcher_bundle.py")
logs = load_module("analyze_launcher_logs", SCRIPT_DIR / "analyze_launcher_logs.py")


class LauncherMeasurementTests(unittest.TestCase):
    def test_bundle_report_breaks_down_runtime_and_reports_baggage_without_deleting(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "ecosystem").mkdir()
            (root / "core_runtime" / "core_pack" / "core_control_panel" / "web").mkdir(parents=True)
            (root / "ecosystem" / "pack.bin").write_bytes(b"a" * 10)
            (root / "core_runtime" / "core_pack" / "core_control_panel" / "web" / "app.js").write_bytes(b"b" * 5)
            (root / "kernel.log").write_bytes(b"c" * 7)
            report = bundle.build_report(None, root, 10, label="test")
            self.assertEqual(report["metrics"]["resources_app"]["bytes"], 22)
            self.assertEqual(report["metrics"]["ecosystem"]["bytes"], 10)
            self.assertEqual(report["discardable_candidates"]["bytes"], 7)
            self.assertTrue((root / "kernel.log").exists())

    def test_bundle_deltas_and_growth_gate(self):
        report = {"metrics": {"frontend_web": {"bytes": 120}}}
        baseline = {"metrics": {"frontend_web": {"bytes": 100}}}
        bundle.add_deltas(report, baseline)
        self.assertEqual(report["delta_bytes"]["frontend_web"], 20)
        self.assertEqual(
            bundle.growth_failures(report, ["frontend_web"]),
            [{"metric": "frontend_web", "delta_bytes": 20}],
        )

    def test_log_analyzer_keeps_startup_race_integrity_and_host_execution_separate(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "kernel.log"
            path.write_text(
                "OSError(48) Address already in use existing_ready=true\n"
                "Flow load completed: flows_registered=2, flow_errors=2\n"
                "frontend_pack_hash_mismatch missing_provider\n"
                "host_execution warning\n",
                encoding="utf-8",
            )
            report = logs.analyze([path])
            categories = {finding["category"] for finding in report["findings"]}
            self.assertIn("startup_race", categories)
            self.assertIn("integrity", categories)
            self.assertIn("host_execution", categories)
            self.assertTrue(logs.has_error(report, "integrity"))
            self.assertTrue(logs.has_error(report, "startup_race"))
            self.assertEqual(report["summary"]["by_category"]["integrity"], 3)


if __name__ == "__main__":
    unittest.main()
