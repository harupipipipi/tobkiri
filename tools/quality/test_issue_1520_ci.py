"""Pure regressions for CI execution counts and official producer evidence."""
from __future__ import annotations

import copy
import hashlib
import importlib.util
from pathlib import Path
import sys
import tempfile
import unittest

spec = importlib.util.spec_from_file_location(
    "issue_ci", Path(__file__).with_name("issue_1520_ci.py"))
ci = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ci)


def result(passed: int, ignored: int = 0) -> str:
    """Use the actual stable libtest summary format, without spawning Cargo."""
    return (f"test result: ok. {passed} passed; 0 failed; {ignored} ignored; "
            "0 measured; 0 filtered out; finished in 0.00s\n")


class RustExecutionTests(unittest.TestCase):
    """Compilation or an empty filtered selection cannot satisfy acceptance."""

    def test_workspace_counts_real_cases_among_empty_library_suites(self) -> None:
        receipt = ci.test_receipt(result(0) + result(3) + result(0), 0)
        self.assertEqual(receipt["passed"], 3)

    def test_zero_cases_and_compile_output_are_rejected(self) -> None:
        for output in (result(0, ignored=1), "Finished `test` profile\n"):
            with self.subTest(output=output), self.assertRaisesRegex(
                    ValueError, "without any passing executed tests"):
                ci.test_receipt(output, 0)

    def test_compile_only_and_list_commands_are_rejected(self) -> None:
        for command in (
            ["cargo", "check", "--workspace", "--locked"],
            ["cargo", "test", "--locked", "--no-run"],
            ["cargo", "test", "--locked", "--", "--list"],
        ):
            with self.subTest(command=command), self.assertRaises(ValueError):
                ci.validate_test_command(command)
        ci.validate_test_command(["cargo", "test", "--workspace", "--locked"])

    def test_earlier_passing_tests_cannot_mask_a_later_process_failure(self) -> None:
        with self.assertRaisesRegex(ValueError, "exit status 101"):
            ci.test_receipt(result(7), 101)

    def test_python_replay_requires_one_case_and_its_full_corpus_receipt(self) -> None:
        output = (ci.PARITY_RECEIPT
                  + "component_bytes=123 scope=classifier-and-tokenizer\n")
        ci.test_receipt(output + result(1), 0, expected_passed=1,
                        python_parity=True)
        for candidate in (result(1), output + result(0, 1), output + result(2)):
            with self.subTest(output=candidate), self.assertRaises(ValueError):
                ci.test_receipt(candidate, 0, expected_passed=1, python_parity=True)


class ProducerEvidenceTests(unittest.TestCase):
    """Changed platform archives and stale build/source evidence fail closed."""

    def test_wrong_linux_archive_name_and_bytes_are_rejected(self) -> None:
        pins = ci.linux_wheel_pins(ci.ROOT)
        with tempfile.TemporaryDirectory() as temporary:
            wheels = Path(temporary)
            for pin in pins.values():
                (wheels / pin["filename"]).write_bytes(b"changed archive")
            with self.assertRaisesRegex(ValueError, "official Linux pin"):
                ci.verify_wheels(wheels, pins)
            first = next(iter(pins.values()))
            (wheels / first["filename"]).rename(wheels / "wrong-platform.whl")
            with self.assertRaisesRegex(ValueError, "wheel set"):
                ci.verify_wheels(wheels, pins)

    def build_receipt(self, artifact: Path) -> dict:
        """Create unit-only evidence from the actual current captured inputs.

        These bytes exercise the receipt validator; they are never represented as
        a compiled component, staged for Rust, or counted as runtime acceptance.
        """
        sdk = ci.ROOT / "sdk/python"
        sys.path.insert(0, str(sdk))
        try:
            producer = ci.load_module(sdk / "build_component.py", "ci_test_producer")
        finally:
            sys.path.pop(0)
        plan, _, plan_hash, captures = producer._verify_plan(ci.ROOT / ci.PLAN)
        sources = [{"source": item["source"], "destination": str(name),
                    "sha256": hashlib.sha256(content).hexdigest(),
                    "bytes": len(content)}
                   for item, (name, content) in zip(plan["sources"], captures)]
        composition = ci.load_module(sdk / "composition.py", "ci_test_composition")
        return {
            "format": "tobkiri.component-build/2", "language": "python",
            "build_mode": "official-wheel-pinned-development-producer",
            "production_enabled": False, "build_plan_sha256": plan_hash,
            "source_manifest": sources,
            "source_manifest_sha256": producer.canonical_hash(sources),
            "toolchain_lock_sha256": ci.digest(sdk / "toolchain-lock.json"),
            "producer_sources": {name: ci.digest(sdk / name) for name in (
                "build_component.py", "composition.py", "toolchain-lock.json")},
            "artifact_sha256": ci.digest(artifact),
            "artifact_bytes": artifact.stat().st_size,
            "world": plan["world"], "imports": sorted(plan["allowed_imports"]),
            "exports": sorted(plan["outer_exports"]),
            "target_validation": {"tool": "wac-cli", "world": plan["world"],
                                  "exact_exports": True, "typed_target_check": True},
            "tools": {
                name: {
                    "version": pin["version"],
                    "installed_record_consistency_verified": True,
                    "installed_tree_authenticated_by_archive": False,
                    "wheel": {"filename": pin["filename"], "tag": pin["tag"],
                              "official_pypi_sha256_pin": pin["sha256"],
                              "archive_sha256": pin["sha256"],
                              "archive_sha256_verified": True},
                } for name, pin in ci.linux_wheel_pins(ci.ROOT).items()},
            "composition": {
                "tool": "wac-cli", "version": composition.COMPOSER_VERSION,
                "tool_sha256": composition.COMPOSER_RELEASE_HASHES[
                    "x86_64-unknown-linux-musl"],
                "flags": {"define_components": True, "validate": True,
                          "ambient_dependency_directory": False,
                          "typed_target_validation": True}},
            "options": {"stub_wasi": True, "inherited_environment": False,
                        "compiler_process_pythonhashseed": "0",
                        "guest_pythonhashseed": "compiler-default-uncontrolled",
                        "parallel_validation_compilation": False},
        }

    def test_stale_source_artifact_wheel_and_abi_proof_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            artifact = Path(temporary) / "unit-only.wasm"
            artifact.write_bytes(b"unit-only receipt-validator input")
            baseline = self.build_receipt(artifact)
            ci.verify_python_build(ci.ROOT, artifact, baseline)
            mutations = (
                ("artifact_sha256", "0" * 64),
                ("build_plan_sha256", "0" * 64),
                ("producer_sources", {}),
                ("source_manifest", []),
                ("target_validation", {"exact_exports": False}),
            )
            for field, value in mutations:
                changed = copy.deepcopy(baseline)
                changed[field] = value
                with self.subTest(field=field), self.assertRaisesRegex(
                        ValueError, "provenance mismatch"):
                    ci.verify_python_build(ci.ROOT, artifact, changed)
            changed = copy.deepcopy(baseline)
            changed["tools"]["componentize-py"]["wheel"][
                "archive_sha256_verified"] = False
            with self.assertRaisesRegex(ValueError, "componentize-py wheel"):
                ci.verify_python_build(ci.ROOT, artifact, changed)
            changed = copy.deepcopy(baseline)
            changed["composition"]["tool_sha256"] = "0" * 64
            with self.assertRaisesRegex(ValueError, "typed outer-component proof"):
                ci.verify_python_build(ci.ROOT, artifact, changed)


if __name__ == "__main__":
    unittest.main()
