"""Source contracts for supported runtime execution and portable CI coverage."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import textwrap
import unittest

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github/workflows/issue-1520-rust.yml"


def job(source: str, name: str) -> str:
    """Select one top-level job without requiring a third-party YAML package."""
    match = re.search(
        rf"^  {re.escape(name)}:\n(.*?)(?=^  [a-z][a-z0-9-]*:\n|\Z)",
        source, re.MULTILINE | re.DOTALL,
    )
    if match is None:
        raise AssertionError(f"workflow job is absent: {name}")
    return match.group(1)


def step(source: str, name: str) -> str:
    """Select one named step, including its status condition and command."""
    match = re.search(
        rf"^      - name: {re.escape(name)}\n"
        r"(.*?)(?=^      - (?:name|uses):|\Z)",
        source, re.MULTILINE | re.DOTALL,
    )
    if match is None:
        raise AssertionError(f"workflow step is absent: {name}")
    return match.group(1)


class RuntimeRoutingTests(unittest.TestCase):
    def test_import_audit_diagnostic_is_separate_failure_only_and_original_limits_remain(self) -> None:
        source = (ROOT / '.github/workflows/issue-1520-native-input-stack-import-diagnostics.yml').read_text()
        producer = job(source, 'build-import-diagnostics')
        native = job(source, 'actual-native-import-diagnostics')
        self.assertIn('--native-input-stack-import-diagnostics', producer)
        self.assertIn("python-version: '3.13.16'", producer)
        self.assertIn('native-input-stack-import-diagnostics-build-candidate', producer)
        self.assertIn('generated-bindings/', producer)
        guard = step(native, 'Require the existing supported native ownership platform')
        for constraint in ('= Darwin', '= arm64', '= 24.6.0', 'diskutil info', 'apfs'):
            self.assertIn(constraint, guard)
        run = step(native, 'Run the failure-only native import-audit diagnostic')
        self.assertIn("TOBKIRI_TEST_INPUT_STACK_IMPORT_DIAGNOSTICS: '1'", run)
        self.assertIn('import_audit_diagnostic_preserves_the_original_python_stack_failure', run)
        self.assertIn('--expected-passed 1 --', run)
        self.assertIn('-- --ignored --exact --nocapture --test-threads=1', run)
        for bypass in ('continue-on-error', '--skip', '--exclude', '--no-run', '--list'):
            self.assertNotIn(bypass, run)
        self.assertIn('if: always()', step(native, 'Retain native import-audit failure evidence'))
        harness = (ROOT / 'crates/tobkiri-wasm/src/native_input_stack_probe.rs').read_text()
        self.assertIn('baseline stack proof must not admit diagnostic capabilities', harness)
        self.assertIn('diagnostic-only execution cannot produce an acceptance pass', harness)
        self.assertIn('accepted_as_parity', harness)
        custody = step(job(WORKFLOW.read_text(), 'foundations'),
                       'Private native import diagnostic custody regressions')
        self.assertIn('--expected-passed 3 --', custody)
        self.assertIn('native_setup_store::entry::import_trace::tests', custody)

    def test_synchronous_stack_probe_requires_actual_supported_execution_and_exact_inputs(self) -> None:
        source = (ROOT / '.github/workflows/issue-1520-native-input-stack.yml').read_text()
        producer = job(source, 'build-original-stack')
        native = job(source, 'actual-native-stack')
        self.assertIn('PRODUCER_RUN:', producer)
        self.assertIn("python-version: '3.13.16'", producer)
        self.assertIn('--native-input-stack', producer)
        self.assertIn('harness_input_stack/oracle.py', producer)
        self.assertIn('needs: build-original-stack', native)
        self.assertIn('runs-on: macos-15', native)
        guard = step(native, 'Require the existing supported native ownership platform')
        for constraint in ('= Darwin', '= arm64', '= 24.6.0', 'diskutil info', 'apfs'):
            self.assertIn(constraint, guard)
        execute = step(native, 'Execute original Python stack through actual APFS input and funded IO')
        for required in ('--expected-passed 1 --', 'cargo test --locked -p tobkiri-wasm --lib',
                         'input_stack_probe::synchronous_python_stack_', '--ignored --exact'):
            self.assertIn(required, execute)
        for bypass in ('--skip', '--exclude', '--no-run', '--list', 'continue-on-error'):
            self.assertNotIn(bypass, execute)
        self.assertLess(native.index(guard), native.index(execute))
        self.assertIn('input_stack_native_inputs.py', native)
        self.assertIn('--all-targets --no-deps -- -D warnings', native)
        state = step(native, 'Execute initialization and interleaved native Store isolation')
        for required in ('--expected-passed 1 --', 'cargo test --locked -p tobkiri-wasm --lib',
                         'state_probe::actual_python_exports_keep_init_and_isolate_two_native_stores',
                         '-- --ignored --exact --nocapture --test-threads=1'):
            self.assertIn(required, state)
        for bypass in ('--skip', '--exclude', '--no-run', '--list', 'continue-on-error'):
            self.assertNotIn(bypass, state)
        self.assertIn('native_input_stack_state_probe.rs', source)
        retain = step(native, 'Retain actual native stack evidence without local binary downloads')
        self.assertIn('if: always()', retain)
        self.assertIn('stack-native-evidence-state/', retain)
        self.assertIn('stack-native-state.log*', retain)

    """A portable build or skipped test must not stand in for native evidence."""

    def setUp(self) -> None:
        self.source = WORKFLOW.read_text(encoding="utf-8")
        self.linux = job(self.source, "foundations")
        self.native = job(self.source, "verified-platform-runtime")
        self.producer = job(self.source, "official-python-parity")

    def test_whole_source_package_and_all_native_io_cases_have_real_ci_routes(self) -> None:
        source = step(self.linux, "Original Harness Setup source and DATA projection regressions")
        self.assertIn("test_harness_native_source.py", source)
        self.assertIn("test_harness_native_package.py", source)
        self.assertIn("test_harness_native_value_graph.py", source)
        self.assertIn("test_harness_native_dispatcher.py", source)
        self.assertIn("test_harness_native_wit.py", source)
        io = step(self.linux, "Native DATA blocking IO custody regressions")
        self.assertIn("--expected-passed 16 --", io)
        self.assertIn("cargo test --locked -p tobkiri-wasm --lib", io)
        self.assertIn("child_census::native_data_io::tests", io)
        for bypass in ("continue-on-error", "--skip", "--list", "--no-run"):
            self.assertNotIn(bypass, io)
        fixed = step(self.linux, "Fixed native imports and original invocation custody regressions")
        self.assertIn("--expected-passed 6 --", fixed)
        self.assertIn("native_setup_store::imports::tests", fixed)
        self.assertIn("cargo test --locked -p tobkiri-wasm --lib", fixed)
        for bypass in ("continue-on-error", "--skip", "--list", "--no-run"):
            self.assertNotIn(bypass, fixed)

    def test_native_inspection_owner_executes_both_physical_completion_cases(self) -> None:
        custody = step(
            self.linux, "Native inspection owner physical completion regressions")
        self.assertIn("--expected-passed 2 --", custody)
        self.assertIn("child_census::inspection_custody_tests", custody)
        self.assertIn("cargo test --locked -p tobkiri-wasm --lib", custody)
        for bypass in ("continue-on-error", "--skip", "--list", "--no-run"):
            self.assertNotIn(bypass, custody)

    def test_linux_retains_portable_packages_and_explicit_test_targets(self) -> None:
        packages = step(self.linux, "Portable Rust package tests")
        self.assertEqual(set(re.findall(r"-p ([\w-]+)", packages)), {
            "tobkiri-canon", "tobkiri-wit", "tobkiri-manifest",
            "tobkiri-profile", "tobkiri-flow", "tobkiri-gate", "tobkiri-cli",
            "tobkiri-harness-native",
        })
        wasm = step(self.linux, "Portable Wasm engine integration tests")
        self.assertIn("-p tobkiri-wasm", wasm)
        self.assertEqual(set(re.findall(r"--test (\w+)", wasm)), {
            "component_structure", "input_generated_components", "limits",
            "static_lifts", "typed_values", "warm_fiber",
        })
        journal = step(self.linux, "Portable Core journal tests")
        self.assertIn("-p tobkiri-core --test journal", journal)
        source = step(
            self.linux, "Native acceptance source contracts without runtime claims")
        self.assertIn("-p tobkiri-e2e --test native_acceptance", source)
        for command in (packages, wasm, journal, source):
            self.assertIn("issue_1520_ci.py cargo-test", command)
            self.assertIn("cargo test --locked", command)
            self.assertNotIn("--skip", command)
        self.assertNotIn("cargo test --workspace", self.linux)

    def test_linux_owner_denial_executes_exactly_one_case(self) -> None:
        denial = step(self.linux, "Unsupported Linux owner platform fails closed")
        self.assertIn("--expected-passed 1", denial)
        self.assertIn("-p tobkiri-store --test owner_unavailable", denial)
        self.assertIn(
            "unproven_allocation_owner_platform_is_unavailable -- --exact", denial)
        self.assertNotIn("--ignored", denial)

    def test_decoded_registry_runs_the_pinned_source_consumer_and_retains_evidence(self) -> None:
        comparison = step(self.linux, "Decoded Registry original-source comparison")
        for required in (
            "TOBKIRI_PROVIDER_REGISTRY_ORACLE_RECEIPT:",
            "/evidence/original-python/receipt.json",
            "TOBKIRI_PROVIDER_REGISTRY_ORACLE_SHA256: "
            "4be9b93b9f041b5e57d91b18fa982e88fdd1b997f72f810353be814bfae82bed",
            "TOBKIRI_PROVIDER_REGISTRY_EVIDENCE_DIR:",
            "--expected-passed 49", "--expected-passed 1",
            "cargo test --locked --manifest-path packs/Cargo.toml",
            "oracle_tests::provider_registry_decoded_source_consumer -- --ignored --exact",
            "cargo clippy --locked --manifest-path packs/Cargo.toml",
        ):
            self.assertIn(required, comparison)
        self.assertNotIn("continue-on-error", comparison)
        retention = step(self.linux, "Retain decoded Registry comparison evidence")
        self.assertIn("if: always()", retention)
        self.assertIn("uses: actions/upload-artifact@v4", retention)
        self.assertIn("/provider-registry-native/", retention)

    def test_registry_consumer_pins_match_the_complete_original_receipt(self) -> None:
        package = ROOT / "packs/local.provider-registry-rust"
        raw = (package / "evidence/original-python/receipt.json").read_bytes()
        receipt = json.loads(raw)
        source = (package / "src/oracle_tests.rs").read_text()
        for name, expected in (
            ("PINNED_RECEIPT_SHA256", hashlib.sha256(raw).hexdigest()),
            ("PINNED_SOURCE_MANIFEST_SHA256", receipt["source_manifest_sha256"]),
        ):
            declared = re.search(rf'const {name}: &str =\s*"([0-9a-f]{{64}})";', source)
            self.assertIsNotNone(declared, name)
            self.assertEqual(declared.group(1), expected, name)
        count = re.search(r"const EXPECTED_SOURCE_FILES: usize = ([0-9]+);", source)
        self.assertIsNotNone(count)
        self.assertEqual(int(count.group(1)), len(receipt["source_manifest"]))
        self.assertEqual(int(count.group(1)), receipt["limits"]["source_file_count"])

    def test_registry_only_packager_runs_python_and_actual_worker_tests(self) -> None:
        command = step(self.linux, "Python 3.13 asset packager regression")
        self.assertIn("unittest discover -s tools/python313-producer/packager/tests", command)
        self.assertIn("cargo test --locked --manifest-path "
                      "tools/python313-producer/packager/Cargo.toml", command)
        self.assertNotIn("continue-on-error", command)
        self.assertNotIn("--ignored", command)

    def test_existing_foundation_checks_are_retained(self) -> None:
        for command in (
            "test_issue_1520_gate.py", "test_issue_1520_ci.py",
            "test_issue_1520_workflow.py", "--stage foundation",
            "migration/tests/test_inventory.py", "cargo fmt --all -- --check",
            "cargo build --manifest-path crates/test-guests/Cargo.toml",
            "packs/build.py --target-dir", "cargo clippy --workspace --all-targets",
            "unittest discover -s sdk/python/tests", "verify-source.test.mjs",
            "npm ci --ignore-scripts", "run: npm test",
        ):
            with self.subTest(command=command):
                self.assertIn(command, self.linux)

    def test_native_preflight_is_strict_before_full_workspace(self) -> None:
        self.assertIn("runs-on: macos-15", self.native)
        preflight = step(
            self.native, "Verify the currently supported native proof platform")
        for check in (
            'test "$(uname -m)" = arm64', 'test "$(uname -r)" = 24.6.0',
            'df -P "$RUNNER_TEMP"', 'diskutil info "$device"',
            "Type \\(Bundle\\):[[:space:]]+apfs", 'echo "TMPDIR=$RUNNER_TEMP"',
        ):
            self.assertIn(check, preflight)
        runtime = step(
            self.native, "Execute complete Rust workspace on the supported platform")
        self.assertIn("cargo test --workspace --locked --no-fail-fast", runtime)
        self.assertLess(self.native.index(preflight), self.native.index(runtime))
        for bypass in ("--skip", "--exclude", "--no-run", "--list"):
            self.assertNotIn(bypass, runtime)

    def test_producer_failure_does_not_skip_native_workspace(self) -> None:
        header = self.native.split("    steps:", 1)[0]
        self.assertIn("needs: official-python-parity", header)
        self.assertIn("if: ${{ !cancelled() }}", header)

    def test_component_and_receipt_cross_only_the_same_run_boundary(self) -> None:
        transfer = step(
            self.producer, "Transfer the authenticated component and producer receipt")
        download = step(
            self.native, "Download this run's official Linux Python component")
        self.assertIn("uses: actions/upload-artifact@v4", transfer)
        self.assertIn("uses: actions/download-artifact@v4", download)
        for block in (transfer, download):
            self.assertIn("name: official-python-component", block)
        self.assertIn("if-no-files-found: error", transfer)
        self.assertIn("/shell-policy.wasm", transfer)
        self.assertIn("/shell-policy.build.json", transfer)
        self.assertNotIn("/pack", transfer)
        self.assertNotIn("if:", transfer)
        self.assertIn("needs.official-python-parity.result == 'success'", download)
        for cross_run_selector in ("run-id:", "repository:", "github-token:"):
            self.assertNotIn(cross_run_selector, download)

    def test_native_revalidates_inputs_before_real_parity_replay(self) -> None:
        stage = step(
            self.native,
            "Revalidate and bind the transferred component to this checkout")
        replay = step(
            self.native, "Replay the original 91 cases through actual Rust Core")
        self.assertIn("issue_1520_ci.py stage-python", stage)
        self.assertIn(
            '--artifact "$RUNNER_TEMP/issue-1520-python/shell-policy.wasm"', stage)
        self.assertIn('--destination "$RUNNER_TEMP/issue-1520-python/pack"', stage)
        self.assertIn("steps.download_python.outcome == 'success'", stage)
        self.assertIn(
            "TOBKIRI_TEST_PYTHON_PACK_DIR=$RUNNER_TEMP/issue-1520-python/pack", stage)
        self.assertLess(self.native.index(stage), self.native.index(replay))
        self.assertIn("--expected-passed 1 --python-parity", replay)
        self.assertIn("cargo test --locked -p tobkiri-e2e --test parity", replay)
        self.assertIn(
            "shell_policy_frozen_inputs_replay_through_the_real_rust_host", replay)
        self.assertIn("-- --ignored --exact --nocapture --test-threads=1", replay)
        self.assertNotIn("cargo test", self.producer)
        self.assertNotIn("--python-parity", self.producer)

    def test_custom_ordered_fixture_has_separate_authenticated_native_replay(self) -> None:
        portable = step(self.linux, "Preserved ordered Python component transport regression")
        self.assertIn("test_stage_preserved_ordered_python.py", portable)
        stage = step(self.native, "Authenticate the preserved custom Python 3.13 component")
        replay = step(self.native, "Replay the ordered Python candidate through actual Rust Core")
        self.assertIn("scripts/wasm/stage_preserved_ordered_python.py", stage)
        self.assertIn("materialization.json", stage)
        self.assertIn("TOBKIRI_TEST_ORDERED_PYTHON_", stage)
        self.assertLess(self.native.index(stage), self.native.index(replay))
        self.assertIn("--expected-passed 1 --", replay)
        self.assertIn("--test ordered_python", replay)
        self.assertIn("ordered_python_bridge_replays_through_real_core", replay)
        self.assertIn("-- --ignored --exact --nocapture --test-threads=1", replay)
        self.assertNotIn("--python-parity", replay)
        condition = re.search(r"^        if: (.+)$", replay, re.MULTILINE).group(1)
        for prerequisite in ("native_platform", "rust_toolchain", "stage_ordered_python"):
            self.assertIn(f"steps.{prerequisite}.outcome == 'success'", condition)
        self.assertIn("!cancelled()", condition)
        self.assertNotIn("success()", condition)
        self.assertNotIn("stage_python.", condition)
        retain = step(self.native, "Retain actual ordered Python replay evidence")
        self.assertIn("if: always()", retain)
        for name in ("component.build.json", "materialization.json", "pack/pack.json",
                     "native-replay.json", "ordered-python-parity.log",
                     "ordered-python-parity.receipt.json"):
            self.assertIn(name, retain)

    def test_execution_profile_observation_requires_one_actual_native_case(self) -> None:
        replay = step(self.native, "Observe execution Profile through actual Core")
        for required in (
            "issue_1520_ci.py cargo-test", "--expected-passed 1 --",
            "cargo test --locked -p tobkiri-e2e --test execution_profile",
            "current_profile_uses_two_actual_sessions_and_inter_pack_destination_frames",
            "-- --exact",
        ):
            self.assertIn(required, replay)
        self.assertNotIn("--ignored", replay)
        condition = re.search(r"^        if: (.+)$", replay, re.MULTILINE).group(1)
        self.assertIn("!cancelled()", condition)
        for prerequisite in ("native_platform", "rust_toolchain"):
            self.assertIn(f"steps.{prerequisite}.outcome == 'success'", condition)
        self.assertNotIn("workspace", condition)
        retain = step(self.native, "Retain actual runtime test output")
        self.assertIn("if: always()", retain)
        for name in ("execution-profile.log", "execution-profile.receipt.json"):
            self.assertIn(name, retain)

    def test_flow_bridge_observations_keep_original_cases_and_workspace_check(self) -> None:
        cases = (
            ("Core", "core", "provider_proxy", "core-flow-bridge",
             "guest_run_flow_uses_actual_core_bridge_and_manifest_flow_typed_port"),
            ("Wasm", "wasm", "port_proxy", "wasm-flow-bridge",
             "guest_run_flow_uses_weak_bridge_and_real_flow_engine_typed_port"),
            ("failed", "wasm", "port_proxy", "failed-flow-bridge",
             "flow_step_failure_keeps_original_unavailable_port_outcome"),
        )
        retain = step(self.native, "Retain actual runtime test output")
        self.assertIn("if: always()", retain)
        for label, package, target, log, case in cases:
            with self.subTest(case=case):
                replay = step(self.native, f"Observe original {label} Flow bridge in isolation")
                for required in (
                    "issue_1520_ci.py cargo-test", "--expected-passed 1 --",
                    f"cargo test --locked -p tobkiri-{package} --test {target}",
                    case, "-- --exact --nocapture --test-threads=1",
                    f'--log "$RUNNER_TEMP/{log}.log"',
                ):
                    self.assertIn(required, replay)
                self.assertNotIn("--ignored", replay)
                self.assertIn("!cancelled()", replay)
                for prerequisite in ("native_platform", "rust_toolchain"):
                    self.assertIn(f"steps.{prerequisite}.outcome == 'success'", replay)
                self.assertNotIn("workspace", replay)
                for name in (f"{log}.log", f"{log}.receipt.json"):
                    self.assertIn(name, retain)
        workspace = step(self.native, "Execute complete Rust workspace on the supported platform")
        self.assertIn("cargo test --workspace --locked --no-fail-fast", workspace)
        self.assertNotIn("--test-threads", workspace)

    def test_audit_round_trip_observation_preserves_the_original_case_and_limits(self) -> None:
        replay = step(self.native, "Observe original Audit round trip in isolation")
        for required in (
            "issue_1520_ci.py cargo-test", "--expected-passed 1 --",
            "cargo test --locked -p tobkiri-core --lib",
            "core::audit_guest_tests::verified_guest_audit_round_trip_has_original_nested_ack_and_provenance",
            "-- --exact --nocapture --test-threads=1",
            'TOBKIRI_TEST_AUDIT_PHASE_TIMING: "1"',
        ):
            self.assertIn(required, replay)
        self.assertIn("!cancelled()", replay)
        for prerequisite in ("native_platform", "rust_toolchain"):
            self.assertIn(f"steps.{prerequisite}.outcome == 'success'", replay)
        self.assertNotIn("--ignored", replay)
        self.assertNotIn("success()", replay)
        workspace = step(self.native, "Execute complete Rust workspace on the supported platform")
        self.assertIn('TOBKIRI_TEST_AUDIT_PHASE_TIMING: "1"', workspace)
        self.assertIn("cargo test --workspace --locked --no-fail-fast", workspace)
        self.assertNotIn("--test-threads", workspace)
        retain = step(self.native, "Retain actual runtime test output")
        for name in ("audit-round-trip.log", "audit-round-trip.receipt.json"):
            self.assertIn(name, retain)

    def test_replay_survives_other_failures_but_requires_prerequisites(self) -> None:
        replay = step(
            self.native, "Replay the original 91 cases through actual Rust Core")
        condition = re.search(r"^        if: (.+)$", replay, re.MULTILINE).group(1)
        self.assertIn("!cancelled()", condition)
        for prerequisite in ("native_platform", "rust_toolchain", "stage_python"):
            self.assertIn(f"steps.{prerequisite}.outcome == 'success'", condition)
        self.assertNotIn("success()", condition)
        self.assertNotIn("workspace", condition)
        self.assertNotIn("lint", condition)
        self.assertNotIn("continue-on-error", self.source)
        self.assertNotIn("|| true", self.source)


class FlowDependencyTests(unittest.TestCase):
    """Execute the exact workflow detector, including the missing-input case."""

    def detector(self) -> str:
        """Return the inline Python check from the workflow's Pure Flow step."""
        source = WORKFLOW.read_text(encoding="utf-8")
        check = step(job(source, "foundations"), "Pure Flow dependency graph")
        self.assertIn(
            "cargo tree --locked -p tobkiri-flow > flow-dependencies.txt", check)
        self.assertNotIn("if rg", check)
        match = re.search(
            r"python3 - <<'PYTHON'\n(.*?)^          PYTHON$",
            check, re.MULTILINE | re.DOTALL,
        )
        self.assertIsNotNone(match)
        return textwrap.dedent(match.group(1))

    def run_detector(
        self, dependencies: str | None,
    ) -> subprocess.CompletedProcess[str]:
        """Run without Cargo or ripgrep; an absent input must remain an error."""
        with tempfile.TemporaryDirectory() as directory:
            if dependencies is not None:
                (Path(directory) / "flow-dependencies.txt").write_text(
                    dependencies, encoding="utf-8")
            return subprocess.run(
                [sys.executable, "-c", self.detector()], cwd=directory,
                capture_output=True, text=True, check=False,
            )

    def test_allowed_dependencies_pass_without_ripgrep(self) -> None:
        result = self.run_detector("tobkiri-flow v0.1.0\n└── tobkiri-gate v0.1.0\n")
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_each_forbidden_dependency_fails(self) -> None:
        for dependency in ("wasmtime", "rusqlite", "reqwest"):
            with self.subTest(dependency=dependency):
                result = self.run_detector(
                    f"tobkiri-flow v0.1.0\n└── {dependency} v1.0\n")
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("forbidden runtime dependency", result.stderr)

    def test_missing_dependency_file_is_not_a_clean_graph(self) -> None:
        result = self.run_detector(None)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("FileNotFoundError", result.stderr)


if __name__ == "__main__":
    unittest.main()
