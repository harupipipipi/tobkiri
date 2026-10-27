"""Receipt integrity tests; these never execute or construct an actual guest."""
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import component_inputs
import thread_capabilities_ci_inputs as binder


class ReceiptTests(unittest.TestCase):
    def fixture(self, root):
        raw = b"synthetic receipt-integrity input, not an executable component"
        artifact = root / "component.data"
        artifact.write_bytes(raw)
        plan = component_inputs._thread_capabilities_plan(
            component_inputs._capture_thread_capabilities_sources(component_inputs.HERE.parents[1])
        )
        plan_path = root / "plan.json"
        plan_path.write_text(json.dumps(plan))
        plan_hash = hashlib.sha256(plan_path.read_bytes()).hexdigest()
        proof = {
            "format": "tobkiri.component-build/2", "diagnostic_only": True,
            "whole_component_parity": False, "production_enabled": False,
            "guest_runtime_identity_verified": False,
            "product_resident_concurrency_cancel_acceptance_claimed": False,
            "artifact_sha256": hashlib.sha256(raw).hexdigest(), "artifact_bytes": len(raw),
            "build_plan_sha256": plan_hash,
            "compiler_executable_sha256": "1" * 64, "producer_ci_receipt_sha256": "2" * 64,
            "world": component_inputs.THREAD_CAPABILITIES_WORLD,
            "imports": component_inputs.THREAD_CAPABILITIES_IMPORTS,
            "exports": component_inputs.THREAD_CAPABILITIES_EXPORTS,
            "options": {"stub_wasi": True, "inherited_environment": False},
            "target_validation": {"exact_exports": True, "typed_target_check": True},
        }
        proof_path = root / "proof.json"
        proof_path.write_text(json.dumps(proof))
        outer = {
            "format": "tobkiri.python313-native-thread-capabilities-diagnostic-candidate/1",
            "outcome": "success", "inputs_unchanged": True, "diagnostic_only": True,
            "production_enabled": False, "whole_component_parity": False,
            "guest_runtime_identity_verified": False, "native_core_replay": False,
            "product_resident_concurrency_cancel_acceptance_claimed": False,
            "source": {"sha256": binder.STAGED_SHA256},
            "producer": {"sha256": "1" * 64}, "producer_receipt": {"sha256": "2" * 64},
            "components": {"harness-thread-capabilities": {
                "artifact": {"bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()},
                "build_receipt": {"bytes": proof_path.stat().st_size,
                                  "sha256": hashlib.sha256(proof_path.read_bytes()).hexdigest()},
                "source": {"plan_sha256": plan_hash},
            }},
        }
        receipt = root / "receipt.json"
        receipt.write_text(json.dumps(outer))
        return artifact, receipt, proof_path, plan_path

    def test_integrity_binding_is_not_execution_or_product_acceptance(self):
        with tempfile.TemporaryDirectory() as temporary:
            paths = self.fixture(Path(temporary).resolve())
            result = binder.bind(*paths)
            self.assertEqual(result["TOBKIRI_TEST_THREAD_COMPONENT"], str(paths[0]))
            self.assertNotIn("product_acceptance", result)
            paths[0].write_bytes(b"changed after publication")
            with self.assertRaisesRegex(ValueError, "differs"):
                binder.bind(*paths)

    def test_unstubbed_imports_and_acceptance_claims_are_rejected_even_if_rehashed(self):
        for field, value in [("production_enabled", True), ("whole_component_parity", True),
                             ("options", {"stub_wasi": False, "inherited_environment": True}),
                             ("imports", ["wasi:filesystem/types@0.2.0"])]:
            with self.subTest(field=field), tempfile.TemporaryDirectory() as temporary:
                paths = self.fixture(Path(temporary).resolve())
                proof = json.loads(paths[2].read_bytes())
                proof[field] = value
                paths[2].write_text(json.dumps(proof))
                outer = json.loads(paths[1].read_bytes())
                outer["components"]["harness-thread-capabilities"]["build_receipt"] = {
                    "bytes": paths[2].stat().st_size,
                    "sha256": hashlib.sha256(paths[2].read_bytes()).hexdigest(),
                }
                paths[1].write_text(json.dumps(outer))
                with self.assertRaisesRegex(ValueError, "differs"):
                    binder.bind(*paths)

    def test_symlinked_input_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            paths = self.fixture(root)
            alias = root / "alias.data"
            alias.symlink_to(paths[0])
            with self.assertRaisesRegex(ValueError, "physical"):
                binder.bind(alias, *paths[1:])


if __name__ == "__main__":
    unittest.main()
