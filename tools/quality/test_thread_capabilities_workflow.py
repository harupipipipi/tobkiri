"""Keep interpreter diagnostics separate from the whole-source index fixture."""
from pathlib import Path
import re
import unittest

ROOT = Path(__file__).resolve().parents[2]


class DiagnosticScopeTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
