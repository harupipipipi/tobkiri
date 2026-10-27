"""Source/data contracts only; actual admission and Flow run separately in CI."""

import ast
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import materialize_bundle_catalog as materialize

ROOT = Path(__file__).resolve().parents[3]


class BundleMaterializationTests(unittest.TestCase):
    def test_manifest_uses_no_large_config_or_sidecar_and_no_implicit_rights(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            files = {
                "component.wasm": b"inert source fixture, not executable",
                "wit/pack.wit": b"inert WIT fixture",
            }
            materialize.write_pack(
                root,
                materialize.CATALOG_PACK,
                files,
                shapes=[(materialize.CATALOG, "port")],
                provides=[materialize.CATALOG],
                consumes=[],
                allowed=[],
                flows=[],
                toolchain="source-fixture-only",
            )
            manifest = json.loads(
                (root / "packs" / materialize.CATALOG_PACK / "pack.json").read_bytes()
            )
            self.assertIsNone(manifest["runtime"]["config"])
            self.assertIsNone(manifest["publisher_key"])
            self.assertEqual(manifest["permissions"]["ports"], [])
            self.assertEqual(manifest["permissions"]["fs"], [])
            self.assertFalse(manifest["permissions"]["native"])
            self.assertEqual({row["path"] for row in manifest["files"]}, set(files))

    def test_argument_free_flow_uses_the_distinct_catalog_pack(self):
        root = ROOT / "packs/local.harness-catalog-input"
        flow = json.loads((root / "flows/read.flow.json").read_bytes())
        self.assertEqual(flow["implements"], materialize.FLOW)
        self.assertEqual(flow["trigger"]["input"], materialize.REQUESTS)
        self.assertEqual(
            flow["steps"],
            [
                {
                    "call": {"args": {}, "op": "read", "shape": materialize.CATALOG},
                    "id": "catalog",
                    "out": "catalog",
                }
            ],
        )
        source = (root / "src/lib.rs").read_text()
        self.assertIn('flow::run_flow(CATALOG_FLOW, "{}")', source)
        for refused in ("terminal_read", "call_user_port", "CallCx::"):
            self.assertNotIn(refused, source)

    def test_received_ui_entry_and_actual_core_are_required_by_the_consumer(self):
        source = (ROOT / "crates/tobkiri-e2e/tests/bundle_catalog.rs").read_text()
        compact = "".join(source.split())
        for required in (
            "session.ui_invoke_received(",
            "self.core.save_profile(",
            "self.core.start(",
            "original_terminal_pair(",
            ".init_config()?",
        ):
            self.assertIn(required, compact)
        for refused in ("CallCx::", "call_user_port(", "approve_literal_user("):
            self.assertNotIn(refused, compact)

    def test_materializer_requires_actual_receipt_and_embedded_source_identity(self):
        source = (
            ROOT / "tools/python313-producer/materialize_bundle_catalog.py"
        ).read_text()
        tree = ast.parse(source)
        run = next(
            n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "run"
        )
        first = run.body[1] if isinstance(run.body[0], ast.Expr) else run.body[0]
        self.assertIsInstance(first, ast.If)
        self.assertIn("GITHUB_ACTIONS", ast.get_source_segment(source, first))
        self.assertTrue(
            any(
                isinstance(n, ast.Call)
                and isinstance(n.func, ast.Attribute)
                and n.func.attr == "verify_component"
                for n in ast.walk(run)
            )
        )
        self.assertIn('captured["bundled-defaults/embedded.json"]', source)
        self.assertIn('source["original_bundle_cases"]', source)


if __name__ == "__main__":
    unittest.main()
