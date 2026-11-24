"""Offline custody checks for the complete immutable graph input set."""

from copy import deepcopy
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import poll_graph_inputs as graph  # noqa: E402


class GraphInputTests(unittest.TestCase):
    def setUp(self):
        self.pins = graph.read_json(graph.PINS)

    def test_complete_committed_members_include_tiny_libraries(self):
        graph.validate(self.pins)
        self.assertEqual(len(self.pins["files"]), 31)
        self.assertEqual(
            self.pins["files"]["original/work/link-trace/0009.wasm"]["bytes"], 7043
        )
        self.assertEqual(
            self.pins["files"]["candidate/work/link-trace/0005.wasm"]["bytes"], 407
        )

    def test_partial_pin_set_is_rejected(self):
        pins = deepcopy(self.pins)
        del pins["files"]["original/work/link-trace/0005.wasm"]
        with self.assertRaisesRegex(ValueError, "complete"):
            graph.validate(pins)

    def test_noncanonical_paths_and_invalid_sizes_are_rejected(self):
        pins = deepcopy(self.pins)
        pins["files"]["../component.wasm"] = pins["files"].pop("comparison.json")
        with self.assertRaises(ValueError):
            graph.validate(pins)
        pins = deepcopy(self.pins)
        pins["files"]["original/work/stage/component.wasm"]["bytes"] = True
        with self.assertRaisesRegex(ValueError, "member"):
            graph.validate(pins)


if __name__ == "__main__":
    unittest.main()
