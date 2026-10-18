"""Pure contract/Flow declarations; no guest, Core, DATA or tool execution."""
from __future__ import annotations

import json
from pathlib import Path
import re
import sys
import unittest

PACKS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PACKS))
import build_deepthink as worker
import build_deepthink_input as adapter


def interfaces(text: str) -> dict[str, str]:
    """Retain each exact existing interface body rather than matching names."""
    result = {}
    for match in re.finditer(r"interface\s+([a-z0-9-]+)\s*\{", text):
        depth = 1
        end = match.end()
        while depth and end < len(text):
            depth += (text[end] == "{") - (text[end] == "}")
            end += 1
        if depth or match.group(1) in result:
            raise ValueError("unclosed or duplicate WIT interface")
        result[match.group(1)] = text[match.start():end]
    return result


class IndependentOperationContracts(unittest.TestCase):
    def test_old_members_and_versions_are_exact_and_only_six_shapes_are_added(self):
        baseline = interfaces((PACKS / "tests/fixtures/deepthink-original-interfaces.wit").read_text(encoding="utf-8"))
        current = interfaces((PACKS / "shapes/deepthink.wit").read_text(encoding="utf-8"))
        self.assertEqual({name: current[name] for name in baseline}, baseline)
        self.assertEqual(set(current) - set(baseline), set(worker.NEW_PORT_SHAPES) | set(worker.NEW_FLOW_SHAPES))
        self.assertEqual(len(re.findall(r"\w[\w-]*:\s*func", current["continuation-input"])), 6)
        self.assertEqual(len(re.findall(r"\w[\w-]*:\s*func", current["workflow"])), 5)

    def test_new_input_operations_only_start_declared_flows_and_no_worker_call_authority(self):
        self.assertEqual(set(adapter.PUBLIC_VIEWS) - {adapter.INPUT_VIEW}, {
            "local.deepthink.tool-diagnostics@1.0", "local.deepthink.goal-review-input@1.0"})
        permissions = adapter.permissions()
        self.assertEqual(set(permissions["ports"]), set(adapter.PUBLIC_VIEWS) | set(adapter.FLOW_VIEWS))
        for dependency in adapter.TYPE_VIEWS:
            self.assertNotIn(dependency, permissions["ports"])
            self.assertIn({"shape": dependency.rsplit("@", 1)[0], "version": "1.0", "optional": False}, adapter.consumed_ports())
        self.assertFalse(any(permissions["storage"].values()))
        self.assertEqual(permissions["secrets"], [])
        self.assertEqual(permissions["network"]["origins"], [])

    def test_diagnostic_and_review_flows_each_have_one_real_worker_step(self):
        for name, public, target, operation in [
            ("tool-diagnostics", "tool-diagnostics", "tool-receipt-reader", "page"),
            ("goal-review", "goal-review-input", "goal-review-worker", "review"),
        ]:
            flow = json.loads((PACKS / "local.deepthink/flows" / (name + ".flow.json")).read_text(encoding="utf-8"))
            self.assertEqual(flow["implements"], "local.deepthink." + name + "-flow@1.0")
            self.assertEqual(flow["trigger"], {"input": "local.deepthink." + public + "@1.0"})
            self.assertEqual(flow["loop_limits"], "profile-default")
            self.assertEqual(len(flow["steps"]), 1)
            step = flow["steps"][0]
            self.assertEqual(step["call"]["shape"], "local.deepthink." + target + "@1.0")
            self.assertEqual(step["call"]["op"], operation)
            self.assertNotIn("return", step)
            self.assertNotIn("save", json.dumps(step))
            if operation == "page":
                self.assertNotIn("stream", step)
            else:
                self.assertEqual(step["stream"], {"emit": "local.harness-chat.assistant-output@1.0", "collect": "turn"})


if __name__ == "__main__":
    unittest.main()
