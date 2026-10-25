"""Narrow source checks for the diagnostic-only schema failure path."""

import hashlib
import base64
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE / "schema_probe"))

import build_schema  # noqa: E402
import exception_diagnostics as diagnostics  # noqa: E402


class SchemaExceptionDiagnosticsTests(unittest.TestCase):
    def test_app_derivation_is_exact_and_leaves_frozen_baseline_bytes_intact(self):
        app_path = HERE / "schema_probe/app.py"
        baseline = app_path.read_bytes()
        helper = (HERE / "schema_probe/exception_diagnostics.py").read_bytes()
        derived, provenance = diagnostics.derive_diagnostic_app(baseline, helper)
        expected = baseline.replace(
            diagnostics.APP_IMPORT,
            diagnostics.APP_IMPORT
            + b"from exception_diagnostics import safe_exception_diagnostics\n",
            1,
        )
        expected = expected.replace(
            diagnostics.EXCEPT_MARKER,
            diagnostics.EXCEPT_MARKER
            + b"            diagnostics = safe_exception_diagnostics(error)\n",
            1,
        )
        expected = expected.replace(
            diagnostics.FRAMES_MARKER,
            diagnostics.FRAMES_MARKER
            + b'                        "diagnostics": diagnostics,\n',
            1,
        )
        self.assertEqual(derived, expected)
        compile(derived.decode("utf-8"), "diagnostic-app.py", "exec")
        self.assertEqual(app_path.read_bytes(), baseline)
        self.assertEqual(provenance["baseline_app"]["sha256"], hashlib.sha256(baseline).hexdigest())
        self.assertEqual(provenance["derived_app"]["sha256"], hashlib.sha256(derived).hexdigest())
        self.assertTrue(provenance["diagnostic_only"])
        self.assertIn("safe_exception_diagnostics(error)", provenance["diff"])

    def test_frame_facts_avoid_repr_str_and_descriptor_evaluation(self):
        calls = []

        class HostileError(Exception):
            @property
            def schema_path(self):
                calls.append("schema_path")
                raise AssertionError("property must not run")

            @property
            def __dict__(self):
                calls.append("dict property")
                raise AssertionError("custom __dict__ property must not run")

            def __repr__(self):
                calls.append("repr")
                raise AssertionError("repr must not run")

            def __str__(self):
                calls.append("str")
                raise AssertionError("str must not run")

            def __getattribute__(self, name):
                if name in {"schema_path", "__dict__"}:
                    calls.append(name)
                    raise AssertionError("custom attribute access must not run")
                return super().__getattribute__(name)

        inner = HostileError()
        raw_fields = BaseException.__dict__["__dict__"].__get__(inner, type(inner))
        raw_fields["safe_field"] = "kept"
        raw_fields["huge_integer"] = 1 << 20_000

        def fail(name, error):
            raise RuntimeError("outer failure")

        try:
            fail("schema_path", inner)
        except RuntimeError as outer:
            captured = diagnostics.capture_exception_diagnostics(outer)

        frame = next(item for item in captured["frames"] if item["function"] == "fail")
        locals_report = frame["locals"]
        self.assertEqual(
            locals_report["name"],
            {"present": True, "type": "builtins.str", "value": "schema_path"},
        )
        self.assertFalse(locals_report["value"]["present"])
        error_report = locals_report["error"]
        self.assertTrue(error_report["present"])
        self.assertTrue(error_report["type"].endswith(".HostileError"))
        self.assertEqual(
            error_report["raw_instance_fields"]["safe_field"],
            {"type": "builtins.str", "value": "kept"},
        )
        huge_integer = error_report["raw_instance_fields"]["huge_integer"]
        self.assertEqual(
            huge_integer.get("value_omitted"),
            "integer exceeds the 64-bit diagnostic bound",
        )
        self.assertNotIn("value", huge_integer)
        self.assertIsInstance(json.dumps(captured), str)
        self.assertEqual(calls, [])

    def test_safe_capture_failure_does_not_replace_the_original_exception(self):
        original = RuntimeError("original remains handled by app")
        with patch.object(
            diagnostics,
            "capture_exception_diagnostics",
            side_effect=KeyboardInterrupt,
        ):
            result = diagnostics.safe_exception_diagnostics(original)
        self.assertEqual(
            result,
            {
                "format": "tobkiri.schema-exception-diagnostics/1",
                "capture_failed": True,
            },
        )

    def test_isolated_host_loader_uses_exact_helper_path_without_sys_path_changes(self):
        helper_source = (HERE / "schema_probe/exception_diagnostics.py").read_bytes()
        driver_path = HERE / "schema_probe/driver.py"
        script = (
            "import base64, importlib.util, sys\n"
            "from pathlib import Path\n"
            "spec = importlib.util.spec_from_file_location('_schema_driver', sys.argv[1])\n"
            "driver = importlib.util.module_from_spec(spec)\n"
            "spec.loader.exec_module(driver)\n"
            "before = sys.path[:]\n"
            "helper_path = Path(sys.argv[2])\n"
            "source = base64.b64decode(sys.argv[3])\n"
            "helper = driver.load_exception_diagnostics(helper_path, source)\n"
            "assert sys.path == before\n"
            "assert str(helper_path) == helper.__file__\n"
            "assert str(helper_path.parent) not in sys.path\n"
            "assert helper.APP_IMPORT == b'from exercise import exercise\\n'\n"
        )
        with tempfile.TemporaryDirectory() as directory:
            helper_path = Path(directory) / "exception_diagnostics.py"
            helper_path.write_bytes(helper_source)
            subprocess.run(
                [
                    sys.executable,
                    "-I",
                    "-S",
                    "-B",
                    "-c",
                    script,
                    str(driver_path),
                    str(helper_path),
                    base64.b64encode(helper_source).decode("ascii"),
                ],
                check=True,
                capture_output=True,
                text=True,
            )

    def test_controller_gates_flag_on_all_inactive_candidate_inputs(self):
        paths = tuple(Path(f"/private/{index}") for index in range(4))
        with self.assertRaisesRegex(ValueError, "complete inactive candidate"):
            build_schema._validate_schema_exception_diagnostics(
                True,
                allow_ui_wall_clock=True,
                allow_ui_monotonic_clock=True,
                allow_inactive_ui_libc=False,
                allow_ci_link_trace=True,
                predecessor_paths=paths,
            )
        with self.assertRaisesRegex(ValueError, "complete inactive candidate"):
            build_schema._validate_schema_exception_diagnostics(
                True,
                allow_ui_wall_clock=1,
                allow_ui_monotonic_clock=True,
                allow_inactive_ui_libc=True,
                allow_ci_link_trace=True,
                predecessor_paths=paths,
            )
        build_schema._validate_schema_exception_diagnostics(
            True,
            allow_ui_wall_clock=True,
            allow_ui_monotonic_clock=True,
            allow_inactive_ui_libc=True,
            allow_ci_link_trace=True,
            predecessor_paths=paths,
        )


if __name__ == "__main__":
    unittest.main()
