"""Source-only checks for the bounded import-audit diagnostic candidate."""

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
PRODUCER = ROOT / "tools/python313-producer"
sys.path[:0] = [
    str(ROOT / "scripts/wasm"),
    str(PRODUCER),
    str(PRODUCER / "harness_input_stack"),
    str(ROOT / "packs/local.ai-tool-bridge-ordered-python"),
    str(ROOT / "tobkiri_runtime"),
]

import build_components  # noqa: E402
import component_inputs  # noqa: E402
import prepare_harness_input_stack as baseline  # noqa: E402
import prepare_harness_input_stack_import_diagnostics as diagnostic  # noqa: E402
from fetch_sdk import inventory  # noqa: E402


class ImportDiagnosticsSourceTests(unittest.TestCase):
    def test_candidate_is_separate_and_pins_the_exact_original_source(self) -> None:
        original_before = baseline.captured()
        files = diagnostic.captured()
        original_after = baseline.captured()
        provenance = json.loads(files["provenance.json"])
        base_provenance = json.loads(original_before["provenance.json"])
        plan = diagnostic.plan_for(files)
        unsigned_provenance = dict(provenance)
        metadata_hash = unsigned_provenance.pop("metadata_sha256")

        self.assertEqual(original_before, original_after)
        self.assertEqual(plan["format"], "tobkiri.python-build/3")
        self.assertEqual(plan["world"], diagnostic.WORLD)
        self.assertEqual(plan["outer_exports"], [diagnostic.PROBE_INTERFACE])
        self.assertEqual(
            plan["allowed_imports"],
            [diagnostic.CAPTURED_INTERFACE, diagnostic.IMPORT_TRACE_INTERFACE],
        )
        self.assertEqual(
            files["python/baseline_app.py"], original_before["python/app.py"]
        )
        self.assertEqual(files["python/logic.py"], original_before["python/logic.py"])
        self.assertEqual(
            files["wit/deps/captured-inputs/native-inputs.wit"],
            original_before["wit/deps/captured-inputs/native-inputs.wit"],
        )
        self.assertEqual(
            files["wit/probe.wit"],
            diagnostic.bounded_read(
                ROOT
                / "tools"
                / "python313-producer"
                / "harness_input_stack_import_diagnostics"
                / "probe.wit"
            ),
        )
        self.assertEqual(
            files["wit/deps/diagnostics/import-trace.wit"],
            diagnostic.bounded_read(
                ROOT
                / "tools"
                / "python313-producer"
                / "harness_input_stack_import_diagnostics"
                / "import-trace.wit"
            ),
        )

        unchanged = set(original_before) - {
            "provenance.json",
            "python/app.py",
            "wit/probe.wit",
        }
        for name in unchanged:
            self.assertEqual(files[name], original_before[name], name)

        self.assertEqual(
            provenance["baseline_source_provenance_sha256"],
            hashlib.sha256(original_before["provenance.json"]).hexdigest(),
        )
        self.assertEqual(
            provenance["baseline_app_sha256"],
            hashlib.sha256(original_before["python/app.py"]).hexdigest(),
        )
        self.assertEqual(
            provenance["diagnostic_app_sha256"],
            hashlib.sha256(files["python/app.py"]).hexdigest(),
        )
        self.assertEqual(
            provenance["original_function_bodies"],
            base_provenance["original_function_bodies"],
        )
        for key in (
            "build_performed",
            "actual_build_performed",
            "wasm_component_built",
            "runtime_performed",
            "wasm_execution_performed",
            "guest_runtime_executed",
            "production_enabled",
            "whole_setup_performed",
            "full_setup_performed",
            "native_trace_observed",
            "native_result_claimed",
        ):
            self.assertIs(provenance[key], False, key)
        self.assertTrue(provenance["diagnostic_outcome_must_fail"])
        self.assertTrue(provenance["source_capsule_only"])
        self.assertFalse(provenance["source_capsule_executable"])
        self.assertEqual(
            metadata_hash,
            hashlib.sha256(
                diagnostic._canonical_json_bytes(unsigned_provenance)
            ).hexdigest(),
        )

        source_records = {
            item["staged_as"]: item for item in provenance["sources"]
        }
        self.assertEqual(set(source_records), set(files) - {"provenance.json"})
        for staged_as, record in source_records.items():
            self.assertEqual(record["bytes"], len(files[staged_as]), staged_as)
            self.assertEqual(
                record["sha256"],
                hashlib.sha256(files[staged_as]).hexdigest(),
                staged_as,
            )
        self.assertEqual(
            provenance["baseline_sources"],
            [
                {
                    "source_path": item["source_path"],
                    "original_staged_as": item["staged_as"],
                    "sha256": item["sha256"],
                    "bytes": item["bytes"],
                }
                for item in base_provenance["sources"]
            ],
        )

        baseline_plan = baseline.plan_for(original_before)
        self.assertEqual(baseline_plan["world"], "native-input-stack")
        self.assertEqual(
            baseline_plan["allowed_imports"], [baseline.CAPTURED_INTERFACE]
        )
        self.assertNotIn(
            diagnostic.IMPORT_TRACE_INTERFACE, baseline_plan["allowed_imports"]
        )

    def test_diagnostic_wit_and_app_are_limited_to_one_bounded_import_hook(
        self,
    ) -> None:
        files = diagnostic.captured()
        app_source = files["python/app.py"].decode("utf-8")
        app_tree = ast.parse(app_source)
        probe_wit = files["wit/probe.wit"].decode("utf-8")
        trace_wit = files["wit/deps/diagnostics/import-trace.wit"].decode("utf-8")

        self.assertIn("package tobkiri-ui:native-input-stack@1.0.0;", probe_wit)
        self.assertIn("world native-input-stack-import-diagnostics", probe_wit)
        self.assertIn("import tobkiri-ui:inputs/captured@1.0.0;", probe_wit)
        self.assertIn(
            "import tobkiri-ui:diagnostics/import-trace@1.0.0;", probe_wit
        )
        self.assertIn("export probe;", probe_wit)
        self.assertEqual(
            [line.strip() for line in trace_wit.splitlines() if "func(" in line],
            ["record-import: func(name: string);"],
        )
        self.assertEqual(trace_wit.count("func("), 1)

        imports = []
        for node in app_tree.body:
            if isinstance(node, ast.Import):
                imports.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                imports.append(node.module)
        self.assertEqual(
            set(imports), {"sys", "baseline_app", "wit_world.imports"}
        )
        self.assertNotIn("os", imports)
        self.assertNotIn("pathlib", imports)

        baseline_import = next(
            node
            for node in app_tree.body
            if isinstance(node, ast.ImportFrom) and node.module == "baseline_app"
        )
        trace_import = next(
            node
            for node in app_tree.body
            if isinstance(node, ast.ImportFrom) and node.module == "wit_world.imports"
        )
        self.assertEqual(
            [(alias.name, alias.asname) for alias in trace_import.names],
            [("import_trace", None)],
        )
        reference_binding = next(
            node
            for node in app_tree.body
            if isinstance(node, ast.Assign)
            and any(
                isinstance(target, ast.Name) and target.id == "_record_import"
                for target in node.targets
            )
        )
        self.assertIsInstance(reference_binding.value, ast.Attribute)
        self.assertEqual(reference_binding.value.attr, "record_import")
        self.assertIsInstance(reference_binding.value.value, ast.Name)
        self.assertEqual(reference_binding.value.value.id, "import_trace")
        hook_install = next(
            node
            for node in ast.walk(app_tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "sys"
            and node.func.attr == "addaudithook"
        )
        self.assertLess(baseline_import.lineno, hook_install.lineno)
        self.assertLess(trace_import.lineno, hook_install.lineno)
        self.assertLess(reference_binding.lineno, hook_install.lineno)

        hook = next(
            node
            for node in app_tree.body
            if isinstance(node, ast.FunctionDef) and node.name == "_import_audit"
        )
        hook_calls = [node for node in ast.walk(hook) if isinstance(node, ast.Call)]
        record_calls = [
            node
            for node in hook_calls
            if isinstance(node.func, ast.Name) and node.func.id == "_record_import"
        ]
        self.assertEqual(len(record_calls), 1)
        self.assertEqual(len(record_calls[0].args), 1)
        hook_nodes = list(ast.walk(hook))
        self.assertFalse(
            any(isinstance(node, (ast.Import, ast.ImportFrom)) for node in hook_nodes)
        )
        self.assertFalse(
            any(
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id in {"str", "repr", "open", "print"}
                for node in hook_nodes
            )
        )
        self.assertIn(
            'args[0] if args and type(args[0]) is str else ""', app_source
        )

        probe_class = next(
            node
            for node in app_tree.body
            if isinstance(node, ast.ClassDef) and node.name == "Probe"
        )
        self.assertEqual(
            [
                base.id
                for base in probe_class.bases
                if isinstance(base, ast.Name)
            ],
            ["_BaselineProbe"],
        )
        self.assertEqual(
            [
                node.name
                for node in probe_class.body
                if isinstance(node, ast.FunctionDef)
            ],
            ["__init__", "run"],
        )
        init_method = next(
            node
            for node in probe_class.body
            if isinstance(node, ast.FunctionDef) and node.name == "__init__"
        )
        init_nodes = list(ast.walk(init_method))
        self.assertTrue(
            any(
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Call)
                and isinstance(node.func.value.func, ast.Name)
                and node.func.value.func.id == "super"
                and node.func.attr == "__init__"
                for node in init_nodes
            )
        )
        self.assertTrue(
            any(
                isinstance(node, ast.Assign)
                and isinstance(node.targets[0], ast.Attribute)
                and node.targets[0].attr == "_import_audit_installed"
                and isinstance(node.value, ast.Constant)
                and node.value.value is False
                for node in init_method.body
            )
        )
        run_method = next(
            node
            for node in probe_class.body
            if isinstance(node, ast.FunctionDef) and node.name == "run"
        )
        run_calls = [
            node for node in ast.walk(run_method) if isinstance(node, ast.Call)
        ]
        self.assertEqual(
            sum(isinstance(node, ast.Return) for node in ast.walk(run_method)), 1
        )
        self.assertFalse(
            any(isinstance(node, ast.Try) for node in ast.walk(run_method))
        )
        self.assertEqual(
            sum(
                isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "sys"
                and node.func.attr == "addaudithook"
                for node in run_calls
            ),
            1,
        )
        audit_flag = next(
            node
            for node in run_method.body
            if isinstance(node, ast.If)
            and isinstance(node.test, ast.UnaryOp)
            and isinstance(node.test.operand, ast.Attribute)
            and node.test.operand.attr == "_import_audit_installed"
        )
        flag_set = next(
            node
            for node in audit_flag.body
            if isinstance(node, ast.Assign)
            and isinstance(node.targets[0], ast.Attribute)
            and node.targets[0].attr == "_import_audit_installed"
        )
        super_run = next(
            node
            for node in run_calls
            if isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Call)
            and isinstance(node.func.value.func, ast.Name)
            and node.func.value.func.id == "super"
            and node.func.attr == "run"
        )
        self.assertLess(hook_install.lineno, flag_set.lineno)
        self.assertLess(flag_set.lineno, super_run.lineno)
        self.assertTrue(
            any(
                isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Call)
                and isinstance(node.func.value.func, ast.Name)
                and node.func.value.func.id == "super"
                and node.func.attr == "run"
                for node in run_calls
            )
        )
        self.assertNotIn("logging", app_source)
        self.assertNotIn("tobkiri_runtime", app_source)

    def test_source_stage_is_private_and_rejects_overwrite(self) -> None:
        original_plans = component_inputs.plans(True)
        original_path = sys.path[:]
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory).resolve() / "diagnostic-stage"
            receipt = component_inputs.stage_native_input_stack_import_diagnostics(
                ROOT, destination
            )
            plan = json.loads((destination / "plan.json").read_bytes())
            self.assertEqual(receipt["files"], inventory(destination))
            self.assertEqual(plan["world"], diagnostic.WORLD)
            self.assertEqual(plan["outer_exports"], [diagnostic.PROBE_INTERFACE])
            self.assertEqual(
                plan["allowed_imports"],
                [diagnostic.CAPTURED_INTERFACE, diagnostic.IMPORT_TRACE_INTERFACE],
            )
            self.assertEqual(
                receipt["plan_sha256"],
                hashlib.sha256((destination / "plan.json").read_bytes()).hexdigest(),
            )
            with self.assertRaises(FileExistsError):
                component_inputs.stage_native_input_stack_import_diagnostics(
                    ROOT, destination
                )
        self.assertEqual(component_inputs.plans(True), original_plans)
        self.assertEqual(sys.path, original_path)

    def test_builder_has_a_distinct_exclusive_mode_and_retains_bindings(self) -> None:
        source = (PRODUCER / "build_components.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        run = next(
            node
            for node in tree.body
            if isinstance(node, ast.FunctionDef) and node.name == "run"
        )
        arguments = {argument.arg for argument in run.args.kwonlyargs}
        self.assertIn("native_input_stack_import_diagnostics", arguments)
        self.assertIn(
            "tobkiri.python313-native-input-stack-import-diagnostics-candidate/1",
            source,
        )
        self.assertIn("harness-input-stack-import-diagnostics", source)
        self.assertIn("stage_native_input_stack_import_diagnostics", source)
        self.assertIn(
            "native_input_stack or native_input_stack_import_diagnostics", source
        )
        self.assertIn("--native-input-stack-import-diagnostics", source)
        selector_group = next(
            node
            for node in tree.body
            if isinstance(node, ast.If)
            and isinstance(node.test, ast.Compare)
            and any(
                isinstance(item, ast.Constant)
                and item.value == "__main__"
                for item in node.test.comparators
            )
        )
        group_assignment = next(
            node
            for node in selector_group.body
            if isinstance(node, ast.Assign)
            and isinstance(node.targets[0], ast.Name)
            and node.targets[0].id == "mode"
        )
        self.assertIsInstance(group_assignment.value, ast.Call)
        self.assertIsInstance(group_assignment.value.func, ast.Attribute)
        self.assertEqual(
            group_assignment.value.func.attr, "add_mutually_exclusive_group"
        )
        mode_flags = {
            node.args[0].value
            for node in ast.walk(selector_group)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "mode"
            and node.func.attr == "add_argument"
            and node.args
            and isinstance(node.args[0], ast.Constant)
        }
        self.assertEqual(
            mode_flags,
            {
                "--all-five",
                "--harness-setup",
                "--harness-native",
                "--native-input-stack",
                "--native-input-stack-import-diagnostics",
                "--native-thread-capabilities",
            },
        )

        with patch.object(build_components.sys, "platform", "linux"), patch.dict(
            os.environ, {"GITHUB_ACTIONS": "true"}
        ):
            with self.assertRaisesRegex(ValueError, "separate build scopes"):
                build_components.run(
                    None,
                    Path("/unused-producer"),
                    Path("/unused-validation"),
                    Path("/unused-destination"),
                    native_input_stack=True,
                    native_input_stack_import_diagnostics=True,
                )

    def test_diagnostic_workflow_keeps_failure_only_native_filter_and_artifacts(
        self,
    ) -> None:
        workflow = (
            ROOT
            / ".github/workflows/issue-1520-native-input-stack-import-diagnostics.yml"
        ).read_text(encoding="utf-8")
        self.assertIn("runs-on: macos-15", workflow)
        self.assertIn("test \"$(uname -r)\" = 24.6.0", workflow)
        self.assertIn("Type \\(Bundle\\):", workflow)
        self.assertIn("python-version: '3.13.16'", workflow)
        self.assertIn("--native-input-stack-import-diagnostics", workflow)
        self.assertIn("--expected-passed 1", workflow)
        # This historical failure-only workflow is now dispatched explicitly;
        # its source paths are no longer automatic push-trigger entries.
        events = workflow.split("permissions:", 1)[0]
        self.assertIn("workflow_dispatch:", events)
        self.assertNotIn("push:", events)
        trace = ROOT / "crates/tobkiri-wasm/src/native_import_trace.rs"
        trace_tests = trace.with_name("native_import_trace_tests.rs")
        self.assertIn('#[path = "native_import_trace_tests.rs"]', trace.read_text())
        self.assertTrue(trace_tests.is_file())
        self.assertIn(
            "import_audit_diagnostic_preserves_the_original_python_stack_failure",
            workflow,
        )
        self.assertEqual(
            workflow.count("TOBKIRI_TEST_INPUT_STACK_IMPORT_DIAGNOSTICS: '1'"), 1
        )
        self.assertIn("native-input-stack-import-diagnostics-build-candidate", workflow)
        self.assertIn("native-input-stack-import-diagnostics-native-evidence", workflow)
        self.assertNotIn("continue-on-error", workflow)


if __name__ == "__main__":
    unittest.main()
