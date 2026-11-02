"""CI binder mode/identity checks; fixtures never execute a component."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools/python313-producer"))
sys.path.insert(0, str(ROOT / "scripts/wasm"))
import harness_native_ci_inputs as binder  # noqa: E402
import stage_harness_native as stage  # noqa: E402


class DiagnosticInputBindingTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.environment = self.root / "github-env"
        self.platform = patch.object(binder.sys, "platform", "darwin")
        self.platform.start()
        self.addCleanup(self.platform.stop)
        self.actions = patch.dict(os.environ, {"GITHUB_ACTIONS": "true"})
        self.actions.start()
        self.addCleanup(self.actions.stop)

    def fixture(self, diagnostic):
        files = stage.captured(schema_call_diagnostic=diagnostic)
        plan = stage.plan_for(files)
        component = self.root / "component"
        inputs, work = component / "inputs", component / "work"
        inputs.mkdir(parents=True)
        work.mkdir()
        raw_plan = (json.dumps(plan, sort_keys=True) + "\n").encode()
        (inputs / "plan.json").write_bytes(raw_plan)
        (inputs / "provenance.json").write_bytes(files["provenance.json"])
        artifact = work / "component.wasm"
        # Only a hash-bound binder fixture. Runtime/ABI validity is NOT proved.
        payload = b"binder fixture; never executed"
        artifact.write_bytes(payload)
        (work / "component.build.json").write_text(json.dumps({
            "format": "tobkiri.component-build/2", "world": "harness-native",
            "artifact_sha256": hashlib.sha256(payload).hexdigest(),
            "artifact_bytes": len(payload),
            "build_plan_sha256": hashlib.sha256(raw_plan).hexdigest(),
            "imports": [binder.IMPORT], "exports": [binder.RUNTIME, binder.TRANSPORT],
            "production_enabled": False,
        }))
        return artifact, inputs, plan

    def test_baseline_binding_rejects_derived_source_without_explicit_mode(self):
        artifact, _, _ = self.fixture(True)
        with self.assertRaisesRegex(ValueError, "current original source"):
            binder.bind(artifact, self.root / "bound", self.environment)
        self.assertFalse((self.root / "bound").exists())
        self.assertFalse(self.environment.exists())

    def test_diagnostic_binding_rejects_baseline_instead_of_format_fallback(self):
        artifact, _, _ = self.fixture(False)
        with self.assertRaisesRegex(ValueError, "diagnostic provenance"):
            binder.bind(artifact, self.root / "bound", self.environment,
                        schema_call_diagnostic=True)
        self.assertFalse(self.environment.exists())

    def test_diagnostic_binds_actual_derived_index_and_marks_source_difference(self):
        artifact, inputs, _ = self.fixture(True)
        report = binder.bind(artifact, self.root / "bound", self.environment,
                             schema_call_diagnostic=True)
        provenance = json.loads((inputs / "provenance.json").read_bytes())
        self.assertEqual(report["source_index_sha256"],
                         provenance["source_index"]["index_sha256"])
        self.assertFalse(report["original_source_files_unchanged"])
        self.assertTrue(report["actual_derived_source_index_verified"])
        self.assertFalse(report["component_init_verified"])
        self.assertFalse(report["whole_catalog_acceptance"])
        self.assertEqual(report["checkpoint_contract"]["tag_count"], 69)
        self.assertIn("CI_SCHEMA_TAG_COUNT: usize = 69",
                      (self.root / "bound/module-index.rs").read_text())
        self.assertIn("TOBKIRI_TEST_NATIVE_HARNESS_SCHEMA_DIAGNOSTIC=1\n",
                      self.environment.read_text())

    def test_self_consistent_rehashed_plan_cannot_substitute_validation_source(self):
        artifact, inputs, plan = self.fixture(True)
        row = next(row for row in plan["sources"]
                   if row["source"] == "python/tobkiri_protocol/validation.py")
        row["sha256"] = hashlib.sha256(b"foreign validation source").hexdigest()
        raw_plan = (json.dumps(plan, sort_keys=True) + "\n").encode()
        (inputs / "plan.json").write_bytes(raw_plan)
        build_path = artifact.with_name("component.build.json")
        build = json.loads(build_path.read_bytes())
        build["build_plan_sha256"] = hashlib.sha256(raw_plan).hexdigest()
        build_path.write_text(json.dumps(build))
        with self.assertRaisesRegex(ValueError, "current original source"):
            binder.bind(artifact, self.root / "bound", self.environment,
                        schema_call_diagnostic=True)
        self.assertFalse(self.environment.exists())
