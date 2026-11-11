"""Portable source/controlled-call tests; no actual Wasm, IO or fuel proof."""

from __future__ import annotations

import ast
import copy
from functools import lru_cache
import hashlib
import json
from pathlib import Path
import sys
import tempfile
from types import ModuleType, SimpleNamespace
from typing import Any
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "scripts/wasm"), str(ROOT / "tools/python313-producer"),
                str(ROOT / "tools/python313-producer/tests")]

import stage_harness_native as staging  # noqa: E402
import component_inputs  # noqa: E402
import harness_native_dependencies as dependencies  # noqa: E402
from harness_native import schema_diagnostic_stage as diagnostic  # noqa: E402
import test_harness_native_dependencies as dependency_fixtures  # noqa: E402


def _digest(raw):
    return hashlib.sha256(raw).hexdigest()


class SchemaCheckpointDiagnosticTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Actual authenticated source capture; no original module executes.
        cls.baseline = staging.captured()
        cls.derived = diagnostic.derive(cls.baseline)
        cls.provenance = json.loads(cls.derived["provenance.json"])

    def helper(self, inspect):
        bindings = ModuleType("wit_world.imports.bindings")
        bindings.inspect = inspect
        modules = {"wit_world": ModuleType("wit_world"),
                   "wit_world.imports": ModuleType("wit_world.imports"),
                   "wit_world.imports.bindings": bindings}
        namespace = {"__name__": "controlled_schema_checkpoint"}
        exec(compile(self.derived[diagnostic.HELPER], diagnostic.HELPER, "exec"), namespace)
        return namespace, patch.dict(sys.modules, modules)

    def validation(self, checkpoint, provider=None):
        events = []

        class SchemaFailure(ValueError):
            pass

        class Resource:
            def __init__(self, name):
                self.name = name

            def is_file(self):
                return True

            def read_bytes(self):
                events.append(("read", self.name))
                return self.name.encode()

            def __str__(self):
                return "controlled-schema/" + self.name

        names = ["pack_manifest_v4.schema.json", "contract_v4.schema.json"]
        resources = SimpleNamespace(
            joinpath=lambda name: Resource(name),
            iterdir=lambda: iter(Resource(name) for name in names),
        )

        def parse(raw):
            payload = {"$id": raw.decode()}
            events.append(("parsed", payload))
            return payload

        def check(payload):
            events.append(("provider", payload))
            if provider is not None:
                return provider(payload)

        tree = ast.parse(self.derived[diagnostic.VALIDATION])
        functions = [copy.deepcopy(node) for node in tree.body
                     if isinstance(node, ast.FunctionDef)
                     and node.name in {"load_schema", "_schema_store"}]
        self.assertEqual(len(functions), 2)
        namespace = {
            "Any": Any, "lru_cache": lru_cache, "SCHEMA_ALIASES": {"pack": names[0]},
            "_captured_schema_resources": lambda: resources, "strict_loads": parse,
            "ProtocolError": type("ControlledProtocolError", (ValueError,), {}),
            "SchemaValidationError": SchemaFailure,
            "Draft202012Validator": SimpleNamespace(check_schema=check),
            "_schema_checkpoint": checkpoint,
        }
        exec(compile(ast.Module(body=functions, type_ignores=[]),
                     "controlled-original-validation", "exec"), namespace)
        return namespace, events, SchemaFailure

    def test_derivation_binds_actual_bytes_and_preserves_every_other_original_member(self):
        proof = diagnostic.validate(self.derived)
        self.assertEqual(proof, self.provenance)
        self.assertEqual(set(self.derived) - set(self.baseline), {diagnostic.HELPER})
        for name, raw in self.baseline.items():
            if name not in {diagnostic.VALIDATION, diagnostic.INDEX, "provenance.json"}:
                self.assertEqual(self.derived[name], raw, name)
        values = diagnostic._index_values(self.derived[diagnostic.INDEX])
        validation = next(row for row in values["MODULES"] if row[0] == "tobkiri_protocol.validation")
        self.assertEqual(validation[2], _digest(self.derived[diagnostic.VALIDATION]))
        self.assertNotEqual(validation[2], _digest(self.baseline[diagnostic.VALIDATION]))
        self.assertFalse(proof["original_source_files_unchanged"])
        self.assertTrue(proof["other_original_members_unchanged"])
        self.assertFalse(proof["validation_derivation"]["provider_substitution"])
        self.assertEqual(proof["checkpoint_contract"]["tag_count"], 69)
        self.assertIn('Draft202012Validator.check_schema(payload)',
                      self.derived[diagnostic.VALIDATION].decode())
        self.assertIn('+    _schema_checkpoint("check-schema-start", schema_name)',
                      proof["validation_derivation"]["diff"])

    def test_baseline_stage_is_unchanged_and_diagnostic_requires_explicit_selection(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(
            staging, "_captured_original", return_value=self.baseline,
        ):
            root = Path(directory)
            baseline_plan = staging.stage(root / "baseline")
            self.assertEqual(baseline_plan, staging.plan_for(self.baseline))
            for name, raw in self.baseline.items():
                self.assertEqual((root / "baseline" / name).read_bytes(), raw)
            self.assertFalse((root / "baseline" / diagnostic.HELPER).exists())
            selected = component_inputs.stage_harness_native(
                ROOT, root / "diagnostic", schema_call_diagnostic=True)
            self.assertIs(selected["schema_call_diagnostic"], True)
            self.assertEqual(json.loads((root / "diagnostic/provenance.json").read_bytes()),
                             self.provenance)
            with self.assertRaises(TypeError):
                staging.stage(root / "invalid", schema_call_diagnostic=None)

    def test_source_callsite_index_helper_and_provenance_drift_are_refused(self):
        baseline = dict(self.baseline)
        baseline[diagnostic.VALIDATION] += b"\n# post-authentication drift\n"
        with self.assertRaisesRegex(ValueError, "baseline component source"):
            diagnostic.derive(baseline)
        with self.assertRaisesRegex(ValueError, "callsite changed"):
            diagnostic.instrument(self.baseline[diagnostic.VALIDATION].replace(
                b"check_schema(payload)", b"check_schema(dict(payload))"))
        for name in (diagnostic.VALIDATION, diagnostic.INDEX, diagnostic.HELPER):
            with self.subTest(name=name):
                changed = dict(self.derived)
                changed[name] += b"\n# unrecorded change\n"
                with self.assertRaises(ValueError):
                    diagnostic.validate(changed)
        changed = dict(self.derived)
        proof = json.loads(changed["provenance.json"])
        proof["undeclared_field"] = True
        changed["provenance.json"] = json.dumps(proof).encode()
        with self.assertRaises(ValueError):
            diagnostic.validate(changed)

    def test_real_cache_keeps_alias_filename_distinct_and_store_body_uncached_only(self):
        checkpoints = []
        namespace, events, _ = self.validation(lambda *args: checkpoints.append(args))
        load, store = namespace["load_schema"], namespace["_schema_store"]
        first = load("pack")
        self.assertIs(load("pack"), first)
        second = load("pack_manifest_v4.schema.json")
        self.assertIsNot(second, first)
        self.assertEqual([item[0] for item in events].count("provider"), 2)
        self.assertEqual(checkpoints[:4], [
            ("check-schema-start", "pack"), ("check-schema-end", "pack"),
            ("check-schema-start", "pack_manifest_v4.schema.json"),
            ("check-schema-end", "pack_manifest_v4.schema.json"),
        ])
        result = store()
        self.assertIs(store(), result)
        self.assertEqual(checkpoints.count(("schema-store-entry",)), 1)
        self.assertEqual(set(result), {"pack_manifest_v4.schema.json", "contract_v4.schema.json"})
        self.assertEqual(load.cache_info().currsize, 3)
        parsed = [item[1] for item in events if item[0] == "parsed"]
        supplied = [item[1] for item in events if item[0] == "provider"]
        self.assertTrue(all(left is right for left, right in zip(parsed, supplied)))

    def test_actual_provider_throw_keeps_original_cause_and_has_no_normal_end(self):
        checkpoints = []
        supplied = ValueError("controlled provider failure")

        def fail(_payload):
            raise supplied

        namespace, _, failure_class = self.validation(lambda *args: checkpoints.append(args), fail)
        with self.assertRaises(failure_class) as caught:
            namespace["load_schema"]("pack")
        self.assertIs(caught.exception.__cause__, supplied)
        self.assertIs(caught.exception.__context__, supplied)
        self.assertTrue(caught.exception.__suppress_context__)
        self.assertEqual(checkpoints, [("check-schema-start", "pack")])
        self.assertEqual(namespace["load_schema"].cache_info().currsize, 0)

    def test_measurement_failures_remain_fatal_outside_original_catch_and_cache(self):
        for failed_stage in ("check-schema-start", "check-schema-end"):
            with self.subTest(stage=failed_stage):
                supplied = MemoryError("controlled diagnostic capacity failure")

                def measure(stage, _name):
                    if stage == failed_stage:
                        raise supplied

                namespace, events, _ = self.validation(measure)
                with self.assertRaises(MemoryError) as caught:
                    namespace["load_schema"]("pack")
                self.assertIs(caught.exception, supplied)
                self.assertEqual([event[0] for event in events].count("provider"),
                                 int(failed_stage == "check-schema-end"))
                self.assertEqual(namespace["load_schema"].cache_info().currsize, 0)

    def test_fixed_frames_have_numeric_distinct_tags_and_ignore_the_ack(self):
        frames = []

        class UnusableAcknowledgment:
            def __bool__(self):
                raise AssertionError("ack must not select validation behavior")

        namespace, modules = self.helper(
            lambda raw: (frames.append(json.loads(raw)), UnusableAcknowledgment())[1])
        with modules:
            self.assertIsNone(namespace["checkpoint"]("check-schema-start", "pack"))
            namespace["checkpoint"]("check-schema-end", "pack_manifest_v4.schema.json")
            namespace["checkpoint"]("schema-store-entry")
        self.assertEqual([row["sequence"] for row in frames], [1, 2, 3])
        self.assertNotEqual(frames[0]["schema_tag"], frames[1]["schema_tag"])
        self.assertIsNone(frames[2]["schema_tag"])
        for row in frames:
            self.assertEqual(set(row), {"format", "sequence", "stage", "schema_tag"})
            self.assertLess(len(json.dumps(row).encode()), 512)
            self.assertEqual(row["format"], diagnostic.FRAME_FORMAT)

    def test_full_declared_inventory_fits_without_interrupting_validation(self):
        frames = []
        namespace, modules = self.helper(lambda raw: frames.append(json.loads(raw)))
        tags = self.provenance["checkpoint_contract"]["schema_tags"]
        with modules:
            namespace["checkpoint"]("schema-store-entry")
            for row in tags:
                namespace["checkpoint"]("check-schema-start", row["name"])
                namespace["checkpoint"]("check-schema-end", row["name"])
        self.assertEqual(len(frames), 69 * 2 + 1)
        self.assertEqual([row["sequence"] for row in frames], list(range(1, 140)))
        self.assertEqual(
            self.provenance["checkpoint_contract"]["maximum_sequence"], 139
        )

    def test_failed_schema_retries_preserve_original_errors_after_overflow(self):
        frames = []
        helper, modules = self.helper(lambda raw: frames.append(json.loads(raw)))
        supplied = ValueError("original repeated schema failure")

        def fail(_payload):
            raise supplied

        namespace, events, failure_class = self.validation(helper["checkpoint"], fail)
        with modules:
            for _ in range(150):
                with self.assertRaises(failure_class) as caught:
                    namespace["load_schema"]("pack")
                self.assertIs(caught.exception.__cause__, supplied)
        self.assertEqual(len([event for event in events if event[0] == "provider"]), 150)
        self.assertEqual(namespace["load_schema"].cache_info().currsize, 0)
        self.assertEqual([row["sequence"] for row in frames], list(range(1, 141)))

    def test_checkpoint_unknown_inputs_exhaustion_and_import_failure_do_not_fallback(self):
        frames = []
        namespace, modules = self.helper(lambda raw: frames.append(json.loads(raw)))
        with modules:
            for args in (("unknown", "pack"), ("check-schema-start", "unknown"),
                         ("check-schema-start", None), ("schema-store-entry", "pack")):
                with self.subTest(args=args), self.assertRaises(RuntimeError):
                    namespace["checkpoint"](*args)
            with self.assertRaises(TypeError):
                namespace["checkpoint"]("schema-store-entry", unexpected=True)
            self.assertEqual(frames, [])
            namespace["_sequence"] = 138
            namespace["checkpoint"]("schema-store-entry")
            for _ in range(20):
                namespace["checkpoint"]("schema-store-entry")
            self.assertEqual([row["sequence"] for row in frames], [139, 140])
        supplied = MemoryError("controlled native import failure")

        def fail(_raw):
            raise supplied

        namespace, modules = self.helper(fail)
        with modules, self.assertRaises(MemoryError) as caught:
            namespace["checkpoint"]("schema-store-entry")
        self.assertIs(caught.exception, supplied)
        self.assertEqual(namespace["_sequence"], 1)

    def test_dependency_stage_has_distinct_proof_and_keeps_actual_derived_source(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(
            staging, "_captured_original", return_value=self.baseline,
        ):
            root = Path(directory)
            schema, stdlib, rpds, verified = dependency_fixtures.fixture(root)
            stage = root / "staged"
            original = component_inputs.stage_harness_native(ROOT, stage, schema_call_diagnostic=True)
            selected = dependencies.stage(stage, original, verified, schema, stdlib, rpds)
            proof = json.loads((stage / "python" / dependencies.METADATA).read_bytes())
            self.assertEqual(proof["format"], dependencies.DIAGNOSTIC_FORMAT)
            self.assertFalse(proof["original_source_files_unchanged"])
            self.assertTrue(proof["other_original_members_unchanged"])
            self.assertIs(selected["harness_native_dependencies"]["schema_call_diagnostic"], True)
            self.assertEqual(proof["checkpoint_contract"], self.provenance["checkpoint_contract"])
            for name, raw in self.derived.items():
                self.assertEqual((stage / name).read_bytes(), raw)

    def test_component_driver_requires_both_explicit_control_and_diagnostic_format(self):
        # Execute only the exact pure evidence-check block, not the producer.
        tree = ast.parse((ROOT / "tools/python313-producer/component_driver.py").read_bytes())
        main = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "run")
        branch = next(node for node in main.body if isinstance(node, ast.If)
                      and ast.unparse(node.test) == "dependencies is not None")
        start = next(index for index, node in enumerate(branch.body)
                     if isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name)
                     and node.targets[0].id == "schema_diagnostic")
        code = compile(ast.Module(body=branch.body[start:], type_ignores=[]), "producer-check", "exec")
        baseline = {"format": "tobkiri.harness-native-dependencies/1", "entrypoint": "entry",
                    "original_plan_sha256": "pin", "production_enabled": False,
                    "dependency_closure_complete": False, "component_init_performed": False}
        derived = {**baseline, "format": dependencies.DIAGNOSTIC_FORMAT, "diagnostic_only": True,
                   "original_source_files_unchanged": False, "staged_source_files_unchanged": True,
                   "other_original_members_unchanged": True, "provider_substitution": False,
                   "whole_catalog_acceptance": False}
        for flag, proof, accepted in ((False, baseline, True), (True, derived, True),
                                      (False, derived, False), (True, baseline, False),
                                      (1, derived, False)):
            namespace = {"dependencies": {"schema_call_diagnostic": flag, "original_plan_sha256": "pin"},
                         "dependency_evidence": proof, "plan": {"entrypoint": "entry"}}
            with self.subTest(flag=flag, format=proof["format"]):
                if accepted:
                    exec(code, namespace)
                else:
                    with self.assertRaises(ValueError):
                        exec(code, namespace)


if __name__ == "__main__":
    unittest.main()
