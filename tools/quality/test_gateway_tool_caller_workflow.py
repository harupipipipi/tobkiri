"""CI routing/export contracts only, never native or guest acceptance."""
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

from test_issue_1520_workflow import ROOT, WORKFLOW, job, step

STAGE = "Authenticate the preserved Gateway tool-caller graph"
REPLAY = "Replay the original Python tool caller through actual Rust Core"
RETAIN = "Retain Gateway tool-caller native evidence"
INDEX_SHA = "0441bde565b5e1765a8fdc262e61db00a5fff0c160624d99d5d841f65c737d96"
NATIVE_TEST = (
    "gateway_tool_caller::"
    "original_python_tool_caller_replays_through_real_core"
)
INDEX = (
    ROOT / "tools/python313-producer/trials/2026-10-20-native-caller-static"
    / "bundled-index.json"
)
FLAGS = (
    "build_performed", "wasm_execution_performed",
    "native_core_acceptance", "production_enabled",
)


class GatewayToolCallerWorkflowTests(unittest.TestCase):
    """Inspect routing and execute only the workflow's metadata export code."""

    def setUp(self) -> None:
        self.source = WORKFLOW.read_text(encoding="utf-8")
        self.native = job(self.source, "verified-platform-runtime")
        self.linux = job(self.source, "foundations")
        self.stage = step(self.native, STAGE)
        self.replay = step(self.native, REPLAY)

    def export_code(self) -> str:
        """Extract the exact inline exporter rather than a test replacement."""
        match = re.search(
            r"python3 -B - [^\n]* <<'PY' \| tee -a [^\n]*\n"
            r"(.*?)^          PY$",
            self.stage, re.MULTILINE | re.DOTALL,
        )
        self.assertIsNotNone(match)
        code = textwrap.dedent(match.group(1))
        self.assertIn("import hashlib", code)
        self.assertIn("environment.write", code)
        return code

    def test_portable_regressions_do_not_run_the_native_entry(self) -> None:
        routing = step(self.linux, "Gateway tool-caller native routing regression")
        transport = step(
            self.linux, "Preserved Gateway tool-caller transport regression")
        self.assertIn("python3 -B tools/quality/test_gateway_tool_caller_workflow.py",
                      routing)
        self.assertIn(
            "python3 -B -m unittest discover -s tests "
            "-p test_preserved_gateway_tool_caller.py", transport)
        self.assertNotIn(NATIVE_TEST, self.linux)

    def test_native_steps_require_exact_successful_prerequisites(self) -> None:
        self.assertIn("runs-on: macos-15", self.native)
        preflight = step(
            self.native, "Verify the currently supported native proof platform")
        for check in (
            'test "$(uname -m)" = arm64', 'test "$(uname -r)" = 24.6.0',
            'df -P "$RUNNER_TEMP"', 'diskutil info "$device"',
            "Type \\(Bundle\\):[[:space:]]+apfs",
        ):
            self.assertIn(check, preflight)
        base = ("${{ !cancelled() && steps.native_platform.outcome == 'success'"
                " && steps.rust_toolchain.outcome == 'success'")
        for block, suffix in (
            (self.stage, " }}"),
            (self.replay,
             " && steps.stage_gateway_tool_caller.outcome == 'success' }}"),
        ):
            condition = re.search(r"^        if: (.+)$", block, re.MULTILINE)
            self.assertEqual(condition.group(1), base + suffix)
        self.assertIn("id: stage_gateway_tool_caller", self.stage)
        self.assertLess(self.native.index(preflight), self.native.index(self.stage))
        self.assertLess(self.native.index(self.stage), self.native.index(self.replay))
        for bypass in ("continue-on-error", "|| true", "success()"):
            self.assertNotIn(bypass, self.stage + self.replay)

    def test_preparation_only_uses_preserved_fixture_and_fresh_graph(self) -> None:
        self.assertIn("shell: bash", self.stage)
        self.assertIn("set -euo pipefail", self.stage)
        self.assertIn("python3 -B scripts/wasm/stage_preserved_gateway_tool_caller.py",
                      self.stage)
        self.assertIn('--output "$RUNNER_TEMP/issue-1520-gateway-tool-caller"',
                      self.stage)
        self.assertIn('tee "$RUNNER_TEMP/gateway-tool-caller-stage.log"', self.stage)
        self.assertIn(
            "2>&1 <<'PY' | tee -a "
            '"$RUNNER_TEMP/gateway-tool-caller-stage.log"',
            self.stage,
        )
        for forbidden in (
            "curl ", "pip ", "componentize", "wac ", "cargo ", "download-artifact",
            "CARGO_PROFILE", "RUSTFLAGS", "sys.set", "setrecursionlimit",
        ):
            self.assertNotIn(forbidden, self.stage)
        body = self.stage.split("        run: |\n", 1)[1]
        checked = subprocess.run(
            ["bash", "-n"], input=textwrap.dedent(body),
            capture_output=True, text=True, check=False,
        )
        self.assertEqual(checked.returncode, 0, checked.stderr)

    def test_exact_native_test_cannot_be_a_static_or_skipped_substitute(self) -> None:
        for required in (
            "issue_1520_ci.py cargo-test",
            '--log "$RUNNER_TEMP/gateway-tool-caller-native.log"',
            "--expected-passed 1 --",
            "cargo test --locked -p tobkiri-e2e --test ordered_python",
            NATIVE_TEST,
            "-- --ignored --exact --nocapture --test-threads=1",
        ):
            self.assertIn(required, self.replay)
        for forbidden in (
            "--no-run", "--list", "--skip", "current_caller_graph_has_exact_static_abi",
            "--python-parity", "stage_python.", "stage_ordered_python.",
        ):
            self.assertNotIn(forbidden, self.replay)

    def test_retention_is_unconditional_and_contains_only_metadata(self) -> None:
        retained = step(self.native, RETAIN)
        self.assertIn("if: always()", retained)
        self.assertIn("uses: actions/upload-artifact@v4", retained)
        self.assertIn("name: gateway-tool-caller-native-replay", retained)
        self.assertIn("if-no-files-found: warn", retained)
        prefix = "${{ runner.temp }}/issue-1520-gateway-tool-caller/"
        expected = {
            prefix + name for name in (
                "materialization.json", "graph/index.json",
                "graph/source-observations.json", "graph/bridge/pack.json",
                "graph/bridge/component.build.json", "graph/caller/pack.json",
                "graph/caller/component.build.json", "native-replay.json",
                "module-inventory.json",
                "module-extraction.json",
                "shared-code/extraction.json", "shared-native-replay.json",
            )
        } | {
            "${{ runner.temp }}/" + name for name in (
                "gateway-tool-caller-stage.log", "gateway-tool-caller-native.log",
                "gateway-tool-caller-native.receipt.json",
                "shared-python-native.log", "shared-python-native.receipt.json",
            )
        }
        actual = set(re.findall(r"^            (.+)$", retained, re.MULTILINE))
        self.assertEqual(actual, expected)
        self.assertNotIn(".wasm", retained)
        self.assertNotIn("**", retained)

    def test_shared_module_replay_is_separate_and_requires_extracted_originals(self) -> None:
        staged = step(
            self.native, "Materialize exact shared modules for the original Python Packs"
        )
        replay = step(
            self.native, "Replay original Python Packs using authenticated shared modules"
        )
        for block, dependency in (
            (staged, "extract_original_modules"), (replay, "stage_shared_modules")
        ):
            for prerequisite in ("native_platform", "rust_toolchain", dependency):
                self.assertIn(f"steps.{prerequisite}.outcome == 'success'", block)
            self.assertIn("!cancelled()", block)
            self.assertNotIn("continue-on-error", block)
        bridge = '"$TOBKIRI_TEST_GATEWAY_TOOL_CALLER_GRAPH_DIR/bridge/component.wasm"'
        caller = '"$TOBKIRI_TEST_GATEWAY_TOOL_CALLER_GRAPH_DIR/caller/component.wasm"'
        self.assertLess(staged.index(bridge), staged.index(caller))
        self.assertIn("tools/component-unbundle/materialize.py", staged)
        self.assertIn("TOBKIRI_TEST_SHARED_MODULES_SHA256", staged)
        self.assertIn("hashlib.sha256(receipt).hexdigest()", staged)
        self.assertIn("/shared-native-replay.json", replay)
        self.assertNotIn("/native-replay.json", replay)
        self.assertIn("--expected-passed 1 --", replay)
        self.assertIn("-- --ignored --exact --nocapture --test-threads=1", replay)
        name = "original_python_shared_modules_replay_through_real_core"
        self.assertIn(name, replay)
        self.assertNotIn(name, self.linux)
        self.assertIn("/native-replay.json", step(self.native, STAGE))

    def test_module_inventory_uses_only_the_authenticated_original_graph(self) -> None:
        name = "Inventory exact original component module sharing"
        inspected = step(self.native, name)
        self.assertIn("steps.stage_gateway_tool_caller.outcome == 'success'", inspected)
        self.assertIn("tools/quality/component_module_inventory.py", inspected)
        for pack in ("caller", "bridge"):
            self.assertIn(
                f'"$TOBKIRI_TEST_GATEWAY_TOOL_CALLER_GRAPH_DIR/{pack}/component.wasm"',
                inspected,
            )
        self.assertIn("/module-inventory.json", inspected)
        self.assertNotIn("cargo ", inspected)
        self.assertNotIn("stage_preserved_", inspected)
        self.assertLess(self.native.index(name), self.native.index(REPLAY))
        self.assertIn("tools/quality/test_component_module_inventory.py", self.linux)

    def test_extraction_preserves_authenticated_inputs_and_independent_native_replay(self) -> None:
        extracted = step(self.native, "Extract exact original component modules")
        for prerequisite in ("native_platform", "rust_toolchain", "stage_gateway_tool_caller"):
            self.assertIn(f"steps.{prerequisite}.outcome == 'success'", extracted)
        self.assertIn("tools/component-unbundle/measure.py", extracted)
        for pack in ("caller", "bridge"):
            self.assertIn(
                f'"$TOBKIRI_TEST_GATEWAY_TOOL_CALLER_GRAPH_DIR/{pack}/component.wasm"',
                extracted,
            )
        self.assertIn("/module-extraction.json", extracted)
        self.assertNotIn("stage_preserved_", extracted)
        self.assertNotIn("module-extraction", self.replay)
        self.assertNotIn("component-unbundle", self.replay)
        self.assertIn("Bounded component extraction regression", self.linux)

    def run_export(
        self, *, metadata_change: dict | None = None,
        index_change: bytes | None = None, missing: str | None = None,
        existing_evidence: str | None = None, newline_root: bool = False,
    ) -> tuple[subprocess.CompletedProcess[str], str, str]:
        """Use archived index bytes only; do not stage or execute components."""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / (
                "fixture\ninjected" if newline_root else "fixture")
            (root / "graph").mkdir(parents=True)
            raw_index = INDEX.read_bytes()
            self.assertEqual(hashlib.sha256(raw_index).hexdigest(), INDEX_SHA)
            (root / "graph/index.json").write_bytes(
                raw_index if index_change is None else index_change)
            metadata = {
                "format": "tobkiri.preserved-gateway-tool-caller/1",
                "graph_directory": "graph", "graph_index_sha256": INDEX_SHA,
                **dict.fromkeys(FLAGS, False),
            }
            metadata.update(metadata_change or {})
            (root / "materialization.json").write_text(json.dumps(metadata))
            if missing:
                (root / missing).unlink()
            if existing_evidence == "file":
                (root / "native-replay.json").write_text("retain")
            elif existing_evidence == "dangling-link":
                (root / "native-replay.json").symlink_to(root / "absent")
            environment = Path(temporary) / "environment"
            environment.write_text("PRIOR=keep\n")
            result = subprocess.run(
                [sys.executable, "-B", "-c", self.export_code(),
                 str(root), str(environment)],
                capture_output=True, text=True, check=False,
            )
            return result, environment.read_text(), str(root)

    def test_exact_inactive_metadata_exports_only_three_pinned_values(self) -> None:
        result, environment, root = self.run_export()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(environment, (
            "PRIOR=keep\n"
            f"TOBKIRI_TEST_GATEWAY_TOOL_CALLER_GRAPH_DIR={root}/graph\n"
            f"TOBKIRI_TEST_GATEWAY_TOOL_CALLER_INDEX_SHA256={INDEX_SHA}\n"
            f"TOBKIRI_TEST_GATEWAY_TOOL_CALLER_EVIDENCE={root}/native-replay.json\n"
        ))

    def test_invalid_metadata_or_claims_never_exports_environment(self) -> None:
        changes = [
            {"format": "different"}, {"graph_directory": "../escape"},
            {"graph_index_sha256": "0" * 64},
        ] + [{key: value} for key in FLAGS for value in (True, 0, None)]
        for change in changes:
            with self.subTest(change=change):
                result, environment, _ = self.run_export(metadata_change=change)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(environment, "PRIOR=keep\n")

    def test_changed_or_missing_index_and_receipt_fail_before_export(self) -> None:
        cases = (
            {"index_change": b"{}"},
            {"missing": "graph/index.json"},
            {"missing": "materialization.json"},
        )
        for change in cases:
            with self.subTest(change=change):
                result, environment, _ = self.run_export(**change)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(environment, "PRIOR=keep\n")

    def test_old_receipts_links_and_environment_injection_fail(self) -> None:
        for change in (
            {"existing_evidence": "file"}, {"existing_evidence": "dangling-link"},
            {"newline_root": True},
        ):
            with self.subTest(change=change):
                result, environment, _ = self.run_export(**change)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(environment, "PRIOR=keep\n")


if __name__ == "__main__":
    unittest.main()
