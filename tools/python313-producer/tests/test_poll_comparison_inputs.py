"""Synthetic supplier authentication and local-execution guard regressions."""

import copy
import hashlib
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import build_ui_poll_comparison as comparison
import poll_comparison_inputs as inputs
import poll_reference_driver as driver


def fixture():
    pins = {
        "format": "tobkiri.ui-poll-comparison-inputs/1",
        "repository": "harupipipipi/tobkiri",
        "workflow": ".github/workflows/issue-1520-ui-poll-producers.yml",
        "head": "1" * 40,
        "run": 1,
        "job": 2,
        "artifact": 3,
        "artifact_bytes": 4096,
        "artifact_sha256": "2" * 64,
        "files": {name: {"bytes": 1, "sha256": "3" * 64} for name in inputs.MEMBERS},
    }
    run = {
        "id": 1,
        "head_sha": pins["head"],
        "path": pins["workflow"],
        "repository": {"full_name": pins["repository"]},
        "status": "completed",
        "conclusion": "success",
    }
    job = {
        "id": 2,
        "run_id": 1,
        "name": "paired-producers",
        "status": "completed",
        "conclusion": "success",
        "steps": [
            {"name": name, "status": "completed", "conclusion": "success"}
            for name in sorted(inputs.REQUIRED_STEPS)
        ],
    }
    artifact = {
        "id": 3,
        "name": "ui-empty-poll-paired-producers-inactive",
        "expired": False,
        "size_in_bytes": 4096,
        "digest": "sha256:" + pins["artifact_sha256"],
        "workflow_run": {"id": 1, "head_sha": pins["head"]},
    }
    return pins, run, job, artifact


class ComparisonInputTests(unittest.TestCase):
    def test_empty_generated_package_markers_are_data_but_modules_are_not_empty(self):
        import poll_link_trace as trace

        with tempfile.TemporaryDirectory() as tmp:
            stage = Path(tmp) / "stage"
            stage.mkdir(mode=0o700)
            marker = stage / "__init__.py"
            marker.write_bytes(b"")
            marker.chmod(0o600)
            inventory = driver.stage_inventory(stage)
            self.assertEqual(
                inventory["__init__.py"],
                {
                    "bytes": 0,
                    "sha256": hashlib.sha256(b"").hexdigest(),
                    "mode": "0o600",
                },
            )
            with self.assertRaises(ValueError):
                trace.fingerprint(marker, trace.FILE_LIMIT)
            nested = stage / "nested"
            nested.mkdir(mode=0o700)
            output = nested / "component.wasm"
            output.write_bytes(b"not the root output")
            output.chmod(0o600)
            self.assertIn(
                "nested/component.wasm",
                driver.stage_inventory(stage, exclude_component=True),
            )
            support = stage / "poll_loop.py"
            support.write_bytes(b"synthetic embedded support source")
            support.chmod(0o644)
            self.assertEqual(
                driver.stage_inventory(stage)["poll_loop.py"]["mode"], "0o644"
            )
            with self.assertRaises(ValueError):
                trace.fingerprint(support, trace.FILE_LIMIT)
            support.chmod(0o666)
            with self.assertRaises(ValueError):
                driver.stage_inventory(stage)

    def test_each_producer_command_must_preserve_captured_source_and_helper(self):
        for changed in ("app.py", "probe.wit", "_tobkiri_poll_oracle.abi3.so"):
            with self.subTest(changed=changed), tempfile.TemporaryDirectory() as tmp:
                stage = Path(tmp)
                control = {"probe_inputs": {}}
                for name in ("app.py", "probe.wit", "_tobkiri_poll_oracle.abi3.so"):
                    raw = ("synthetic captured bytes for " + name).encode()
                    path = stage / name
                    path.write_bytes(raw)
                    path.chmod(0o600)
                    identity = {
                        "bytes": len(raw),
                        "sha256": hashlib.sha256(raw).hexdigest(),
                    }
                    if name.endswith(".so"):
                        control["helper"] = identity
                    else:
                        control["probe_inputs"][name] = identity
                driver.require_captured_stage(stage, control)
                (stage / changed).write_bytes(b"producer changed captured input")
                with self.assertRaises(ValueError):
                    driver.require_captured_stage(stage, control)

    def test_actual_supplier_metadata_needs_all_fixed_bindings_and_stages(self):
        pins, run, job, artifact = fixture()
        inputs.authenticate(pins, run, job, artifact)
        changes = (
            (1, "head_sha", "4" * 40),
            (1, "status", "in_progress"),
            (1, "repository", {"full_name": "other/fork"}),
            (2, "run_id", 99),
            (2, "conclusion", "failure"),
            (2, "steps", job["steps"][:-1]),
            (3, "expired", True),
            (3, "digest", "sha256:" + "4" * 64),
            (3, "workflow_run", {"id": 99, "head_sha": pins["head"]}),
        )
        for item, key, value in changes:
            with self.subTest(item=item, key=key):
                args = copy.deepcopy([pins, run, job, artifact])
                args[item][key] = value
                with self.assertRaises(ValueError):
                    inputs.authenticate(*args)

    def test_pin_set_cannot_omit_or_substitute_any_decoded_file(self):
        for change in ("missing", "extra", "malformed-hash", "boolean-size"):
            with self.subTest(change=change):
                pins, *_ = fixture()
                if change == "missing":
                    del pins["files"][inputs.HELPER]
                elif change == "extra":
                    pins["files"]["../outside"] = {"bytes": 1, "sha256": "3" * 64}
                elif change == "malformed-hash":
                    pins["files"][inputs.HELPER]["sha256"] = "3" * 63
                else:
                    pins["files"][inputs.HELPER]["bytes"] = True
                with self.assertRaises(ValueError):
                    inputs.validate_pins(pins)

    def test_successful_step_names_cannot_hide_pending_or_skipped_execution(self):
        for status, conclusion in (("in_progress", None), ("completed", "skipped")):
            with self.subTest(status=status, conclusion=conclusion):
                pins, run, job, artifact = fixture()
                job["steps"][0].update(status=status, conclusion=conclusion)
                with self.assertRaises(ValueError):
                    inputs.authenticate(pins, run, job, artifact)

    def test_macos_invocation_stops_before_reading_or_executing_inputs(self):
        with (
            patch.object(comparison.sys, "platform", "darwin"),
            patch.object(comparison, "read_json") as read,
        ):
            with self.assertRaisesRegex(ValueError, "Linux CI"):
                comparison.run(*[Path("/must-not-touch")] * 4)
            read.assert_not_called()


if __name__ == "__main__":
    unittest.main()
