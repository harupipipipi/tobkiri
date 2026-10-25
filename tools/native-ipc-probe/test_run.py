"""Controller regressions: missing coverage and private output never become PASS."""
from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import sys
import unittest

SPEC = importlib.util.spec_from_file_location("native_ipc_run", Path(__file__).with_name("run.py"))
probe = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(probe)


def valid_receipt() -> dict:
    """An input vector, not evidence of native app execution."""
    return {
        "format": "tobkiri.native-ipc-observation/1", "passed": True,
        "native_caller_entries": 2,
        "native_approval_ui_tested": False, "configuration_api_tested": False,
        "full_product_gui_tested": False,
        "report": {
            "version": 1, "root_start": True, "iframe_loaded": True, "root_stop": True,
            "parent_dom_blocked": True, "parent_internals_blocked": True,
            "storage_blocked": True, "sdk_exposed": False, "sdk_attempts": 0,
            "sdk_rejected": 0, "raw_surfaces": 1, "raw_sent": 11,
            "command_count": 11, "failure": "none",
        },
    }


class ControllerTests(unittest.TestCase):
    def evaluate(self, receipt=None, *, stderr=None, code=0, failure=None):
        value = receipt if receipt is not None else valid_receipt()
        native = probe.PREFIX + json.dumps(value).encode() + b"\n"
        if stderr is None:
            stderr = (probe.BAD_KEY + b"PRIVATE-SENTINEL but received \n") * 11
        return probe.evaluate(native, stderr, code, failure)

    def test_all_controls_and_native_rejections_required(self):
        self.assertTrue(self.evaluate()["passed"])
        for count in (0, 10, 12):
            with self.subTest(count=count):
                result = self.evaluate(stderr=(probe.BAD_KEY + b"secret\n") * count)
                self.assertFalse(result["passed"])
                self.assertEqual(result["failure"], "unconfirmed-raw-rejection")

    def test_unsettled_missing_or_extra_calls_never_pass(self):
        for key, value in (
            ("root_start", False), ("iframe_loaded", False), ("root_stop", False),
            ("parent_dom_blocked", False), ("parent_internals_blocked", False),
            ("storage_blocked", False), ("sdk_exposed", True),
            ("raw_surfaces", 0), ("raw_sent", 10), ("command_count", 10),
            ("failure", "timeout"),
        ):
            with self.subTest(key=key):
                receipt = valid_receipt()
                receipt["report"][key] = value
                self.assertFalse(self.evaluate(receipt)["passed"])
        for count in (1, 3):
            receipt = valid_receipt()
            receipt["native_caller_entries"] = count
            self.assertFalse(self.evaluate(receipt)["passed"])

    def test_sdk_and_two_raw_surfaces_need_complete_matching_coverage(self):
        receipt = valid_receipt()
        receipt["report"].update(sdk_exposed=True, sdk_attempts=11, sdk_rejected=10)
        self.assertFalse(self.evaluate(receipt)["passed"])
        receipt["report"].update(sdk_rejected=11, raw_surfaces=2, raw_sent=22)
        self.assertFalse(self.evaluate(receipt)["passed"])
        self.assertTrue(self.evaluate(
            receipt, stderr=(probe.BAD_KEY + b"secret\n") * 22)["passed"])

    def test_closed_schema_rejects_duplicate_fields_and_secret_text(self):
        for update in (
            {"approved": "PRIVATE-SENTINEL"},
            {"native_caller_entries": True},
            {"native_approval_ui_tested": True},
            {"report": {"failure": "PRIVATE-SENTINEL"}},
        ):
            receipt = valid_receipt()
            receipt.update(update)
            result = self.evaluate(receipt)
            self.assertFalse(result["passed"])
            self.assertNotIn("PRIVATE-SENTINEL", json.dumps(result))
        receipt = valid_receipt()
        receipt["report"]["failure"] = "PRIVATE-SENTINEL"
        self.assertNotIn("PRIVATE-SENTINEL", json.dumps(self.evaluate(receipt)))
        text = json.dumps(valid_receipt()).replace('"passed": true', '"passed": false,"passed": true')
        result = probe.evaluate(probe.PREFIX + text.encode(), b"", 0, None)
        self.assertEqual(result["failure"], "schema")

    def test_missing_duplicate_truncated_oversize_and_shutdown_are_failures(self):
        line = probe.PREFIX + json.dumps(valid_receipt()).encode() + b"\n"
        for output in (b"", line + line, line[:100], probe.PREFIX + b"x" * 4097):
            self.assertFalse(probe.evaluate(output, b"", 0, None)["passed"])
        for failure in ("timeout", "capture-limit", "spawn", "PRIVATE-SENTINEL"):
            result = probe.evaluate(line, b"", 0, failure)
            self.assertFalse(result["passed"])
            self.assertNotIn("PRIVATE-SENTINEL", json.dumps(result))
        self.assertEqual(self.evaluate(code=1)["failure"], "process-exit")
        for marker in ("Core shutdown failed", "Core shutdown budget reached or worker lost"):
            self.assertEqual(self.evaluate(stderr=marker.encode())["failure"], "shutdown")
        self.assertEqual(probe.evaluate(
            b"x" * (probe.MAX_CAPTURE + 1), b"", 0, None)["failure"], "capture-limit")

    def test_capture_drains_both_pipes_and_does_not_print_either(self):
        program = "import os; os.write(1,b'out'); os.write(2,b'PRIVATE-SENTINEL')"
        stdout, stderr, code, failure = probe.capture([sys.executable, "-c", program], dict(os.environ))
        self.assertEqual((stdout, stderr, code, failure), (b"out", b"PRIVATE-SENTINEL", 0, None))
        result = probe.evaluate(stdout, stderr, code, failure)
        self.assertNotIn("PRIVATE-SENTINEL", json.dumps(result))

    def test_capture_overflow_and_timeout_stop_only_the_owned_child(self):
        output = "import os; os.write(2,b'x'*65536)"
        result = probe.capture([sys.executable, "-c", output], dict(os.environ), limit=32)
        self.assertEqual(result[3], "capture-limit")
        self.assertLessEqual(len(result[1]), 32)
        result = probe.capture(
            [sys.executable, "-c", "import time; time.sleep(10)"],
            dict(os.environ), timeout=0.05,
        )
        self.assertEqual(result[3], "timeout")


if __name__ == "__main__":
    unittest.main()
