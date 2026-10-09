import ast
from pathlib import Path
import re

import pytest


ROOT = Path(__file__).resolve().parent.parent
PACKAGE_TEST = "tobkiri_launcher/scripts/tests/test_package_presentation_artifact.py"
LOCKED_INSTALLER = ".github/scripts/install_locked_python_test_dependencies.py"
LOCKED_EXPORTS = (
    "tobkiri_runtime/requirements.txt",
    "tobkiri_runtime/requirements-dev.txt",
)
FORMAL_PACKAGING_PRODUCER = "run_formal_defaults_packaging"


def _job_blocks(workflow: str) -> dict[str, str]:
    """Return top-level GitHub Actions job bodies without requiring PyYAML."""
    matches = re.finditer(
        r"^  (?P<name>[A-Za-z0-9_-]+):\n(?P<body>.*?)(?=^  [A-Za-z0-9_-]+:|\Z)",
        workflow,
        re.MULTILINE | re.DOTALL,
    )
    return {match.group("name"): match.group("body") for match in matches}


def _invokes_package_test(job: str) -> bool:
    """Identify direct and Rust build-script invocations of the package test."""
    direct = PACKAGE_TEST in job
    rust_launcher_tests = (
        "working-directory: tobkiri_launcher/src-tauri" in job
        and re.search(r"(?m)^\s+run:\s+cargo test(?:\s|$)", job) is not None
    )
    return direct or rust_launcher_tests


def test_launcher_route_scan_targets_the_current_ci_build() -> None:
    """Do not scan the checked-in panel after building into a temporary directory."""
    workflow = (ROOT / ".github/workflows/test.yml").read_text(encoding="utf-8")
    job = _job_blocks(workflow)["pack-architecture"]
    output = "${{ runner.temp }}/tobkiri-panel-build"
    assert f"TOBKIRI_PANEL_BUILD_DIR: {output}" in job
    assert f'--panel-root "{output}"' in job


def test_full_root_suite_installs_locked_runtime_dependencies() -> None:
    """The full suite imports real provider adapters and their Host dependencies."""
    workflow = (ROOT / ".github/workflows/test.yml").read_text(encoding="utf-8")
    job = _job_blocks(workflow)["root-python-tests"]
    install = job.split("- name: Install dependencies", 1)[1].split(
        "- name: Test compact runner directly", 1
    )[0]
    assert 'if [ "${{ matrix.python-version }}" = "3.11" ]; then' in install
    assert f"python {LOCKED_INSTALLER}" in install
    assert "-- pytest tests/ -v" in job


def _capture_test_functions(source: str) -> set[str]:
    """Discover function selectors and reject unsupported class-based coverage."""
    tree = ast.parse(source)
    classes = sorted(
        node.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ClassDef) and node.name.startswith("Test")
    )
    assert not classes, (
        "Capture shards support function selectors only; allocate class cases "
        f"explicitly before adding test classes: {classes}"
    )
    return {
        node.name
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name.startswith("test_")
    }


def test_capture_partition_discovery_rejects_unallocated_test_classes() -> None:
    """A new test class must never silently disappear from the capture matrix."""
    with pytest.raises(AssertionError, match="allocate class cases explicitly"):
        _capture_test_functions(
            "def test_existing(): pass\n"
            "class TestAdditionalCapture:\n"
            "    def test_new_case(self): pass\n"
        )


def test_capture_partition_discovery_includes_sync_and_async_functions() -> None:
    assert _capture_test_functions(
        "def test_sync(): pass\n"
        "async def test_async(): pass\n"
        "def helper(): pass\n"
    ) == {"test_sync", "test_async"}


def test_package_shards_preserve_coverage_and_timeout_evidence() -> None:
    """Split independent suites without dropping tests or expanding job limits."""
    workflow = (ROOT / ".github/workflows/test.yml").read_text(encoding="utf-8")
    job = _job_blocks(workflow)["tobkiri-package-pytest"]
    assert 'python-version: ["3.10", "3.11", "3.13"]' in job
    assert "shard: [capture-lifecycle, capture-guards, migration-providers]" in job
    assert "fail-fast: false" in job
    assert "timeout-minutes: 15" in job
    lifecycle = job.split("capture-lifecycle)", 1)[1].split(";;", 1)[0]
    guards = job.split("capture-guards)", 1)[1].split(";;", 1)[0]
    selector_pattern = r"tests/test_pack_control_v4\.py::(test_[a-z0-9_]+)"
    lifecycle_names = re.findall(selector_pattern, lifecycle)
    guard_names = re.findall(selector_pattern, guards)
    allocated = lifecycle_names + guard_names
    source = ROOT / "tobkiri_runtime/tests/test_pack_control_v4.py"
    discovered = _capture_test_functions(source.read_text(encoding="utf-8"))
    assert lifecycle_names and guard_names
    assert len(allocated) == len(set(allocated)), "Capture cases must run once per Python"
    assert set(allocated) == discovered, (
        "Allocate every capture test explicitly; missing="
        f"{sorted(discovered - set(allocated))}, stale="
        f"{sorted(set(allocated) - discovered)}"
    )
    # Function selectors deliberately include every parametrized case.
    for shard, selectors in ((lifecycle, lifecycle_names), (guards, guard_names)):
        assert shard.count("tests/test_pack_control_v4.py") == len(selectors)
        assert "[" not in shard and "-k" not in shard and "-m " not in shard
    second = job.split("migration-providers)", 1)[1].split(";;", 1)[0]
    common_second, extra = second.split('if [ "${{ matrix.python-version }}" = "3.11" ]; then', 1)
    def names(value: str) -> list[str]:
        return re.findall(r"tests/(test_[a-z0-9_]+\.py)", value)

    common = ["test_pack_control_v4.py"] + names(common_second)
    assert len(common) == len(set(common)) == 12
    assert set(common) == {
        "test_external_profile_catalog.py", "test_external_dispatch_cold_capture.py",
        "test_host_policy_lock.py", "test_native_pack_onboarding.py",
        "test_runtime_version_resolution.py", "test_pack_control_v4.py",
        "test_complete_v4_migration_gate.py", "test_defaultspack_extension_registry.py",
        "test_defaultspack_provider_foundation.py", "test_defaultspack_provider_program.py",
        "test_defaultspack_opencode_zen_provider.py", "test_defaultspack_agent_run_store.py",
    }
    assert names(extra) == ["test_agent_engine_tools.py", "test_company_contract_facade.py"]
    assert '-- pytest "${tests[@]}" -v --durations=25' in job
    assert "--timeout-seconds 720" in job
    assert "Unknown package test shard" in job
    assert "always() && steps.package_pytest.outcome != 'skipped'" in job
    assert "continue-on-error" not in job
    assert "-py${{ matrix.python-version }}-${{ matrix.shard }}.log" in job
    assert "name: package-pytest-${{ matrix.python-version }}-${{ matrix.shard }}-" in job


def test_workflow_private_key_regressions_run_on_windows() -> None:
    """Native ACL checks must not be validated solely by Linux skip results."""
    workflow = (ROOT / ".github/workflows/test.yml").read_text(encoding="utf-8")
    job = _job_blocks(workflow)["task-d-cross-platform-smoke"]
    assert "os: [ubuntu-latest, windows-latest]" in job
    invocation = job.split("- name: Run Task D smoke tests", 1)[1]
    for name in ("test_workflow_attempt_key_privacy.py", "test_windows_private_file.py"):
        assert f"tests/{name}" in invocation
        assert (ROOT / "tobkiri_runtime/tests" / name).is_file()


def test_recovery_regressions_run_without_contract_marker_filtering() -> None:
    """Keep the Profile/Host recovery suite explicit and preserve failure logs."""
    workflow = (ROOT / ".github/workflows/test.yml").read_text(encoding="utf-8")
    job = _job_blocks(workflow)["tobkiri-contract-checks"]
    recovery = job.split("- name: Run Profile and Host recovery regressions", 1)[1]
    invocation, remaining = recovery.split("- name: Upload recovery pytest log", 1)
    assert "-- pytest -v" in invocation
    assert "-m contract" not in invocation
    for name in (
        "test_tobkiri_host_resources_admission.py",
        "test_tobkiri_host_execution_integration.py",
        "test_production_v4_host_extension_admission.py",
        "test_profile_source_reconfirmation.py",
        "test_setup_handlers.py",
        "test_profile_architecture_review_c.py",
    ):
        assert f"tests/{name}" in invocation
        assert (ROOT / "tobkiri_runtime/tests" / name).is_file()
    assert "steps.recovery_pytest.outcome == 'failure'" in remaining
    assert "-- pytest -m contract -v" in remaining
    contract_step = remaining.split("- name: Run active contract cluster pytest", 1)[1]
    contract_step = contract_step.split("- name: Upload contract pytest log", 1)[0]
    assert (
        "if: ${{ !cancelled() && (success() || "
        "steps.recovery_pytest.outcome == 'failure') }}"
    ) in contract_step
    assert "continue-on-error" not in job


def test_locked_python_test_installer_uses_both_project_exports() -> None:
    installer = (ROOT / LOCKED_INSTALLER).read_text(encoding="utf-8")
    for export in LOCKED_EXPORTS:
        assert export in installer
    assert "sys.executable" in installer
    assert "pip" in installer

    runtime_requirements = (ROOT / LOCKED_EXPORTS[0]).read_text(encoding="utf-8")
    dev_requirements = (ROOT / LOCKED_EXPORTS[1]).read_text(encoding="utf-8")
    assert "jsonschema==4.26.0" in runtime_requirements
    assert "pytest==9.1.1" in dev_requirements
    build_script = (
        ROOT / "tobkiri_launcher" / "src-tauri" / "build.rs"
    ).read_text(encoding="utf-8")
    assert FORMAL_PACKAGING_PRODUCER in build_script
    assert PACKAGE_TEST not in build_script


def test_every_package_test_workflow_job_uses_locked_python_test_installer() -> None:
    workflow_root = ROOT / ".github" / "workflows"
    workflow_paths = sorted(
        [*workflow_root.glob("*.yml"), *workflow_root.glob("*.yaml")]
    )
    invoked_jobs: list[tuple[Path, str]] = []
    for workflow_path in workflow_paths:
        workflow = workflow_path.read_text(encoding="utf-8")
        for job_name, job in _job_blocks(workflow).items():
            if _invokes_package_test(job):
                invoked_jobs.append((workflow_path, job_name))
                assert LOCKED_INSTALLER in job, (
                    f"{workflow_path}:{job_name} must install locked runtime "
                    "and test dependencies before invoking the package test"
                )
                assert "pip install pytest cryptography" not in job

    assert invoked_jobs == [
        (ROOT / ".github" / "workflows" / "desktop-installers.yml", "build-installer"),
        (ROOT / ".github" / "workflows" / "release.yml", "build"),
        (ROOT / ".github" / "workflows" / "test.yml", "pack-architecture"),
        (ROOT / ".github" / "workflows" / "test.yml", "tobkiri-launcher-macos"),
        (ROOT / ".github" / "workflows" / "test.yml", "tobkiri-launcher-windows"),
    ]
