"""Run fixed, unchanged original Setup tests in CI, never a native acceptance gate.

The original conftest creates its original verified packaged-bundle fixture.
These tests supply the pre-port behavior baseline for the Rust review caller;
they do not exercise a real DMG, GUI, native review or physical-fault recovery.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import subprocess
import sys
import xml.etree.ElementTree as ET

from original_authority_reference import ORIGINAL_HEAD, REQUIREMENTS, requirements


TESTS = (
    "tests/test_defaults_v4_setup_transaction.py::test_review_and_cancel_are_read_only",
    "tests/test_defaults_v4_setup_transaction.py::test_stale_catalog_shell_executable_pin_is_rejected",
    "tests/test_defaults_v4_setup_transaction.py::test_catalog_byte_tamper_is_rejected_before_user_state",
    "tests/test_inactive_bootstrap_successor.py::test_missing_handover_cannot_authorize_registry_replacement",
    "tests/test_inactive_bootstrap_successor.py::test_changed_bundle_requires_a_new_exact_review",
    "tests/test_inactive_bootstrap_successor.py::test_stale_authority_epoch_denies_successor_before_append",
)
PACKAGES = (*REQUIREMENTS, "jsonschema", "attrs", "jsonschema-specifications",
            "referencing", "rpds-py", "pyyaml", "typing-extensions", "wasmtime")
SOURCES = (
    "tests/conftest.py", "tests/conformance_support/packaged_profile.py",
    "core_runtime/bootstrap/profile_capture.py",
    "core_runtime/bootstrap/profile_publication.py",
    "core_runtime/bootstrap/profile_registry.py",
    "ecosystem/defaultspack/defaultspack/profile_runtime_composition.py",
    "ecosystem/defaultspack/domain/profile_resolution.py",
    *dict.fromkeys(test.split("::")[0] for test in TESTS),
)


def check_original(root: Path) -> None:
    """Require the actual clean original checkout and exact original interpreter."""
    if os.environ.get("GITHUB_ACTIONS") != "true" or platform.python_version() != "3.13.16":
        raise ValueError("original Setup execution requires explicit pinned CI inputs")
    head = subprocess.check_output(["git", "-C", str(root), "rev-parse", "HEAD"], text=True).strip()
    dirty = subprocess.check_output(
        ["git", "-C", str(root), "status", "--porcelain=v1", "--untracked-files=all"], text=True)
    if head != ORIGINAL_HEAD or dirty:
        raise ValueError("clean independent pre-port checkout required")


def run_tests(root: Path, destination: Path) -> None:
    """Execute the unchanged original tests and retain bounded result metadata only."""
    destination.mkdir(parents=True, exist_ok=False)
    runtime = root / "tobkiri_runtime"
    sources = {name: hashlib.sha256((runtime / name).read_bytes()).hexdigest()
               for name in SOURCES}
    junit = destination / "original-setup.xml"
    effects = destination / "readonly-effects.json"
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1",
               PYTHONPATH=os.pathsep.join((str(runtime), str(Path(__file__).parent.resolve()))),
               PYTEST_DISABLE_PLUGIN_AUTOLOAD="1",
               TOBKIRI_ORIGINAL_SETUP_ROOT=str(root),
               TOBKIRI_ORIGINAL_SETUP_EFFECTS=str(effects),
               TOBKIRI_USER_DATA=str(destination / "unused-host-state"),
               RUMI_USER_DATA=str(destination / "unused-host-state"))
    command = [sys.executable, "-B", "-m", "pytest", "-q", "-ra", "-p", "no:cacheprovider",
               "-p", "original_setup_review_observer",
               "--basetemp", str(destination / "pytest"), "--junitxml", str(junit), *TESTS]
    # Logs stay in the CI runner and are streamed to its job log. No database,
    # key, raw fixture, executable artifact or entire source tree is uploaded.
    completed = subprocess.run(command, cwd=runtime, env=env, timeout=900, check=False)
    cases = []
    if junit.is_file():
        if junit.stat().st_size > 2 * 1024 * 1024:
            raise ValueError("original Setup JUnit observation exceeds bound")
        for case in ET.parse(junit).iter("testcase"):
            outcome = next((tag for tag in ("error", "failure", "skipped")
                            if case.find(tag) is not None), "passed")
            cases.append({"name": case.get("name"), "outcome": outcome})
    expected = {test.split("::")[1] for test in TESTS}
    passed = (completed.returncode == 0 and len(cases) == len(TESTS)
              and {case["name"] for case in cases} == expected
              and all(case["outcome"] == "passed" for case in cases))
    report = {"format": "io.tobkiri.original-setup-reference.v1",
              "original_head": ORIGINAL_HEAD, "python": platform.python_version(),
              "sources": sources, "tests": list(TESTS), "cases": cases,
              "passed": passed, "native_comparison": False,
              "qualification": False, "production_enabled": False,
              "scope": "six original packaged-fixture Setup and successor regressions only"}
    if effects.is_file():
        if effects.stat().st_size > 128 * 1024:
            raise ValueError("original Setup side-effect diagnostic exceeds bound")
        report["readonly_effects"] = json.loads(effects.read_text())
    (destination / "summary.json").write_text(json.dumps(report, indent=2) + "\n")
    if not passed:
        raise AssertionError("all six original Setup reference cases must pass without skips")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("requirements", "run-tests"))
    parser.add_argument("--original-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    root = args.original_root.resolve(strict=True)
    check_original(root)
    if args.command == "requirements":
        # The original lock selects this rpds row for CPython >=3.11. The other
        # locked row is only for <3.11; never let pip silently choose a version.
        requirements(root, args.output, packages=PACKAGES, versions={"rpds-py": "2026.6.3"})
    else:
        run_tests(root, args.output.resolve())


if __name__ == "__main__":
    main()
