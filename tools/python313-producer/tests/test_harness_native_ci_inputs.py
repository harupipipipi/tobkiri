"""Check the normal and diagnostic native Harness artifact selections."""

import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import harness_native_ci_inputs as binding  # noqa: E402
from build_native import fingerprint  # noqa: E402


def write(path, document):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document), encoding="utf-8")
    return fingerprint(path)


class HarnessNativeCIInputsTests(unittest.TestCase):
    def check_variant(self, root, diagnostic):
        name = "harness-native-schema-diagnostic" if diagnostic else "harness-native"
        directory = root / name
        artifact = directory / "work/component.wasm"
        artifact.parent.mkdir(parents=True)
        artifact.write_bytes(b"opaque component data")
        identity = fingerprint(artifact)
        provenance = write(directory / "inputs/provenance.json", {})
        metadata = {
            "format": binding.DIAGNOSTIC_FORMAT if diagnostic else binding.NORMAL_DEPENDENCIES,
            "original_source_files_unchanged": not diagnostic,
        }
        if diagnostic:
            metadata["diagnostic_only"] = True
        metadata_name = "python/" + binding.METADATA
        metadata_identity = write(directory / "inputs" / metadata_name, metadata)
        row = {
            "source": metadata_name,
            "destination": binding.METADATA,
            "sha256": metadata_identity["sha256"],
        }
        plan = {"entrypoint": binding.ENTRYPOINT, "sources": [row]}
        manifest = [{**row, "bytes": metadata_identity["bytes"]}]
        build = {
            "source_manifest": manifest,
            "source_manifest_sha256": hashlib.sha256(
                json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest(),
            "build_plan_sha256": "fixture plan",
            "world": "harness-native",
            "imports": [binding.IMPORT],
            "exports": [binding.RUNTIME, binding.TRANSPORT],
            "inner": {
                "imports": [binding.IMPORT],
                "exports": ["exports", binding.RUNTIME, binding.TRANSPORT],
            },
            "harness_native_dependencies": metadata,
            "compiler_executable_sha256": "fixture producer",
            "producer_ci_receipt_sha256": "fixture producer receipt",
        }
        variant = "schema-diagnostic" if diagnostic else "candidate"
        member = {
            "artifact": identity,
            "build_receipt": write(artifact.with_name("component.build.json"), build),
            "source": {
                "source_files": plan["sources"],
                "plan_sha256": build["build_plan_sha256"],
                "files": {"provenance.json": provenance, metadata_name: metadata_identity},
            },
        }
        receipt = {
            "format": f"tobkiri.python313-native-harness-{variant}/1",
            "outcome": "success",
            "inputs_unchanged": True,
            "production_enabled": False,
            "diagnostic_only": diagnostic,
            "components": {name: member},
            "producer": {"sha256": build["compiler_executable_sha256"]},
            "producer_receipt": {"sha256": build["producer_ci_receipt_sha256"]},
        }
        receipt_path = root / "components-build.json"
        write(receipt_path, receipt)
        binding.verify_producer_selection(artifact, build, plan, identity, diagnostic=diagnostic)
        with self.assertRaisesRegex(ValueError, "producer selection"):
            binding.verify_producer_selection(
                artifact, build, plan, identity, diagnostic=not diagnostic
            )
        with self.assertRaisesRegex(ValueError, "successful producer"):
            binding.verify_producer_selection(
                artifact, build, plan, {**identity, "bytes": 0}, diagnostic=diagnostic
            )
        with self.assertRaisesRegex(ValueError, "successful producer"):
            binding.verify_producer_selection(
                artifact, build, {**plan, "sources": [row, row]}, identity,
                diagnostic=diagnostic,
            )
        write(receipt_path, {**receipt, "outcome": "failed"})
        with self.assertRaisesRegex(ValueError, "producer selection"):
            binding.verify_producer_selection(
                artifact, build, plan, identity, diagnostic=diagnostic
            )

    def test_normal_and_diagnostic_receipts_cannot_be_interchanged(self):
        for diagnostic in (False, True):
            with self.subTest(diagnostic=diagnostic), tempfile.TemporaryDirectory() as temporary:
                self.check_variant(Path(temporary), diagnostic)


if __name__ == "__main__":
    unittest.main()
