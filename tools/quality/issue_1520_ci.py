#!/usr/bin/env python3
"""Run actual Rust tests and stage the official Linux Python parity fixture."""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
import platform
import re
import shutil
import subprocess
import sys
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
PLAN = Path("sdk/python/tests/fixtures/build/shell-policy/plan.json")
CORPUS_SHA256 = "504e542a6e4cec983e402753db7a3bf93a96f394b69400249d353d1e7442edd5"
LINUX_TAGS = {
    "componentize-py": "cp39-abi3-manylinux_2_28_x86_64",
    "wasmtime": "py3-none-manylinux1_x86_64",
}
LINUX_COMPOSER_BYTES = 22922992
PARITY_RECEIPT = "Python wasm parity receipt: cases=91 "
TEST_RESULT = re.compile(
    r"^test result: ok\. (\d+) passed; 0 failed; \d+ ignored; "
    r"\d+ measured; \d+ filtered out;.*$", re.MULTILINE,
)


def digest(path: Path) -> str:
    """Hash actual bytes without loading a component into memory."""
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def load_module(path: Path, name: str) -> Any:
    """Load an existing producer without invoking its command-line entry point."""
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def validate_test_command(command: list[str]) -> None:
    """Compile-only and listing commands cannot produce runtime acceptance."""
    if (command[:2] != ["cargo", "test"] or "--locked" not in command
            or any(arg in {"--no-run", "--list", "--help", "-h"}
                   or arg.startswith("--no-run=") for arg in command)):
        raise ValueError("acceptance requires cargo test --locked with execution")


def test_receipt(output: str, returncode: int, *, expected_passed: int | None = None,
                 python_parity: bool = False) -> dict[str, Any]:
    """Require a successful process and at least one completed libtest case."""
    if returncode != 0:
        raise ValueError(f"cargo test failed with exit status {returncode}")
    counts = [int(match.group(1)) for match in TEST_RESULT.finditer(output)]
    passed = sum(counts)
    if not counts or passed == 0:
        raise ValueError("cargo test completed without any passing executed tests")
    if expected_passed is not None and passed != expected_passed:
        raise ValueError(
            f"expected {expected_passed} executed tests, observed {passed}")
    if python_parity and PARITY_RECEIPT not in output:
        raise ValueError("the original 91-case Python replay receipt is absent")
    return {"passed": passed, "libtest_summaries": len(counts)}


def cargo_test(command: list[str], log: Path, *, expected_passed: int | None,
               python_parity: bool) -> None:
    """Stream the real process output and retain its executed-test receipt."""
    validate_test_command(command)
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open("w", encoding="utf-8") as stream:
        process = subprocess.Popen(command, cwd=ROOT, stdout=subprocess.PIPE,
                                   stderr=subprocess.STDOUT, text=True)
        assert process.stdout is not None
        for line in process.stdout:
            print(line, end="", flush=True)
            stream.write(line)
        returncode = process.wait()
    receipt = test_receipt(log.read_text(encoding="utf-8"), returncode,
                           expected_passed=expected_passed,
                           python_parity=python_parity)
    receipt["command"] = command
    log.with_suffix(".receipt.json").write_text(
        json.dumps(receipt, sort_keys=True, indent=2) + "\n", encoding="utf-8")


def linux_wheel_pins(root: Path) -> dict[str, dict[str, str]]:
    """Select the existing official x86_64 Linux pins, without updating them."""
    lock = json.loads((root / "sdk/python/toolchain-lock.json").read_text())
    if (lock.get("format") != "tobkiri.python-toolchain-lock/1"
            or lock.get("source") != "https://pypi.org/"):
        raise ValueError("unsupported official Python toolchain lock")
    return {name: {**lock["tools"][name]["wheels"][tag], "tag": tag,
                   "version": lock["tools"][name]["version"]}
            for name, tag in LINUX_TAGS.items()}


def verify_wheels(directory: Path, pins: dict[str, dict[str, str]]) -> None:
    """Reject wrong platform archives and changed bytes before installation."""
    if {path.name for path in directory.iterdir()} != {
            pin["filename"] for pin in pins.values()}:
        raise ValueError("downloaded wheel set differs from the official Linux pins")
    for pin in pins.values():
        path = directory / pin["filename"]
        if path.is_symlink() or not path.is_file() or digest(path) != pin["sha256"]:
            raise ValueError("wheel archive differs from its official Linux pin")


def verify_python_build(root: Path, artifact: Path, receipt: dict[str, Any]) -> None:
    """Bind the new component to current captured sources and producer evidence."""
    sdk = root / "sdk/python"
    # The normal producer's plan verifier is pure; it retains all path, WIT and
    # source-capture checks instead of maintaining a second plan implementation.
    sys.path.insert(0, str(sdk))
    try:
        producer = load_module(sdk / "build_component.py", "issue1520_python_build")
    finally:
        sys.path.pop(0)
    plan, _, plan_hash, captures = producer._verify_plan(root / PLAN)
    sources = [{"source": item["source"], "destination": str(name),
                "sha256": hashlib.sha256(content).hexdigest(), "bytes": len(content)}
               for item, (name, content) in zip(plan["sources"], captures)]
    expected = {
        "format": "tobkiri.component-build/2", "language": "python",
        "build_mode": "official-wheel-pinned-development-producer",
        "production_enabled": False, "build_plan_sha256": plan_hash,
        "source_manifest": sources,
        "source_manifest_sha256": producer.canonical_hash(sources),
        "toolchain_lock_sha256": digest(sdk / "toolchain-lock.json"),
        "producer_sources": {name: digest(sdk / name) for name in (
            "build_component.py", "composition.py", "toolchain-lock.json")},
        "artifact_sha256": digest(artifact), "artifact_bytes": artifact.stat().st_size,
        "world": plan["world"], "imports": sorted(plan["allowed_imports"]),
        "exports": sorted(plan["outer_exports"]),
        "target_validation": {"tool": "wac-cli", "world": plan["world"],
                              "exact_exports": True, "typed_target_check": True},
    }
    for field, value in expected.items():
        if receipt.get(field) != value:
            raise ValueError(f"Python build provenance mismatch: {field}")
    for name, pin in linux_wheel_pins(root).items():
        tool = receipt.get("tools", {}).get(name, {})
        wheel = tool.get("wheel", {})
        if (tool.get("version") != pin["version"]
                or tool.get("installed_record_consistency_verified") is not True
                or tool.get("installed_tree_authenticated_by_archive") is not False
                or any(wheel.get(field) != value for field, value in {
                    "filename": pin["filename"], "tag": pin["tag"],
                    "official_pypi_sha256_pin": pin["sha256"],
                    "archive_sha256": pin["sha256"],
                    "archive_sha256_verified": True,
                }.items())):
            raise ValueError(f"Python build provenance mismatch: {name} wheel")
    composition = load_module(sdk / "composition.py", "issue1520_composition")
    proof = receipt.get("composition", {})
    if (proof.get("tool") != "wac-cli"
            or proof.get("version") != composition.COMPOSER_VERSION
            or proof.get("tool_sha256") != composition.COMPOSER_RELEASE_HASHES[
                "x86_64-unknown-linux-musl"]
            or proof.get("flags") != {
                "define_components": True, "validate": True,
                "ambient_dependency_directory": False, "typed_target_validation": True}
            or receipt.get("options") != {
                "stub_wasi": True, "inherited_environment": False,
                "compiler_process_pythonhashseed": "0",
                "guest_pythonhashseed": "compiler-default-uncontrolled",
                "parallel_validation_compilation": False}):
        raise ValueError("Python build lacks the pinned typed outer-component proof")


def stage_python_pack(root: Path, artifact: Path, destination: Path) -> None:
    """Use the normal Pack writer with actual new artifact and receipt bytes."""
    receipt_path = artifact.with_suffix(".build.json")
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    verify_python_build(root, artifact, receipt)
    source = root / "packs/local.shell-policy-python"
    manifest = json.loads((source / "pack.json").read_text(encoding="utf-8"))
    if digest(source / "tests/inspection.json") != CORPUS_SHA256:
        raise ValueError("the original 91-case Python corpus changed")
    if manifest["runtime"]["component"] != "component.wasm":
        raise ValueError("the reviewed Python Pack component path changed")
    destination.mkdir(parents=True, exist_ok=False)
    paths = [item["path"] for item in manifest["files"]]
    for item in manifest["files"]:
        relative = item["path"]
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        if relative == "component.wasm":
            original = artifact
        elif relative == "component.build.json":
            original = receipt_path
        else:
            original = source / relative
            if (original.is_symlink() or not original.is_file()
                    or "sha256:" + digest(original) != item["sha256"]
                    or original.stat().st_size != item["size"]):
                raise ValueError("reviewed Python Pack file differs from its manifest")
        shutil.copyfile(original, target)
    # Only file bindings change. Permissions, routing and runtime declarations
    # remain the reviewed template, and no generated files touch the checkout.
    manifest["files"] = []
    writer = load_module(root / "packs/build.py", "issue1520_pack_writer")
    writer.bind_files(destination, manifest, paths)
    writer.write_json(destination.parent / "python-fixture.receipt.json", {
        "format": "tobkiri.issue1520-python-ci/1", "production_enabled": False,
        "template_manifest_sha256": digest(source / "pack.json"),
        "manifest_sha256": digest(destination / "pack.json"),
        "component_sha256": digest(destination / "component.wasm"),
        "build_receipt_sha256": digest(destination / "component.build.json"),
        "corpus_sha256": CORPUS_SHA256,
    })


def prepare_python(work: Path, composer: Path) -> None:
    """Install pinned official Linux wheels, then invoke the existing producer."""
    if platform.system() != "Linux" or platform.machine() != "x86_64":
        raise ValueError("this CI producer requires the pinned x86_64 Linux platform")
    composition = load_module(ROOT / "sdk/python/composition.py", "issue1520_wac")
    if (composer.is_symlink() or not composer.is_file()
            or composer.stat().st_size != LINUX_COMPOSER_BYTES
            or digest(composer) != composition.COMPOSER_RELEASE_HASHES[
                "x86_64-unknown-linux-musl"]):
        raise ValueError("composer differs from the pinned official Linux release")
    work.mkdir(parents=True, exist_ok=False)
    wheels = work / "wheels"
    wheels.mkdir()
    pins = linux_wheel_pins(ROOT)
    requirements = work / "requirements.txt"
    requirements.write_text("".join(
        f"{name}=={pin['version']} --hash=sha256:{pin['sha256']}\n"
        for name, pin in pins.items()), encoding="utf-8")
    subprocess.run([sys.executable, "-m", "venv", str(work / "venv")], check=True)
    python = work / "venv/bin/python"
    pip = [str(python), "-m", "pip", "--isolated", "--disable-pip-version-check"]
    subprocess.run([*pip, "download", "--index-url", "https://pypi.org/simple",
                    "--require-hashes", "--only-binary=:all:", "--no-deps",
                    "--dest", str(wheels), "-r", str(requirements)], check=True)
    verify_wheels(wheels, pins)
    subprocess.run([*pip, "install", "--no-index", "--no-deps", "--no-compile",
                    *(str(wheels / pin["filename"]) for pin in pins.values())],
                   check=True)
    artifact = work / "shell-policy.wasm"
    subprocess.run([
        str(python), "-B", str(ROOT / "sdk/python/build_component.py"),
        "--plan", str(ROOT / PLAN), "--output", str(artifact),
        "--composer", str(composer),
        "--componentize-wheel", str(wheels / pins["componentize-py"]["filename"]),
        "--wasmtime-wheel", str(wheels / pins["wasmtime"]["filename"]),
    ], check=True, cwd=ROOT)
    stage_python_pack(ROOT, artifact, work / "pack")


def main() -> None:
    """Expose only the two acceptance operations used by the workflow."""
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="operation", required=True)
    tests = commands.add_parser("cargo-test")
    tests.add_argument("--log", type=Path, required=True)
    tests.add_argument("--expected-passed", type=int)
    tests.add_argument("--python-parity", action="store_true")
    tests.add_argument("command", nargs=argparse.REMAINDER)
    python = commands.add_parser("prepare-python")
    python.add_argument("--work-dir", type=Path, required=True)
    python.add_argument("--composer", type=Path, required=True)
    args = parser.parse_args()
    if args.operation == "cargo-test":
        command = args.command[1:] if args.command[:1] == ["--"] else args.command
        cargo_test(command, args.log, expected_passed=args.expected_passed,
                   python_parity=args.python_parity)
    else:
        prepare_python(args.work_dir, args.composer)


if __name__ == "__main__":
    main()
