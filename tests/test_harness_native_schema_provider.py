"""Portable source tests for the explicit App schema provider selection.

No actual Wasm, extension, fuel or validation executes here. They bind the exact
source derivation and prove it changes one original statement only.
"""

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
sys.path[:0] = [str(ROOT / "scripts/wasm"), str(ROOT / "tools/python313-producer"),
                str(ROOT / "tools/python313-producer/tests")]

import stage_harness_native as staging  # noqa: E402
import component_inputs  # noqa: E402
import harness_native_dependencies as dependencies  # noqa: E402
import harness_native_provider_inputs as provider_inputs  # noqa: E402
from build_native import fingerprint  # noqa: E402
from fetch_sdk import inventory  # noqa: E402
from harness_native import schema_diagnostic_stage as diagnostic  # noqa: E402
from harness_native import schema_provider_stage as provider  # noqa: E402
import test_harness_native_dependencies as dependency_fixtures  # noqa: E402


def _digest(raw):
    return hashlib.sha256(raw).hexdigest()


def _without_selected_import(raw: bytes) -> ast.Module:
    """Parse validation with the one dependency import statement made canonical."""
    tree = ast.parse(raw)
    handler_try = next(node for node in tree.body if isinstance(node, ast.Try))
    handler_try.body = []
    return tree


class AppSchemaProviderSelectionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.baseline = staging.captured()
        cls.derived = provider.derive(cls.baseline)
        cls.proof = json.loads(cls.derived["provenance.json"])
        cls.diagnostic = diagnostic.derive(cls.baseline)
        cls.combined = provider.derive(cls.diagnostic)

    def test_only_the_dependency_import_changes_and_everything_else_is_original(self):
        original = self.baseline[provider.VALIDATION]
        derived = self.derived[provider.VALIDATION]
        self.assertEqual(derived.count(provider.SELECTED_IMPORT), 1)
        self.assertEqual(provider.unselect(derived), original)
        self.assertEqual(
            derived.replace(provider.SELECTED_IMPORT, provider.ORIGINAL_IMPORT), original,
        )
        self.assertEqual(ast.dump(_without_selected_import(derived)),
                         ast.dump(_without_selected_import(original)))
        # The actual cached loader and its single call stay original statements.
        tree = ast.parse(derived)
        original_tree = ast.parse(original)
        for name in ("load_schema", "_schema_store", "validate_document"):
            current = next(n for n in tree.body if getattr(n, "name", None) == name)
            before = next(n for n in original_tree.body if getattr(n, "name", None) == name)
            self.assertEqual(ast.dump(current), ast.dump(before), name)
        self.assertIn(b"Draft202012Validator.check_schema(payload)", derived)

    def test_derivation_is_exact_validated_and_preserves_every_other_member(self):
        self.assertEqual(provider.validate(self.derived), self.proof)
        self.assertEqual(set(self.derived), set(self.baseline))
        for name, raw in self.baseline.items():
            if name not in {provider.VALIDATION, provider.INDEX, "provenance.json"}:
                self.assertEqual(self.derived[name], raw, name)
        values = diagnostic._index_values(self.derived[provider.INDEX])
        row = next(item for item in values["MODULES"] if item[0] == "tobkiri_protocol.validation")
        self.assertEqual(row[2], _digest(self.derived[provider.VALIDATION]))
        self.assertNotEqual(row[2], _digest(self.baseline[provider.VALIDATION]))
        self.assertEqual(provider.unwrap(self.derived), self.baseline)
        derivation = self.proof["validation_derivation"]
        for field in ("provider_substitution_after_failure", "validation_retry",
                      "success_shortcut", "cache_preload"):
            self.assertIs(derivation[field], False, field)
        self.assertIs(derivation["provider_selected_before_cache_use"], True)
        self.assertIs(self.proof["default_off"], True)
        self.assertIs(self.proof["production_enabled"], False)
        self.assertIs(self.proof["diagnostic_only"], False)
        self.assertFalse(self.proof["original_source_files_unchanged"])
        self.assertTrue(self.proof["other_original_members_unchanged"])

    def test_checkpoint_diagnostic_can_be_measured_with_the_provider_selected(self):
        proof = provider.validate(self.combined)
        self.assertIs(proof["diagnostic_only"], True)
        self.assertEqual(proof["component_name"], provider.DIAGNOSTIC_COMPONENT)
        self.assertEqual(proof["checkpoint_contract"]["tag_count"], 69)
        self.assertEqual(provider.unwrap(self.combined), self.diagnostic)
        diagnostic.validate(provider.unwrap(self.combined))
        text = self.combined[provider.VALIDATION].decode()
        for marker in ('_schema_checkpoint("check-schema-start", schema_name)',
                       '_schema_checkpoint("check-schema-end", schema_name)',
                       "from app_schema_provider import AppSchemaProvider as Draft202012Validator"):
            self.assertEqual(text.count(marker), 1, marker)
        self.assertEqual(staging.captured(schema_call_diagnostic=True,
                                          app_schema_provider=True), self.combined)
        self.assertEqual(staging.captured(app_schema_provider=True), self.derived)
        self.assertEqual(staging.captured(), self.baseline)

    def test_source_index_provenance_and_import_drift_are_refused(self):
        for name in (provider.VALIDATION, provider.INDEX):
            with self.subTest(name=name):
                changed = dict(self.derived)
                changed[name] += b"\n# unrecorded change\n"
                with self.assertRaises(ValueError):
                    provider.validate(changed)
        changed = dict(self.derived)
        record = json.loads(changed["provenance.json"])
        record["undeclared_field"] = True
        changed["provenance.json"] = json.dumps(record).encode()
        with self.assertRaises(ValueError):
            provider.validate(changed)
        for raw in (self.baseline[provider.VALIDATION].replace(
                provider.ORIGINAL_IMPORT, b"    import jsonschema\n"),
                self.derived[provider.VALIDATION]):
            with self.assertRaisesRegex(ValueError, "import"):
                provider.select(raw)
        baseline = dict(self.baseline)
        baseline[provider.VALIDATION] += b"\n# post-authentication drift\n"
        with self.assertRaises(ValueError):
            provider.derive(baseline)
        with self.assertRaises(TypeError):
            staging.captured(app_schema_provider=None)
        with self.assertRaises(TypeError):
            provider.component_name(diagnostic=1)

    def test_stage_selection_is_explicit_and_baseline_stage_is_unchanged(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(
            staging, "_captured_original", return_value=self.baseline,
        ):
            root = Path(directory)
            baseline_plan = staging.stage(root / "baseline")
            self.assertEqual(baseline_plan, staging.plan_for(self.baseline))
            selected = component_inputs.stage_harness_native(
                ROOT, root / "provider", app_schema_provider=True)
            self.assertIs(selected["app_schema_provider"], True)
            self.assertNotIn("schema_call_diagnostic", selected)
            self.assertEqual((root / "provider/provenance.json").read_bytes(),
                             self.derived["provenance.json"])
            self.assertEqual((root / "provider" / provider.VALIDATION).read_bytes(),
                             self.derived[provider.VALIDATION])
            with self.assertRaises(TypeError):
                staging.stage(root / "invalid", app_schema_provider=None)

    def test_entry_derivation_adds_only_the_fixed_kernel_preinit_import(self):
        entry = (ROOT / "tools/python313-producer/harness_native/entry_wrapper.py").read_bytes()
        derived = provider_inputs.derive_entry(entry)
        self.assertEqual(derived.replace(provider_inputs.ENTRY_ADDITION, b""), entry)
        ast.parse(derived)
        self.assertLess(derived.index(b"import jsonschema"),
                        derived.index(b"import _tobkiri_schema_kernel"))
        self.assertLess(derived.index(b"import _tobkiri_schema_kernel"),
                        derived.index(b"_FINDER.seal()"))
        # Provider and model import only happens at the original validation import.
        self.assertNotIn(b"app_schema_provider", derived)
        with self.assertRaises(ValueError):
            provider_inputs.derive_entry(derived)
        with self.assertRaises(ValueError):
            provider_inputs.derive_entry(entry.replace(provider_inputs.ENTRY_ANCHOR, b""))

    def extension(self, root: Path, **changes) -> Path:
        build = root / "extension"
        artifact = build / provider_inputs.ARTIFACT
        artifact.parent.mkdir(parents=True)
        artifact.write_bytes(b"inert kernel fixture; never executed")
        report = {
            "format": "tobkiri.app-schema-kernel-extension/1", "outcome": "success",
            "guest_executed": False, "production_enabled": False,
            "source_head": os.environ["GITHUB_SHA"], "features": ["app-schema-backend"],
            "source_files": inventory(provider_inputs.SOURCE),
            "artifact_path": provider_inputs.ARTIFACT,
            "artifact": fingerprint(artifact),
        }
        report.update(changes)
        (build / "extension-build.json").write_text(json.dumps(report))
        return build

    def test_dependency_stage_adds_only_declared_provider_members_and_honest_proof(self):
        environment = {"GITHUB_ACTIONS": "true", "GITHUB_SHA": "a" * 40}
        for diagnostic_selected, source in ((False, self.derived), (True, self.combined)):
            with self.subTest(diagnostic=diagnostic_selected), tempfile.TemporaryDirectory() as directory, \
                    patch.dict(os.environ, environment), patch.object(
                        staging, "_captured_original", return_value=self.baseline):
                root = Path(directory).resolve()
                schema, stdlib, rpds, verified = dependency_fixtures.fixture(root)
                stage = root / "staged"
                original = component_inputs.stage_harness_native(
                    ROOT, stage, schema_call_diagnostic=diagnostic_selected,
                    app_schema_provider=True)
                with self.assertRaises(ValueError):
                    dependencies.stage(stage, original, verified, schema, stdlib, rpds)
                selected = dependencies.stage(
                    stage, original, verified, schema, stdlib, rpds,
                    kernel_extension=self.extension(root))
                proof = json.loads((stage / "python" / dependencies.METADATA).read_bytes())
                self.assertEqual(proof["format"], dependencies.PROVIDER_FORMAT)
                self.assertIs(selected["harness_native_dependencies"]["app_schema_provider"], True)
                self.assertIs(proof["default_off"], True)
                self.assertIs(proof["cache_preload"], False)
                self.assertIs(proof["validation_results_prepared"], False)
                self.assertIs(proof["whole_catalog_acceptance"], False)
                self.assertNotIn("provider_substitution", proof)
                self.assertIs(proof.get("diagnostic_only", False), diagnostic_selected)
                self.assertEqual(proof["kernel"]["provider_sources"].keys(),
                                 {"app_schema_provider.py", "app_schema_model.py"})
                for name, raw in source.items():
                    self.assertEqual((stage / name).read_bytes(), raw)
                for name in (provider_inputs.KERNEL_NAME, "app_schema_provider.py",
                             "app_schema_model.py"):
                    self.assertTrue((stage / "python" / name).is_file(), name)
                entry = (stage / "python" / dependencies.ENTRYPOINT).with_suffix(".py").read_bytes()
                self.assertIn(provider_inputs.ENTRY_ADDITION, entry)

    def test_extension_must_be_the_actual_source_built_app_backend(self):
        environment = {"GITHUB_ACTIONS": "true", "GITHUB_SHA": "a" * 40}
        for change in ({"features": []}, {"source_head": "b" * 40}, {"guest_executed": True},
                       {"production_enabled": True}, {"outcome": "failed"},
                       {"source_files": {}}):
            with self.subTest(change=change), tempfile.TemporaryDirectory() as directory, \
                    patch.dict(os.environ, environment):
                root = Path(directory).resolve()
                with self.assertRaises(ValueError):
                    provider_inputs.verify_extension(self.extension(root, **change))
        with tempfile.TemporaryDirectory() as directory, patch.dict(
                os.environ, {**environment, "GITHUB_ACTIONS": "false"}):
            root = Path(directory).resolve()
            with patch.dict(os.environ, environment):
                build = self.extension(root)
            with self.assertRaises(ValueError):
                provider_inputs.verify_extension(build)

    def test_component_driver_accepts_only_honest_provider_evidence(self):
        tree = ast.parse((ROOT / "tools/python313-producer/component_driver.py").read_bytes())
        main = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "run")
        branch = next(node for node in main.body if isinstance(node, ast.If)
                      and ast.unparse(node.test) == "dependencies is not None")
        start = next(index for index, node in enumerate(branch.body)
                     if isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name)
                     and node.targets[0].id == "schema_diagnostic")
        code = compile(ast.Module(body=branch.body[start:], type_ignores=[]), "producer-check", "exec")
        honest = {"format": dependencies.PROVIDER_FORMAT, "entrypoint": "entry",
                  "original_plan_sha256": "pin", "production_enabled": False,
                  "dependency_closure_complete": False, "component_init_performed": False,
                  "app_schema_provider": True, "default_off": True, "explicit_ci_selection": True,
                  "original_source_files_unchanged": False, "staged_source_files_unchanged": True,
                  "other_original_members_unchanged": True, "whole_catalog_acceptance": False,
                  "validation_results_prepared": False, "cache_preload": False,
                  "validation_derivation": {"provider_substitution_after_failure": False}}
        for control, proof, accepted in (
            ({"app_schema_provider": True}, honest, True),
            ({"app_schema_provider": True, "schema_call_diagnostic": True},
             {**honest, "diagnostic_only": True}, True),
            ({"app_schema_provider": True}, {**honest, "cache_preload": True}, False),
            ({"app_schema_provider": True}, {**honest, "default_off": False}, False),
            ({"app_schema_provider": True}, {**honest, "format": dependencies.DIAGNOSTIC_FORMAT}, False),
            ({}, honest, False),
            ({"app_schema_provider": 1}, honest, False),
            ({"app_schema_provider": True, "schema_call_diagnostic": True}, honest, False),
        ):
            namespace = {"dependencies": {**control, "original_plan_sha256": "pin"},
                         "dependency_evidence": proof, "plan": {"entrypoint": "entry"}}
            with self.subTest(control=control, format=proof["format"]):
                if accepted:
                    exec(code, namespace)
                else:
                    with self.assertRaises(ValueError):
                        exec(code, namespace)


if __name__ == "__main__":
    unittest.main()
