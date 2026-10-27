"""Small framing/observation contract checks; no Rust build or Wasm execution."""

from pathlib import Path
import json
import tempfile
import unittest

import graph_report as graph


class GraphReportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.component = self.root / "component.wasm"
        self.component.write_bytes(b"\0asm\x0d\0\x01\0\x01\x08\0asm\x01\0\0\0")
        self.rows = [
            ["format", "tobkiri.core-graph-observation/1"],
            ["input", "18"],
            ["validation", "wasmparser-0.245.1-all-function-bodies"],
            ["scope", "source index graph; no runtime provider or storage proof"],
            ["module", "0", "0", "0", "10", "8"],
            ["module-end", "0", "18", "0", "0"],
            ["component-end", "0", "18"],
            ["complete", "1", "1"],
            ["final_provider_resolution_verified", "false"],
            ["errno_storage_identity_verified", "false"],
            ["production_enabled", "false"],
        ]

    def test_tiny_module_is_retained_without_claiming_runtime_proof(self):
        report = graph.summarize(self.component, self.rows, [])
        self.assertEqual(report["modules"][0]["bytes"], 8)
        self.assertTrue(report["all_core_module_occurrences_observed"])
        self.assertFalse(report["final_provider_resolution_verified"])
        self.assertFalse(report["errno_storage_identity_verified"])
        self.assertFalse(report["production_enabled"])

    def test_missing_or_displaced_tiny_module_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "omitted"):
            graph.summarize(
                self.component, [r for r in self.rows if r[0] != "module"], []
            )
        self.rows[4][4] = "11"
        with self.assertRaisesRegex(ValueError, "range differs"):
            graph.summarize(self.component, self.rows, [])

    def test_body_cannot_escape_its_actual_module(self):
        self.rows.insert(5, ["body", "0", "0", "17", "2"])
        with self.assertRaisesRegex(ValueError, "escapes"):
            graph.summarize(self.component, self.rows, [])

    def test_truncated_or_promoted_observations_are_rejected(self):
        raw = self.root / "rows.jsonl"
        raw.write_text("".join(json.dumps(r) + "\n" for r in self.rows))
        self.assertEqual(graph.read_rows(raw), self.rows)
        self.rows[-1][1] = "true"
        raw.write_text("".join(json.dumps(r) + "\n" for r in self.rows))
        with self.assertRaisesRegex(ValueError, "scoped"):
            graph.read_rows(raw)
        raw.write_text('["format"')
        with self.assertRaisesRegex(ValueError, "incomplete"):
            graph.read_rows(raw)

    def test_actual_analysis_refuses_local_execution(self):
        from unittest.mock import patch

        with patch.object(graph.sys, "platform", "darwin"):
            with self.assertRaisesRegex(ValueError, "CI only"):
                graph.run(self.root, self.root, self.root, self.root)


if __name__ == "__main__":
    unittest.main()
