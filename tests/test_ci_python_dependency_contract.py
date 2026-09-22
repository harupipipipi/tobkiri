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
