"""Keep interpreter diagnostics separate from the whole-source index fixture."""
import ast
from pathlib import Path
import re
import unittest

ROOT = Path(__file__).resolve().parents[2]


class DiagnosticScopeTests(unittest.TestCase):
    def test_closed_host_phases_match_actual_guest_import_boundaries_and_waiter_gate(self):
        guest = ROOT / "tools/python313-producer/harness_thread_capabilities/logic.py"
        tree = ast.parse(guest.read_text())
        assignments = sorted(
            (node.lineno, node.value.value)
            for node in ast.walk(tree)
            if isinstance(node, ast.Assign)
            and any(isinstance(target, ast.Name) and target.id == "phase"
                    for target in node.targets)
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
        )
        phases = [value for _, value in assignments]
        native = (ROOT / "tobkiri_launcher/harness-native/src/thread_capabilities_actual_ci.rs").read_text()
        stage_array = re.search(r'const STAGES:.*?= \[(.*?)\];', native, re.S)
        self.assertIsNotNone(stage_array)
        self.assertEqual(re.findall(r'"([a-z-]+)"', stage_array.group(1)), phases)
        waiting = re.search(r'const WORKER_WAITING_STAGE: u8 = (\d+);', native)
        self.assertIsNotNone(waiting)
        self.assertEqual(phases[int(waiting.group(1)) - 1], "worker-waiting")
        for name in ("contextvars", "struct", "sys", "threading"):
            importing = next(node for node in ast.walk(tree)
                             if isinstance(node, ast.Import)
                             and any(alias.name == name for alias in node.names))
            before = next(line for line, value in assignments if value == f"import-{name}")
            after = next(line for line, value in assignments if value == f"{name}-imported")
            self.assertLess(before, importing.lineno)
            self.assertLess(importing.lineno, after)

    def test_diagnostic_feature_does_not_require_or_fabricate_whole_source_index(self):
        cargo = (ROOT / "tobkiri_launcher/harness-native/Cargo.toml").read_text()
        lib = (ROOT / "tobkiri_launcher/harness-native/src/lib.rs").read_text()
        bootstrap = (ROOT / "tobkiri_launcher/harness-native/src/bootstrap_runtime.rs").read_text()
        workflow = (ROOT / ".github/workflows/issue-1520-thread-capabilities.yml").read_text()
        self.assertIn("thread-capabilities-ci = []", cargo)
        diagnostic = re.search(r'#\[cfg\(all\((.*?)\)\)\]\s*mod thread_capabilities_actual_ci;',
                               lib, re.S)
        self.assertIsNotNone(diagnostic)
        self.assertIn('feature = "thread-capabilities-ci"', diagnostic.group(1))
        self.assertNotIn('feature = "bootstrap-ci"', diagnostic.group(1))
        self.assertIn('feature = "bootstrap-ci"', bootstrap)
        self.assertIn("--features thread-capabilities-ci", workflow)
        self.assertNotIn("--features bootstrap-ci", workflow)
        self.assertNotIn("TOBKIRI_HARNESS_NATIVE_MODULE_INDEX_RS", workflow)

    def test_constraint_uses_real_owned_calls_without_a_public_raw_store_escape(self):
        source = (ROOT / "crates/tobkiri-wasm/src/native_sync_reentry_ci.rs").read_text()
        self.assertIn("native_setup_owner_lease", source)
        self.assertIn("func_new_async_owned", source)
        self.assertIn("call_async_owned", source)
        self.assertNotIn(".call_async(", source)
        self.assertNotIn("unsafe", source)
        self.assertIn("same_task=true independent_task=false", source)
        self.assertIn("invocation.deadline()? == original_deadline", source)
        workflow = (ROOT / ".github/workflows/issue-1520-thread-capabilities.yml").read_text()
        self.assertIn("steps.constraint-compile.conclusion == 'success'", workflow)


if __name__ == "__main__":
    unittest.main()
