"""Check the normal and diagnostic native Harness artifact selections."""

import hashlib
import json
from pathlib import Path
import re
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import harness_native_ci_inputs as binding  # noqa: E402
from build_native import fingerprint  # noqa: E402
from harness_native_dependencies import compress_sources  # noqa: E402


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
                "files": {
                    "provenance.json": {**provenance, "mode": "0o600"},
                    metadata_name: {**metadata_identity, "mode": "0o644"},
                },
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
        provenance_path = directory / "inputs/provenance.json"
        provenance_path.write_bytes(b"[]")
        with self.assertRaisesRegex(ValueError, "successful producer"):
            binding.verify_producer_selection(
                artifact, build, plan, identity, diagnostic=diagnostic
            )
        write(provenance_path, {})
        write(receipt_path, {**receipt, "outcome": "failed"})
        with self.assertRaisesRegex(ValueError, "producer selection"):
            binding.verify_producer_selection(
                artifact, build, plan, identity, diagnostic=diagnostic
            )

    def test_normal_and_diagnostic_receipts_cannot_be_interchanged(self):
        for diagnostic in (False, True):
            with self.subTest(diagnostic=diagnostic), tempfile.TemporaryDirectory() as temporary:
                self.check_variant(Path(temporary), diagnostic)

    def test_only_original_profile_schema_bytes_from_verified_capture_bind(self):
        paths = {**binding.PROFILE_SCHEMA_PATHS, **binding.PROFILE_REFERENCE_PATHS}
        originals = {
            name: (binding.ROOT / "tobkiri_runtime" / name).read_bytes()
            for name in paths.values()
        }
        base = {"python/" + name: raw for name, raw in originals.items()}
        with tempfile.TemporaryDirectory() as temporary:
            inputs = Path(temporary)
            capture = inputs / "python" / binding.CAPTURED_DATA
            capture.parent.mkdir()

            def staged_manifest(files: dict[str, bytes], source=originals) -> list[dict]:
                capture.write_text(f"FILES = {compress_sources(files)!r}\n", encoding="utf-8")
                return [
                    {"source": "python/" + binding.CAPTURED_DATA,
                     "destination": binding.CAPTURED_DATA, **fingerprint(capture)},
                    *({"source": "python/" + name, "destination": name,
                       "sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw)}
                      for name, raw in source.items()),
                ]

            captured = {**originals, "tobkiri_protocol/schemas/profile_v6.schema.json": b"{}"}
            manifest = staged_manifest(captured)
            selected = binding._profile_schema_resources(inputs, base, manifest)
            self.assertEqual(selected, {key: originals[name] for key, name in paths.items()})
            rust = binding._rust_profile_schemas("ab" * 32, selected).decode("ascii")
            self.assertIn(f'component_sha256: "sha256:{"ab" * 32}"', rust)
            def decode(field: str) -> bytes:
                return bytes.fromhex("".join(re.findall(r"\\x([0-9a-f]{2})", field)))

            receipt = binding._profile_schema_receipt(selected)
            self.assertEqual(set(receipt), {"v4", "v5", "references"})
            self.assertEqual(set(receipt["references"]), set(binding.PROFILE_REFERENCE_PATHS))
            for version in binding.PROFILE_SCHEMA_PATHS:
                field = re.search(rf'\b{version}: b"([^"]+)"', rust).group(1)
                self.assertEqual(decode(field), selected[version])
            references = rust.split("    references: Some([\n", 1)[1].split("    ]),", 1)[0]
            self.assertEqual([decode(field) for field in re.findall(r'b"([^"]+)"', references)],
                             [selected["common"], selected["content_projection"]])
            for rows in (receipt, receipt["references"]):
                for key in rows.keys() - {"references"}:
                    self.assertEqual(rows[key]["content"].encode("utf-8"), selected[key])
                    self.assertEqual(rows[key]["sha256"], hashlib.sha256(selected[key]).hexdigest())

            capture.write_bytes(capture.read_bytes() + b"\n")
            with self.assertRaisesRegex(ValueError, "verified bytes"):
                binding._profile_schema_resources(inputs, base, manifest)
            for name in (paths["v4"], paths["common"], paths["content_projection"]):
                with self.subTest(name=name):
                    with self.assertRaisesRegex(ValueError, "schema and reference once"):
                        files = {key: raw for key, raw in captured.items() if key != name}
                        manifest = staged_manifest(files)
                        binding._profile_schema_resources(inputs, base, manifest)
                    with self.assertRaisesRegex(ValueError, "differs from original source"):
                        manifest = staged_manifest({**captured, name: originals[name] + b"\n"})
                        binding._profile_schema_resources(inputs, base, manifest)
            with self.assertRaisesRegex(ValueError, "differs from original source"):
                manifest = staged_manifest(captured)
                manifest[1]["sha256"] = manifest[2]["sha256"]
                binding._profile_schema_resources(inputs, base, manifest)
            with self.assertRaisesRegex(ValueError, r"unexpected \$id"):
                common, projection = paths["common"], paths["content_projection"]
                changed = {**originals, common: originals[projection]}
                manifest = staged_manifest(changed, changed)
                changed_base = {"python/" + name: raw for name, raw in changed.items()}
                binding._profile_schema_resources(inputs, changed_base, manifest)


if __name__ == "__main__":
    unittest.main()
