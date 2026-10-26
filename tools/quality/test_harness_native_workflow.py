"""Source contracts for the gated whole-component native CI workflow."""
from __future__ import annotations

from pathlib import Path
import re
import unittest

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github/workflows/issue-1520-harness-native.yml"


def job(source: str, name: str) -> str:
    """Select one top-level workflow job without third-party YAML tooling."""
    match = re.search(
        rf"^  {re.escape(name)}:\n(.*?)(?=^  [a-z][a-z0-9-]*:\n|\Z)",
        source,
        re.MULTILINE | re.DOTALL,
    )
    if match is None:
        raise AssertionError(f"workflow job is absent: {name}")
    return match.group(1)


def step(source: str, name: str) -> str:
    """Select one named step, including its condition and command."""
    match = re.search(
        rf"^      - name: {re.escape(name)}\n"
        r"(.*?)(?=^      - (?:name|uses):|\Z)",
        source,
        re.MULTILINE | re.DOTALL,
    )
    if match is None:
        raise AssertionError(f"workflow step is absent: {name}")
    return match.group(1)


class NativeWorkflowContracts(unittest.TestCase):
    def setUp(self) -> None:
        self.workflow = WORKFLOW.read_text(encoding="utf-8")
        self.native = job(self.workflow, "execute-original-native-bootstrap")

    def test_platform_and_bound_inputs_are_explicit_prerequisites(self) -> None:
        platform = step(self.native, "Require original supported physical ownership platform")
        binder = step(self.native, "Bind the actual component, original modules and native index")
        download = self.native.index("name: whole-native-harness-component")
        self.assertGreater(
            self.native.rfind("uses: actions/download-artifact@v4", 0, download), -1
        )

        self.assertIn("id: native_platform", platform)
        self.assertIn("test \"$(uname -m)\" = arm64", platform)
        self.assertIn("test \"$(uname -r)\" = 24.6.0", platform)
        self.assertIn("id: native_inputs", binder)
        self.assertIn("harness_native_ci_inputs.py", binder)
        self.assertIn("--artifact", binder)
        self.assertIn("component.wasm", binder)
        self.assertIn("--output", binder)
        self.assertLess(self.native.index(platform), download)
        self.assertLess(download, self.native.index(binder))
        self.assertLess(
            self.native.index(binder),
            self.native.index(step(self.native, "Verify actual source metadata operations without general source rights")),
        )

    def test_diagnostics_ignore_prior_test_failures_but_require_both_prerequisites(self) -> None:
        metadata = step(
            self.native,
            "Verify actual source metadata operations without general source rights",
        )
        guard = (
            "${{ !cancelled() && steps.native_platform.conclusion == 'success' "
            "&& steps.native_inputs.conclusion == 'success' }}"
        )
        for name in (
            "Preserve init failures and reject unauthorized inspection and cancelled getters",
            "Run actual original Runtime.init Profile identity and opaque root getters",
            "Preserve the actual native owner when Core adopts it",
        ):
            with self.subTest(step=name):
                source = step(self.native, name)
                condition = re.search(r"(?m)^        if: (.+)$", source)
                self.assertIsNotNone(condition, name)
                self.assertEqual(condition.group(1), guard)
                self.assertNotIn("success()", condition.group(1))
                self.assertLess(self.native.index(metadata), self.native.index(source))

    def test_mandatory_commands_keep_the_real_metadata_policy_init_and_owner_cases(self) -> None:
        commands = (
            (
                "Verify actual source metadata operations without general source rights",
                "--expected-passed 5 --",
                "cargo test --locked -p tobkiri-harness-native --features bootstrap-ci --lib",
                "source_resolve::tests::",
            ),
            (
                "Preserve init failures and reject unauthorized inspection and cancelled getters",
                "--expected-passed 5 --",
                "cargo test --locked -p tobkiri-harness-native --features bootstrap-ci --lib",
                "bootstrap_runtime::policy_tests::",
            ),
            (
                "Run actual original Runtime.init Profile identity and opaque root getters",
                "--expected-passed 1 --",
                "cargo test --locked -p tobkiri-harness-native --features bootstrap-ci --lib",
                "bootstrap_runtime::actual_ci::actual_original_component_initializes_and_returns_original_profile_id",
            ),
            (
                "Preserve the actual native owner when Core adopts it",
                "--expected-passed 2 --",
                "cargo test --locked -p tobkiri-core --lib",
                "core::runtime_owner_tests::",
            ),
        )
        for name, count, cargo, target in commands:
            with self.subTest(step=name):
                source = step(self.native, name)
                for required in (count, cargo, target):
                    self.assertIn(required, source)
                for bypass in ("continue-on-error", "|| true", "--skip", "--no-run", "--list"):
                    self.assertNotIn(bypass, source)

        actual = (ROOT / "tobkiri_launcher/harness-native/src/bootstrap_actual_ci.rs").read_text(
            encoding="utf-8"
        )
        for operation in (
            "Component::new(engine.engine(), &bytes)?",
            "NativeBootstrap::prepare(",
            "runtime.bootstrap_profile_id().await?",
        ):
            self.assertIn(operation, actual)

    def test_actual_consumer_retains_the_shared_limits_without_ci_overrides(self) -> None:
        capacity = (ROOT / "crates/tobkiri-gate/src/native_setup_capacity.rs").read_text(
            encoding="utf-8"
        )
        store = (ROOT / "crates/tobkiri-wasm/src/native_setup_store.rs").read_text(
            encoding="utf-8"
        )
        permissions = (ROOT / "crates/tobkiri-gate/src/permissions.rs").read_text(
            encoding="utf-8"
        )
        actual = (ROOT / "tobkiri_launcher/harness-native/src/bootstrap_actual_ci.rs").read_text(
            encoding="utf-8"
        )

        self.assertIn("NATIVE_SETUP_MEMORY_BYTES: u64 = 64 * 1024 * 1024", capacity)
        self.assertIn("Duration::from_secs(30)", capacity)
        self.assertIn("const FUEL: u64 = 2_000_000_000", store)
        self.assertIn("4 * 1024 * 1024", permissions)
        for observation in (
            "peak <= tobkiri_gate::NATIVE_SETUP_MEMORY_BYTES",
            "fuel_remaining < 2_000_000_000",
            "elapsed < tobkiri_gate::NATIVE_SETUP_WALL",
        ):
            self.assertIn(observation, actual)

        consumer = step(
            self.native,
            "Run actual original Runtime.init Profile identity and opaque root getters",
        )
        for override in (
            "NATIVE_SETUP_MEMORY_BYTES=",
            "NATIVE_SETUP_WALL=",
            "TOBKIRI_TEST_NATIVE_HARNESS_FUEL=",
            "--fuel",
            "--memory",
        ):
            self.assertNotIn(override, consumer)

    def test_preparation_cannot_start_a_fresh_clock_after_compilation(self) -> None:
        actual = (ROOT / "tobkiri_launcher/harness-native/src/bootstrap_actual_ci.rs").read_text(
            encoding="utf-8"
        )
        self.assertEqual(actual.count("owner.begin_native_setup()?"), 1)
        positions = [actual.index(token) for token in (
            "owner.begin_native_setup()?",
            "WasmEngine::new_with_control",
            "File::open(&path)?",
            "let payload = invocation.reserve",
            "bytes.try_reserve_exact(size)?",
            "Component::new(engine.engine(), &bytes)?",
            "drop(bytes);",
            "drop(payload);",
            "original native deadline exhausted during component preparation",
            "NativeBootstrap::prepare(",
        )]
        self.assertEqual(positions, sorted(positions))
        self.assertIn("invocation.deadline()? == deadline", actual)
        self.assertIn("std::time::Instant::now() < deadline", actual)


if __name__ == "__main__":
    unittest.main()
