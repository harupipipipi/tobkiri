"""CI binder mode/identity checks; fixtures never execute a component."""
from __future__ import annotations

import ast
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
sys.path.insert(0, str(ROOT / "tools/python313-producer/tests"))
import app_schema_extension_inputs as app_extension  # noqa: E402
import component_inputs  # noqa: E402
import harness_native_ci_inputs as binder  # noqa: E402
import harness_native_dependencies as dependencies  # noqa: E402
from harness_native import schema_diagnostic_stage as diagnostic_stage  # noqa: E402
from fetch_sdk import inventory  # noqa: E402
import test_harness_native_dependencies as dependency_fixtures  # noqa: E402
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
        self.actions = patch.dict(os.environ, {
            "GITHUB_ACTIONS": "true", "GITHUB_SHA": "fixture source",
        })
        self.actions.start()
        self.addCleanup(self.actions.stop)

    def fixture(
        self, diagnostic: bool, *, app_backend: bool = False, tamper: str | None = None,
    ) -> tuple[Path, Path, dict]:
        variant, name = dependencies.variant(diagnostic=diagnostic, app_backend=app_backend)
        component = self.root / name
        inputs, work = component / "inputs", component / "work"
        work.mkdir(parents=True)
        if app_backend:
            files, plan, metadata = self.app_fixture(inputs, diagnostic, tamper)
        else:
            files, plan, metadata = self.schema_fixture(inputs, diagnostic)
        raw_plan = (json.dumps(plan, sort_keys=True) + "\n").encode()
        (inputs / "plan.json").write_bytes(raw_plan)
        files["plan.json"] = raw_plan
        (inputs / "provenance.json").write_bytes(files["provenance.json"])
        artifact = work / "component.wasm"
        # Only a hash-bound binder fixture. Runtime/ABI validity is NOT proved.
        payload = b"binder fixture; never executed"
        artifact.write_bytes(payload)
        manifest = [{**row, "bytes": len(files[row["source"]])} for row in plan["sources"]]
        build_path = work / "component.build.json"
        build_path.write_text(json.dumps({
            "format": "tobkiri.component-build/2", "world": "harness-native",
            "artifact_sha256": hashlib.sha256(payload).hexdigest(),
            "artifact_bytes": len(payload),
            "build_plan_sha256": hashlib.sha256(raw_plan).hexdigest(),
            "imports": [binder.IMPORT], "exports": [binder.RUNTIME, binder.TRANSPORT],
            "production_enabled": False,
            "source_manifest": manifest,
            "source_manifest_sha256": hashlib.sha256(json.dumps(
                manifest, sort_keys=True, separators=(",", ":"),
            ).encode()).hexdigest(),
            "inner": {"imports": [binder.IMPORT],
                      "exports": ["exports", binder.RUNTIME, binder.TRANSPORT]},
            "harness_native_dependencies": metadata,
            "compiler_executable_sha256": "fixture compiler",
            "producer_ci_receipt_sha256": "fixture receipt",
        }))
        (self.root / "components-build.json").write_text(json.dumps({
            "format": f"tobkiri.python313-native-harness-{variant}/1",
            "outcome": "success", "inputs_unchanged": True,
            "diagnostic_only": diagnostic, "production_enabled": False,
            "app_schema_backend": app_backend, "provider_substitution": app_backend,
            "whole_catalog_acceptance": False,
            "producer": {"sha256": "fixture compiler"},
            "producer_receipt": {"sha256": "fixture receipt"},
            "components": {name: {
                "artifact": binder.fingerprint(artifact),
                "build_receipt": binder.fingerprint(build_path),
                "source": {
                    "plan_sha256": hashlib.sha256(raw_plan).hexdigest(),
                    "source_files": plan["sources"],
                    "files": {
                        path: {
                            "bytes": len(raw),
                            "sha256": hashlib.sha256(raw).hexdigest(),
                            "mode": "0o600" if path == "provenance.json" else "0o644",
                        }
                        for path, raw in files.items()
                    },
                },
            }},
        }))
        return artifact, inputs, plan

    def schema_fixture(self, inputs: Path, diagnostic: bool) -> tuple[dict, dict, dict]:
        files = stage.captured(schema_call_diagnostic=diagnostic)
        plan = stage.plan_for(files)
        original_plan = (json.dumps(plan, sort_keys=True, indent=2) + "\n").encode()
        metadata = {
            "format": binder.DIAGNOSTIC_FORMAT if diagnostic else binder.NORMAL_DEPENDENCIES,
            "original_plan_sha256": hashlib.sha256(original_plan).hexdigest(),
            "original_source_files_unchanged": not diagnostic,
        }
        if diagnostic:
            metadata["diagnostic_only"] = True
        schema_paths = (*binder.PROFILE_SCHEMA_PATHS.values(),
                        *binder.PROFILE_REFERENCE_PATHS.values(),
                        *binder.RECOVERY_SCHEMA_PATHS.values())
        captured_schemas = dependencies.compress_sources({
            name: files["python/" + name] for name in schema_paths
        })
        generated_sources = {
            binder.METADATA: json.dumps(metadata).encode(),
            binder.CAPTURED_DATA: f"FILES = {captured_schemas!r}\n".encode(),
        }
        (inputs / "python").mkdir(parents=True)
        plan["entrypoint"] = binder.ENTRYPOINT
        for destination, raw in generated_sources.items():
            source = "python/" + destination
            files[source] = raw
            (inputs / source).write_bytes(raw)
            plan["sources"].append({
                "source": source,
                "destination": destination,
                "sha256": hashlib.sha256(raw).hexdigest(),
            })
        return files, plan, metadata

    def app_fixture(
        self, inputs: Path, diagnostic: bool, tamper: str | None,
    ) -> tuple[dict, dict, dict]:
        extension = self.root / "extension" / app_extension.ARTIFACT
        extension.parent.mkdir(parents=True)
        extension.write_bytes(b"\x00asm\x01\x00\x00\x00")
        extension_root = self.root / "extension"
        extension_report = {
            "format": "tobkiri.app-schema-kernel-extension/1",
            "outcome": "success", "guest_executed": False,
            "production_enabled": False, "source_head": os.environ["GITHUB_SHA"],
            "features": ["app-schema-backend"],
            "compiled_features": ["default", "app-schema-backend"],
            "source_files": inventory(app_extension.SOURCE),
            "artifact_path": app_extension.ARTIFACT,
            "artifact": binder.fingerprint(extension),
        }
        (extension_root / "extension-build.json").write_text(json.dumps(extension_report))
        original = component_inputs.stage_harness_native(
            ROOT, inputs, schema_call_diagnostic=diagnostic,
        )
        schema, stdlib, rpds, verified = dependency_fixtures.fixture(self.root)
        kernel = app_extension.verify_extension(extension_root, ["app-schema-backend"])
        selected = dependencies.stage(
            inputs, original, verified, schema, stdlib, rpds, app_extension=kernel,
        )
        plan = json.loads((inputs / "plan.json").read_bytes())
        files = {path: (inputs / path).read_bytes() for path in selected["files"]}
        metadata = json.loads(files["python/" + binder.METADATA])
        if tamper:
            if tamper == "extension":
                member = "python/" + app_extension.EXTENSION
                files[member] += b"foreign extension"
            else:
                member = "python/" + binder.CAPTURED_DATA
                table = ast.parse(files[member]).body[0].value
                if tamper == "missing-recovery-schema":
                    name = binder.RECOVERY_SCHEMA_PATHS["function_principal_v1"]
                    replacement = b""
                else:
                    self.assertEqual(tamper, "capture")
                    name = (diagnostic_stage.VALIDATION.removeprefix("python/")
                            if diagnostic else "tobkiri_protocol/bundle_catalog.py")
                    (new_row,) = dependencies.compress_sources(
                        {name: b"# foreign source"}
                    )
                    replacement = repr(new_row).encode() + b",\n"
                row, = (node for node in table.elts if node.elts[0].value == name)
                old_row = repr(ast.literal_eval(row)).encode() + b",\n"
                self.assertEqual(files[member].count(old_row), 1)
                files[member] = files[member].replace(old_row, replacement)
            (inputs / member).write_bytes(files[member])
            row, = (entry for entry in plan["sources"] if entry["source"] == member)
            row["sha256"] = hashlib.sha256(files[member]).hexdigest()
        return files, plan, metadata

    def test_baseline_binding_rejects_derived_source_without_explicit_mode(self):
        artifact, _, _ = self.fixture(True)
        with self.assertRaisesRegex(ValueError, "selected source"):
            binder.bind(artifact, self.root / "bound", self.environment)
        self.assertFalse((self.root / "bound").exists())
        self.assertFalse(self.environment.exists())

    def test_diagnostic_binding_rejects_baseline_instead_of_format_fallback(self):
        artifact, _, _ = self.fixture(False)
        with self.assertRaisesRegex(ValueError, "selected source"):
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
        self.assertIn("CI_SCHEMA_APP_BACKEND: bool = false",
                      (self.root / "bound/module-index.rs").read_text())
        self.assertIn("TOBKIRI_TEST_NATIVE_HARNESS_SCHEMA_DIAGNOSTIC=1\n",
                      self.environment.read_text())
        self.assertFalse((self.root / "bound/launcher-component.json").exists())
        self.assertNotIn("launcher_component_receipt", report)

    def test_normal_binding_emits_v2_launcher_receipt(self) -> None:
        artifact, _, _ = self.fixture(False)
        output = self.root / "bound"
        report = binder.bind(artifact, output, self.environment)
        receipt_path = output / "launcher-component.json"
        receipt = json.loads(receipt_path.read_bytes())
        self.assertEqual(report["launcher_component_receipt"], binder.fingerprint(receipt_path))
        self.assertEqual(receipt["format"], "io.tobkiri.launcher-setup-component.v2")
        self.assertEqual(receipt["component"], binder.fingerprint(artifact))
        self.assertTrue(receipt["modules"])
        self.assertLessEqual(receipt_path.stat().st_size, 64 * 1024)
        self.assert_complete_profile_inventory(receipt["profile_schemas"])

    def assert_complete_profile_inventory(self, schemas: dict) -> None:
        paths = {
            **binder.PROFILE_SCHEMA_PATHS,
            **binder.PROFILE_REFERENCE_PATHS,
            **binder.RECOVERY_SCHEMA_PATHS,
        }
        self.assertEqual(set(schemas), {"v4", "v5", "references", "recovery"})
        self.assertEqual(set(schemas["references"]), {"common", "content_projection"})
        self.assertEqual(set(schemas["recovery"]), {
            "profile_lock_v4", "profile_lock_v5", "resolved_plan_v1", "resolved_plan_v2",
            "activation_record_v1", "activation_record_v2", "function_principal_v1",
        })
        for key, row in {"v4": schemas["v4"], "v5": schemas["v5"],
                         **schemas["references"], **schemas["recovery"]}.items():
            expected = (ROOT / "tobkiri_runtime" / paths[key]).read_bytes()
            self.assertEqual(row, {"sha256": hashlib.sha256(expected).hexdigest(),
                                   "content": expected.decode("utf-8")})

    def test_app_binding_emits_v3_launcher_receipt_for_owned_snapshot(self) -> None:
        artifact, inputs, _ = self.fixture(False, app_backend=True)
        output = self.root / "bound"
        with self.assertRaisesRegex(ValueError, "current original source"):
            binder.bind(artifact, output, self.environment)
        self.assertFalse(output.exists())
        self.assertFalse(self.environment.exists())

        report = binder.bind(artifact, output, self.environment, app_backend=True)
        receipt_path = output / "launcher-component.json"
        receipt = json.loads(receipt_path.read_bytes())
        self.assertEqual(set(receipt), {
            "format", "component", "source_index_sha256", "modules", "profile_schemas",
        })
        self.assertEqual(receipt["format"], "io.tobkiri.launcher-setup-component.v3")
        self.assertEqual(
            report["launcher_component_receipt"], binder.fingerprint(receipt_path),
        )
        self.assertEqual(receipt["component"], binder.fingerprint(artifact))
        self.assertEqual(report["component"], receipt["component"])
        self.assertTrue(report["app_schema_backend"])
        self.assertFalse(report["component_init_verified"])
        self.assertFalse(report["production_enabled"])
        self.assertLessEqual(receipt_path.stat().st_size, 64 * 1024)

        catalog = inputs / "python/tobkiri_protocol/bundle_catalog.py"
        self.assertIn(b"return cls._load_native_snapshot(root)", catalog.read_bytes())
        raw_index = (inputs / diagnostic_stage.INDEX).read_bytes()
        index = diagnostic_stage._index_values(raw_index)
        self.assertEqual(receipt["source_index_sha256"], index["INDEX_SHA256"])
        self.assertEqual(report["source_index_sha256"], receipt["source_index_sha256"])
        base = json.loads((inputs / "provenance.json").read_bytes())
        self.assertNotEqual(
            receipt["source_index_sha256"], base["source_index"]["index_sha256"],
        )
        paths = {module: path.removeprefix("tobkiri_runtime/")
                 for module, path, _ in index["MODULES"]}
        self.assertEqual(receipt["modules"], [
            {"module": name, "relative_file": paths[name]}
            for name in index["PATH_MODULES"]
        ])
        self.assert_complete_profile_inventory(receipt["profile_schemas"])

    def test_app_receipt_rejects_rehashed_foreign_extension(self) -> None:
        artifact, _, _ = self.fixture(False, app_backend=True, tamper="extension")
        output = self.root / "bound"
        with self.assertRaisesRegex(ValueError, "App dependency differs"):
            binder.bind(artifact, output, self.environment, app_backend=True)
        self.assertFalse(output.exists())
        self.assertFalse(self.environment.exists())

    def test_app_receipt_rejects_rehashed_foreign_snapshot_capture(self) -> None:
        artifact, _, _ = self.fixture(False, app_backend=True, tamper="capture")
        output = self.root / "bound"
        with self.assertRaisesRegex(ValueError, "captured App source differs"):
            binder.bind(artifact, output, self.environment, app_backend=True)
        self.assertFalse(output.exists())
        self.assertFalse(self.environment.exists())

    def test_app_receipt_requires_complete_recovery_inventory(self) -> None:
        artifact, _, _ = self.fixture(
            False, app_backend=True, tamper="missing-recovery-schema",
        )
        output = self.root / "bound"
        with self.assertRaisesRegex(ValueError, "contain each fixed member once"):
            binder.bind(artifact, output, self.environment, app_backend=True)
        self.assertFalse(output.exists())
        self.assertFalse(self.environment.exists())

    def test_combined_app_diagnostic_requires_both_flags_and_cannot_stage_launcher(self) -> None:
        artifact, inputs, _ = self.fixture(True, app_backend=True)
        output = self.root / "bound"
        for flags in ({}, {"app_backend": True}, {"schema_call_diagnostic": True}):
            with self.subTest(flags=flags), self.assertRaises(ValueError):
                binder.bind(artifact, output, self.environment, **flags)
            self.assertFalse(output.exists())
            self.assertFalse(self.environment.exists())
        report = binder.bind(artifact, output, self.environment,
                             schema_call_diagnostic=True, app_backend=True)
        self.assertTrue(report["app_schema_backend"])
        self.assertTrue(report["schema_call_diagnostic"])
        self.assertFalse(report["whole_catalog_acceptance"])
        self.assertEqual(report["app_schema_extension"],
                         binder.fingerprint(self.root / "extension" / app_extension.ARTIFACT))
        raw_index = (inputs / diagnostic_stage.INDEX).read_bytes()
        actual_index = diagnostic_stage._index_values(raw_index)["INDEX_SHA256"]
        validation = (inputs / diagnostic_stage.VALIDATION).read_bytes()
        self.assertIn(app_extension.APP_IMPORT, validation)
        self.assertIn(
            b'    _schema_checkpoint("check-schema-start", schema_name)\n'
            + diagnostic_stage._CHECK_ANCHOR
            + b'    _schema_checkpoint("check-schema-end", schema_name)\n',
            validation,
        )
        intermediate = json.loads((inputs / "provenance.json").read_bytes())
        self.assertNotEqual(actual_index, intermediate["source_index"]["index_sha256"])
        self.assertEqual(report["source_index_sha256"], actual_index)
        self.assertIn("CI_SCHEMA_TAG_COUNT: usize = 69", (output / "module-index.rs").read_text())
        self.assertIn("CI_SCHEMA_APP_BACKEND: bool = true", (output / "module-index.rs").read_text())
        self.assertIn("TOBKIRI_TEST_NATIVE_HARNESS_SCHEMA_DIAGNOSTIC=1\n",
                      self.environment.read_text())
        self.assertFalse((output / "launcher-component.json").exists())
        self.assertNotIn("launcher_component_receipt", report)

    def test_combined_app_diagnostic_rejects_rehashed_foreign_extension(self) -> None:
        artifact, _, _ = self.fixture(True, app_backend=True, tamper="extension")
        with self.assertRaisesRegex(ValueError, "App dependency differs"):
            binder.bind(artifact, self.root / "bound", self.environment,
                        schema_call_diagnostic=True, app_backend=True)
        self.assertFalse(self.environment.exists())

    def test_combined_app_diagnostic_rejects_rehashed_foreign_loader_member(self) -> None:
        artifact, _, _ = self.fixture(True, app_backend=True, tamper="capture")
        with self.assertRaisesRegex(ValueError, "captured App source differs"):
            binder.bind(artifact, self.root / "bound", self.environment,
                        schema_call_diagnostic=True, app_backend=True)
        self.assertFalse(self.environment.exists())

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
