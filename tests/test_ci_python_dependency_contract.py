from pathlib import Path
import re


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
