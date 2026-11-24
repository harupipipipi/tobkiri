"""Offline compiler validation, separate from VM/product acceptance."""
import hashlib
import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
# Standalone tests need the owned author directory before importing its builder.
sys.path.insert(0, str(ROOT))
from build_pack import build  # noqa: E402 -- required standalone author bootstrap
from tobkiri_host.artifact_compiler import compile_pack_root  # noqa: E402

SPEC = importlib.util.spec_from_file_location("temporal_abi", ROOT / "source/temporal.py")
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def inventory(root):
    return {str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in root.rglob("*") if path.is_file()}


class PublicAuthoringTests(unittest.TestCase):
    def test_deterministic_public_compiler_route(self):
        with tempfile.TemporaryDirectory(prefix=".author-test-", dir=ROOT) as temp:
            first = build(Path(temp) / "first")
            second = build(Path(temp) / "second")
            self.assertEqual(inventory(first), inventory(second))
            compiled = compile_pack_root(first)
            route = compiled.routes[("acceptance.temporal.context.v1", "temporal.reduce")]
            self.assertEqual(route["runtime_abi"], "python3.13")
            self.assertEqual(route["function_id"], "acceptance.temporal.context.reduce")
            self.assertEqual(route["backend"], "tobkiri.python-pack-v4")
            variant = json.loads((first / "executables.v4.json").read_text())["variants"][0]
            self.assertEqual(variant["implementation_digest"], "sha256:" +
                             hashlib.sha256((ROOT / "source/temporal.py").read_bytes())
                             .hexdigest())

    def test_configurable_ids_compile_without_legacy_binding(self):
        with tempfile.TemporaryDirectory(prefix=".author-rename-", dir=ROOT) as temp:
            root = build(Path(temp) / "renamed", "independent.temporal.renamed",
                         "independent.temporal.function")
            compiled = compile_pack_root(root)
            self.assertNotIn(("acceptance.temporal.context.v1", "temporal.reduce"),
                             compiled.routes)
            route = compiled.routes[("independent.temporal.renamed.v1", "temporal.reduce")]
            self.assertEqual(route["function_id"], "independent.temporal.function")

    def test_abi_unknown_operation_and_invalid_input_fail(self):
        with self.assertRaisesRegex(ValueError, "unknown operation"):
            MODULE.tobkiri_packvm_invoke("unknown", {})
        with self.assertRaises(ValueError):
            MODULE.tobkiri_packvm_invoke("temporal.reduce", {})
        payload = {"namespace": "test", "state": {}, "event": {
            "id": "e1", "sequence": 1, "kind": "assistant.completed",
            "at": "2026-10-08T00:00:00Z"}}
        result = MODULE.tobkiri_packvm_invoke("temporal.reduce", payload)
        self.assertEqual(result["state"]["last_task_completed_at"],
                         "2026-10-08T00:00:00Z")
        self.assertIsNone(result["internal_temporal_context"])
        self.assertNotIn("tobkiri.packvm", json.dumps(result))


if __name__ == "__main__":
    unittest.main()
