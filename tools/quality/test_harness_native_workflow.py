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
            "Verify shared component preparation preserves source funding and custody",
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

    def test_runtime_features_come_from_the_same_resolved_and_compiled_consumer(self) -> None:
        source = step(self.native, "Retain resolved and actually compiled runtime features")
        self.assertIn("steps.native_platform.conclusion == 'success'", source)
        self.assertIn("steps.native_inputs.conclusion == 'success'", source)
        self.assertIn("!cancelled()", source)
        self.assertIn("cargo tree --locked --offline -p tobkiri-harness-native", source)
        self.assertIn("--features bootstrap-ci", source)
        self.assertIn("--target aarch64-apple-darwin", source)
        self.assertIn("--edges features --invert wasmtime@47.0.4", source)
        self.assertIn('"$CARGO_TARGET_DIR"/debug/.fingerprint/wasmtime-*/lib-wasmtime.json', source)
        self.assertIn('test -s "${fingerprint%.json}"', source)
        self.assertNotIn("continue-on-error", source)
        self.assertLess(
            self.native.index(step(self.native, "Run actual original Runtime.init Profile identity and opaque root getters")),
            self.native.index(source),
        )
        self.assertLess(
            self.native.index(source),
            self.native.index(step(self.native, "Preserve the actual native owner when Core adopts it")),
        )
        self.assertIn("${{ runner.temp }}/harness-native-runtime-features/", self.native)

    def test_mandatory_commands_keep_the_real_metadata_policy_init_and_owner_cases(self) -> None:
        commands = (
            (
                "Verify shared component preparation preserves source funding and custody",
                "--expected-passed 2 --",
                "cargo test --locked -p tobkiri-harness-native --features bootstrap-ci --lib",
                "preparation::tests::",
            ),
            (
                "Verify actual source metadata operations without general source rights",
                "--expected-passed 5 --",
                "cargo test --locked -p tobkiri-harness-native --features bootstrap-ci --lib",
                "source_resolve::tests::",
            ),
            (
                "Preserve init failures and reject unauthorized inspection and cancelled getters",
                "--expected-passed 6 --",
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
            "NativeComponentPreparation::new(invocation.clone(), source)?",
            "preparation.compile(input, &expected)?",
            "prepared.initialize(&selected::INDEX).await?",
            "runtime.bootstrap_profile_id().await?",
        ):
            self.assertTrue(operation in actual, f"missing real consumer operation: {operation}")

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
            "NativeComponentPreparation::new(invocation.clone(), source)?",
            "File::open(&path)?",
            "preparation.compile(input, &expected)?",
            "original native deadline exhausted during component preparation",
            "prepared.initialize(&selected::INDEX).await?",
        )]
        self.assertEqual(positions, sorted(positions))
        self.assertIn("invocation.deadline()? == deadline", actual)
        self.assertIn("std::time::Instant::now() < deadline", actual)

    def test_actual_adapter_and_ci_use_the_same_source_preparation(self) -> None:
        adapter = (ROOT / "tobkiri_launcher/src-tauri/src/native_harness_bootstrap.rs").read_text()
        actual = (ROOT / "tobkiri_launcher/harness-native/src/bootstrap_actual_ci.rs").read_text()
        shared = (ROOT / "tobkiri_launcher/harness-native/src/preparation.rs").read_text()
        fixtures = '\n#[cfg(all(test, target_os = "macos"))]\n'
        self.assertEqual(shared.count(fixtures), 1)
        shared = shared.split(fixtures, 1)[0]
        self.assertLess(adapter.index("state.begin(&app)?"),
                        adapter.index("NativeComponentPreparation::new("))
        self.assertIn("self.preparation", adapter)
        self.assertIn(".compile(artifact, expected)?", adapter)
        self.assertIn(".initialize(index)", adapter)
        for caller in (adapter, actual):
            self.assertNotIn("Component::new(", caller)
            self.assertNotIn("WasmEngine::new_", caller)
        self.assertNotIn("begin_native_setup(", shared)
        self.assertNotIn("RuntimeOwner::open", shared)
        self.assertNotIn("deserialize", shared)
        compact = re.sub(r"\s+", "", shared)
        for token in (
            "self.invocation.reserve(CapacityKind::VfsBuffer,",
            "self.invocation.control().reserve(ControlKind::Frame,",
            "bytes.try_reserve_exact(size)?",
            "input.read_exact(&mutbytes)?",
            "&Digest::hash(&bytes)==expected",
            "Component::new(self.engine.engine(),&bytes)",
            "drop(bytes);", "drop(payload);", "drop(permit);",
        ):
            self.assertTrue(token in compact, f"shared preparation lost {token}")
        self.assertLess(compact.index(".reserve(CapacityKind::VfsBuffer,"),
                        compact.index("bytes.try_reserve_exact(size)?"))
        self.assertLess(compact.index("Component::new("), compact.index("drop(bytes);"))
        self.assertLess(compact.index("drop(bytes);"), compact.index("drop(payload);"))
        self.assertIn("self.invocation.deadline()? == self.deadline", shared)
        self.assertIn("self.preparation.invocation", shared)
        self.assertIn("self.preparation.source", shared)

    def test_compiler_choice_is_generic_with_speed_preserved_for_existing_callers(self) -> None:
        engine = (ROOT / "crates/tobkiri-wasm/src/engine.rs").read_text()
        shared = (ROOT / "tobkiri_launcher/harness-native/src/preparation.rs").read_text()
        self.assertIn("Self::new_with_control_and_optimization(control, OptLevel::Speed)", engine)
        self.assertIn("config.cranelift_opt_level(optimization);", engine)
        self.assertIn("config.consume_fuel(true);", engine)
        self.assertIn("config.epoch_interruption(true);", engine)
        self.assertIn("config.memory_init_cow(false);", engine)
        self.assertIn("OptLevel::None", shared)
        self.assertLess(shared.index("let deadline = invocation.deadline()?"),
                        shared.index("WasmEngine::new_with_control_and_optimization("))
        self.assertNotIn("cfg(feature", shared)
        self.assertNotIn("std::env", shared)


if __name__ == "__main__":
    unittest.main()
