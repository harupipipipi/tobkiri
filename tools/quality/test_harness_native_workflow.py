"""Source contracts for the gated whole-component native CI workflow."""
from __future__ import annotations

from pathlib import Path
import re
import unittest

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github/workflows/issue-1520-harness-native.yml"
INSTALLERS = ROOT / ".github/workflows/desktop-installers.yml"


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

    def test_schema_diagnostic_is_an_explicit_separate_producer_and_binder(self) -> None:
        self.assertRegex(self.workflow, r"(?m)^        options: \[bootstrap,.*catalog-schema-diagnostic,")
        build = job(self.workflow, "build-whole-native-harness")
        producer = step(build, "Build the actual native Harness dispatcher and original module closure")
        self.assertIn("build_components.py --harness-native", producer)
        self.assertIn("inputs.verification == 'catalog-schema-diagnostic'", producer)
        self.assertIn("&& '--schema-call-diagnostic' || ''", producer)
        binder = step(self.native, "Bind the actual component, original modules and native index")
        self.assertIn("--schema-call-diagnostic", binder)
        self.assertIn('elif [ "$SCHEMA_DIAGNOSTIC" = true ]; then', binder)
        for component in ("harness-native", "harness-native-schema-diagnostic"):
            self.assertIn(f'harness-built/harness-native/{component}/work/component.wasm', binder)
            for path in ("inputs/plan.json", "inputs/provenance.json", "work/component.wasm",
                         "inputs/python/harness_native_dependency_data.py"):
                self.assertIn(f'harness-native/{component}/{path}', build)

    def test_app_backend_runs_the_whole_harness_catalog_and_recovery(self) -> None:
        build = job(self.workflow, "build-whole-native-harness")
        extension = step(build, "Build the source-bound App schema extension offline")
        self.assertIn("build_schema_kernel_extension.py --app-backend", extension)
        self.assertIn("inputs.verification == 'catalog-app-frame'", extension)
        binder = step(self.native, "Bind the actual component, original modules and native index")
        self.assertIn("--app-backend", binder)
        self.assertIn("harness-native-app-frame/work/component.wasm", binder)
        self.assertIn("harness-native-app-frame/inputs/python/harness_native_dependency_data.py", build)
        for name in ("Execute the actual same-guard catalog to constructor entry",
                     "Observe original Profile replacement and Authority initialization",
                     "Independently compare actual catalog reachpoint and effects"):
            self.assertIn("inputs.verification == 'catalog-app-frame'", step(self.native, name))
        launcher = step(self.native, "Stage the verified same-run Wasm through the original Launcher")
        self.assertIn("inputs.verification == 'launcher-staging'", launcher)
        self.assertNotIn("catalog-app-frame", launcher)

    def test_launcher_snapshot_stages_the_attested_app_component(self) -> None:
        mode = "inputs.verification == 'launcher-snapshot-staging'"
        self.assertRegex(self.workflow, r"(?m)^        options: \[bootstrap,.*launcher-snapshot-staging\]")
        self.assertIn("default: bootstrap", self.workflow)
        self.assertIn(mode + " && '-launcher-snapshot' || ''", self.workflow)
        build = job(self.workflow, "build-whole-native-harness")
        for name in (
            "Restore pinned cross compiler and locked App schema kernel archives",
            "Build the source-bound App schema extension offline",
            "Build the actual native Harness dispatcher and original module closure",
        ):
            self.assertIn(mode, step(build, name))
        binder = step(self.native, "Bind the actual component, original modules and native index")
        self.assertIn(mode, binder)
        self.assertIn("harness-native-app-frame/work/component.wasm", binder)
        stage = step(self.native, "Stage the verified same-run Wasm through the original Launcher")
        for required in (
            mode, "features=native-first-setup", "--features \"$features\"",
            'TOBKIRI_SETUP_COMPONENT_SOURCE="$TOBKIRI_TEST_NATIVE_HARNESS_COMPONENT"',
            'TOBKIRI_SETUP_COMPONENT_RECEIPT="$bound/launcher-component.json"',
            'TOBKIRI_SETUP_COMPONENT_RECEIPT_SHA256="$pin"',
            "selected_setup_component::staged_snapshot_receipt_selects_native_setup",
            "-- --ignored --exact", "verify_runtime_resource_manifest", "fingerprint(copied",
        ):
            self.assertIn(required, stage)
        self.assertNotIn("TOBKIRI_TEST_NATIVE_OWNED_CATALOG", stage)
        self.assertNotIn(mode, step(self.native, "Observe actual catalog schema checkpoints without qualification"))
        evidence = step(self.native, "Retain actual native execution evidence")
        self.assertIn("harness-native-launcher-snapshot.log*", evidence)

    def test_snapshot_dmg_opt_in_carries_the_verified_same_run_receipt_to_the_mount(self) -> None:
        self.assertRegex(self.workflow, r"package_launcher_dmg:\n[^\n]*\n        type: boolean\n        default: false")
        staging = step(self.native, "Stage the verified same-run Wasm through the original Launcher")
        self.assertIn("steps.launcher_staging.outputs.receipt_sha256", self.native)
        self.assertLess(staging.index("fingerprint(copied"), staging.index('>> "$GITHUB_OUTPUT"'))
        package = job(self.workflow, "package-native-setup-dmg")
        for required in (
            "inputs.verification == 'launcher-snapshot-staging' && inputs.package_launcher_dmg",
            "needs: execute-original-native-bootstrap", "uses: ./.github/workflows/desktop-installers.yml",
            "native_setup: true", "needs.execute-original-native-bootstrap.outputs.launcher_receipt_sha256",
        ):
            self.assertIn(required, package)

        installer = job(INSTALLERS.read_text(encoding="utf-8"), "build-installer")
        artifact = step(installer, "Download the same-run Harness component for the CI installer")
        self.assertIn("if: inputs.native_setup", artifact)
        self.assertIn("name: whole-native-harness-component", artifact)
        self.assertNotIn("run-id:", artifact)
        bind = step(installer, "Bind the installer to the verified Launcher staging receipt")
        self.assertIn("harness_native_ci_inputs.py", bind)
        self.assertIn("--app-backend", bind)
        self.assertIn('report["launcher_component_receipt"]["sha256"] !=', bind)
        self.assertIn("--github-env \"$RUNNER_TEMP/harness-native-test.env\"", bind)
        app = step(installer, "Build non-publishable macOS CI/E2E application")
        self.assertIn("--features native-first-setup", app)
        self.assertIn("TOBKIRI_SETUP_COMPONENT_RECEIPT_SHA256", app)
        self.assertIn("--bundles app --ci", app)
        shell = step(installer, "Build verified Tauri Shell artifact")
        self.assertNotIn("TOBKIRI_SETUP_COMPONENT", shell)
        self.assertNotIn("native-first-setup", shell)
        mounted = step(installer, "Verify mounted macOS installer resources")
        self.assertIn("verify_packaged_python_dmg.sh", mounted)
        self.assertIn("--expected-native-setup-component-sha256", mounted)
        self.assertIn("--expected-native-setup-component-bytes", mounted)
        self.assertIn('>> "$GITHUB_OUTPUT"', mounted)
        upload = step(installer, "Upload desktop installer")
        self.assertIn("non-publishable-ci-e2e", upload)
        self.assertIn("steps.verified_dmg.outputs.path", upload)

    def test_app_diagnostic_uses_same_extension_and_only_the_actual_catalog_trace(self) -> None:
        mode = "catalog-app-frame-diagnostic"
        condition = f"inputs.verification == '{mode}'"
        component = "harness-native-app-frame-diagnostic"
        self.assertRegex(self.workflow, rf"(?m)^        options: \[bootstrap,.*\b{mode},")
        self.assertIn("default: bootstrap", self.workflow)
        build = job(self.workflow, "build-whole-native-harness")
        extension = step(build, "Build the source-bound App schema extension offline")
        self.assertIn(condition, extension)
        self.assertNotIn("--schema-phases", extension)
        producer = step(build, "Build the actual native Harness dispatcher and original module closure")
        self.assertEqual(producer.count(condition), 2)
        self.assertIn("--harness-app-schema-backend --kernel-extension", producer)
        self.assertIn("&& '--schema-call-diagnostic' || ''", producer)
        binder = step(self.native, "Bind the actual component, original modules and native index")
        combined = binder.split('elif [ "$SCHEMA_DIAGNOSTIC" = true ]; then', 1)[0]
        self.assertIn(condition, combined)
        self.assertIn("--schema-call-diagnostic --app-backend", combined)
        self.assertIn(f"{component}/work/component.wasm", combined)
        for name in (
            "Prepare independent original catalog resource fixtures",
            "Observe actual catalog schema checkpoints without qualification",
            "Retain resolved and actually compiled runtime features",
        ):
            self.assertIn(condition, step(self.native, name))
        for name in (
            "Execute the actual same-guard catalog to constructor entry",
            "Observe original Profile replacement and Authority initialization",
            "Independently compare actual catalog reachpoint and effects",
            "Prepare fresh independent original nine-case catalog fixtures",
            "Stage the verified same-run Wasm through the original Launcher",
        ):
            self.assertNotIn(condition, step(self.native, name))
        for path in (
            "inputs/plan.json", "inputs/provenance.json",
            "inputs/python/harness-native-dependencies.json",
            "inputs/python/harness_native_dependency_data.py",
            "work/component.build.json", "work/component.wasm",
        ):
            self.assertIn(f"harness-native/{component}/{path}", build)

    def test_schema_diagnostic_keeps_fresh_originals_and_skips_baseline_acceptance(self) -> None:
        for name in (
            "Checkout pinned original factory and identity references",
            "Install original reference dependencies once",
            "Prepare independent original catalog resource fixtures",
        ):
            source = step(self.native, name)
            self.assertIn("inputs.verification == 'catalog-schema-diagnostic'", source)
            self.assertIn("!cancelled()", source)
        for name in (
            "Execute the actual same-guard catalog to constructor entry",
            "Independently compare actual catalog reachpoint and effects",
            "Prepare fresh independent original nine-case catalog fixtures",
        ):
            source = step(self.native, name)
            condition = re.search(r"(?m)^        if: (.+)$", source).group(1)
            self.assertIn("inputs.verification == 'catalog-prefix'", condition)
            self.assertNotIn("catalog-schema-diagnostic", condition)
        self.assertIn("bootstrap_runtime::catalog_parity_actual_ci::original_catalog_nine_driver", self.native)

    def test_schema_diagnostic_commands_fail_closed_and_preserve_evidence(self) -> None:
        commands = (
            ("Verify bounded Core fixed-import schema checkpoints", 6,
             "cargo test --locked -p tobkiri-wasm --lib --features bootstrap-ci",
             "native_fixed_import_ -- --nocapture"),
            ("Verify bounded Host schema checkpoint state", 7,
             "cargo test --locked -p tobkiri-harness-native --lib --features catalog-diagnostics-ci",
             "ci_schema_checkpoint_ -- --nocapture"),
            ("Observe actual catalog schema checkpoints without qualification", 1,
             "cargo test --locked -p tobkiri-harness-native --lib --features catalog-diagnostics-ci",
             "bootstrap_runtime::catalog_actual_ci::original_catalog_schema_diagnostic_driver"),
        )
        for name, count, cargo, target in commands:
            with self.subTest(step=name):
                source = step(self.native, name)
                for required in (
                    "!cancelled()", "inputs.verification == 'catalog-schema-diagnostic'",
                    "steps.native_platform.conclusion == 'success'",
                    "steps.native_inputs.conclusion == 'success'",
                    f"--expected-passed {count} --", cargo, target,
                ):
                    self.assertIn(required, source)
                for bypass in ("continue-on-error", "|| true", "--skip", "--no-run", "--list"):
                    self.assertNotIn(bypass, source)
        driver = step(self.native, commands[-1][0])
        self.assertIn("steps.catalog_prefix_reference.conclusion == 'success'", driver)
        self.assertIn("-- --ignored --exact --test-threads=1 --nocapture", driver)
        features = step(self.native, "Retain resolved and actually compiled runtime features")
        self.assertIn("--features catalog-diagnostics-ci", features)
        self.assertIn("schema-diagnostic-source-provenance.json", features)
        evidence = step(self.native, "Retain actual native execution evidence")
        self.assertIn("if: always()", evidence)
        for path in (
            "original-catalog-prefix/actual-catalog-observations.json",
            "original-catalog-prefix/actual-catalog-schema-trace.json",
            "harness-native-schema-fixed-import.log*", "harness-native-schema-checkpoint.log*",
            "harness-native-catalog-schema-diagnostic.log*", "harness-native-runtime-features/",
        ):
            self.assertIn(path, evidence)

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

    def test_original_launcher_staging_uses_only_the_bound_normal_artifact(self) -> None:
        source = step(
            self.native, "Stage the verified same-run Wasm through the original Launcher"
        )
        for required in (
            "inputs.verification == 'launcher-staging'",
            "steps.native_inputs.conclusion == 'success'",
            '["launcher_component_receipt"]["sha256"]',
            'TOBKIRI_SETUP_COMPONENT_SOURCE="$TOBKIRI_TEST_NATIVE_HARNESS_COMPONENT"',
            'TOBKIRI_SETUP_COMPONENT_RECEIPT="$bound/launcher-component.json"',
            'TOBKIRI_SETUP_COMPONENT_RECEIPT_SHA256="$pin"',
            "features=native-harness-bootstrap,native-setup-review",
            '--features "$features"',
            "verify_runtime_resource_manifest(stage)",
            'name = "native-setup/component.wasm"',
            "fingerprint(copied, 32 * 1024**2) != expected",
        ):
            self.assertIn(required, source)
        for unrelated in ("catalog-schema-diagnostic", "--features core-host", "continue-on-error"):
            self.assertNotIn(unrelated, source)
        evidence = step(self.native, "Retain actual native execution evidence")
        self.assertIn("harness-native-launcher-staging.log*", evidence)

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
                "--expected-passed 6 --",
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
            ".initialize_with_profile_schemas(&selected::INDEX, Some(&selected::PROFILE_SCHEMAS))",
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
            ".initialize_with_profile_schemas(&selected::INDEX, Some(&selected::PROFILE_SCHEMAS))",
        )]
        self.assertEqual(positions, sorted(positions))
        self.assertIn("invocation.deadline()? == deadline", actual)
        self.assertIn("std::time::Instant::now() < deadline", actual)

    def test_actual_adapter_and_ci_use_the_same_source_preparation(self) -> None:
        adapter = (ROOT / "tobkiri_launcher/src-tauri/src/native_harness_bootstrap.rs").read_text()
        actual = (ROOT / "tobkiri_launcher/harness-native/src/bootstrap_actual_ci.rs").read_text()
        shared = (ROOT / "tobkiri_launcher/harness-native/src/preparation.rs").read_text()
        metadata = (ROOT / "tobkiri_launcher/harness-native/src/bootstrap_metadata.rs").read_text()
        fixtures = '\n#[cfg(all(test, target_os = "macos"))]\n'
        self.assertEqual(shared.count(fixtures), 1)
        shared = shared.split(fixtures, 1)[0]
        # The finite worker now owns admission, cancellation and compilation;
        # the earlier adapter-local preparation carrier was removed. Check
        # the real current job sequence rather than the deleted spelling.
        current = adapter.split("async fn run_original_job(", 1)[1]
        current = re.sub(r"\s+", "", current)
        positions = [current.index(token) for token in (
            "runtime.begin(&app,&job)?",
            "job.attach_invocation(&invocation)?",
            "OriginalBootstrapInputs::new(app,job.source_custody()?,&invocation,Some(launch))?",
            "source.check_live()?",
            "NativeComponentPreparation::new_with_cancellation_parent(",
            ".compile(artifact,&expected)?",
            "letinitialize=prepared.initialize_with_setup_catalog(index,profile_schemas,catalog_delivery);",
            "caught(initialize).await",
        )]
        self.assertEqual(positions, sorted(positions))
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
            "SetupCatalog::bind(&self.component_digest,profile_schemas,delivery)",
            "Component::new(self.engine.engine(),&bytes)",
            "drop(bytes);", "drop(payload);", "drop(permit);",
        ):
            self.assertTrue(token in compact, f"shared preparation lost {token}")
        self.assertLess(compact.index(".reserve(CapacityKind::VfsBuffer,"),
                        compact.index("bytes.try_reserve_exact(size)?"))
        self.assertLess(compact.index("Component::new("), compact.index("drop(bytes);"))
        self.assertLess(compact.index("drop(bytes);"), compact.index("drop(payload);"))
        self.assertIn("schemas.bind(component)", metadata)
        self.assertLess(compact.index("SetupCatalog::bind(&self.component_digest,profile_schemas,delivery)"),
                        compact.index("NativeBootstrap::prepare_with_cancellation_parent("))
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
