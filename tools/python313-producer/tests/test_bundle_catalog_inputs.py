"""Actual source capture and inert dependency staging; no runtime or Wasm runs."""

import copy
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import bundle_catalog_inputs as inputs
from bundle_catalog_resources import verify_members
from test_bundle_catalog_resources import fixture

ROOT = Path(__file__).resolve().parents[3]


class BundleInputsTests(unittest.TestCase):
    def test_full_genuine_schema_resources_and_original_module_bodies(self):
        files, modules, _ = inputs.runtime_sources(ROOT, roots=inputs.ROOTS)
        self.assertIn("tobkiri_protocol.validation", modules)
        self.assertIn("tobkiri_protocol.bundle_catalog", modules)
        for record in modules.values():
            self.assertEqual(
                files[record["path"]],
                (ROOT / "tobkiri_runtime" / record["path"]).read_bytes(),
            )
        resources, receipt = inputs._original_schema_resources(ROOT)
        expected = list(
            (ROOT / "tobkiri_runtime/tobkiri_protocol/schemas").glob("*.schema.json")
        )
        self.assertEqual(len(resources), len(expected))
        for path in expected:
            name = path.relative_to(ROOT / "tobkiri_runtime").as_posix()
            raw = path.read_bytes()
            self.assertEqual(resources[name], raw)
            self.assertEqual(
                receipt[name],
                {"bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()},
            )

    def test_both_worlds_preserve_sources_and_reject_changed_boundary_receipt(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            schema, stdlib, rpds = (
                root / name for name in ("schema", "stdlib", "rpds")
            )
            pins = []
            for i in range(5):
                name = f"distribution{i}"
                relative = name + ".dist-info/METADATA"
                raw = b"Metadata-Version: 2.1\n"
                path = schema / "packages" / name / relative
                path.parent.mkdir(parents=True)
                path.write_bytes(raw)
                pins.append(
                    {
                        "distribution": name,
                        "role": "pure-python",
                        "files": {
                            relative: {
                                "bytes": len(raw),
                                "sha256": hashlib.sha256(raw).hexdigest(),
                            }
                        },
                    }
                )
            path = schema / "packages/rpds-py-native/rpds/__init__.py"
            path.parent.mkdir(parents=True)
            path.write_bytes(b"# inert source fixture\n")
            pins.append(
                {
                    "distribution": "rpds-py",
                    "role": "native-oracle",
                    "files": {
                        "rpds/__init__.py": {
                            "bytes": path.stat().st_size,
                            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                        }
                    },
                }
            )
            path = rpds / "work/stage/rpds.abi3.so"
            path.parent.mkdir(parents=True)
            path.write_bytes(b"plain inert source fixture, not an executable extension")
            verified = {
                "inputs": pins,
                "stdlib": {"files": {}},
                "extension": {
                    "bytes": path.stat().st_size,
                    "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                },
            }
            bundle_cases = root / "bundle-cases.json"
            cases = fixture()
            bundle_cases.write_text(json.dumps(cases))
            for native, diagnostic in ((True, False), (False, False), (True, True)):
                with self.subTest(native=native, diagnostic=diagnostic):
                    destination = root / (
                        "diagnostic"
                        if diagnostic
                        else "native"
                        if native
                        else "ordinary"
                    )
                    record = inputs.stage(
                        ROOT,
                        destination,
                        verified,
                        schema,
                        stdlib,
                        rpds,
                        native=native,
                        bundle_cases=bundle_cases,
                        diagnostic_reread=diagnostic,
                    )
                    plan = json.loads((destination / "plan.json").read_bytes())
                    self.assertTrue(plan["stub_wasi"])
                    self.assertEqual(
                        plan["allowed_imports"], [] if native else inputs.CORE_IMPORTS
                    )
                    for name, expected in record["bundle_catalog"][
                        "embedded_resources"
                    ]["members"].items():
                        raw = (destination / "python" / name).read_bytes()
                        self.assertEqual(
                            expected,
                            {
                                "bytes": len(raw),
                                "sha256": hashlib.sha256(raw).hexdigest(),
                            },
                        )
                    self.assertFalse(
                        record["bundle_catalog"]["runtime_config_override_used"]
                    )
                    for name, resource in record["bundle_catalog"][
                        "original_resources"
                    ].items():
                        raw = (destination / "python" / name).read_bytes()
                        self.assertEqual(
                            raw, (ROOT / "tobkiri_runtime" / name).read_bytes()
                        )
                        self.assertEqual(
                            resource["sha256"], hashlib.sha256(raw).hexdigest()
                        )
                    proof = {
                        "source_manifest": [
                            {**item, "bytes": record["files"][item["source"]]["bytes"]}
                            for item in record["source_files"]
                        ],
                        "build_plan_sha256": record["plan_sha256"],
                        "world": plan["world"],
                        "imports": sorted(plan["allowed_imports"]),
                        "exports": sorted(plan["outer_exports"]),
                        "inner": {
                            "imports": sorted(plan["allowed_imports"]),
                            "exports": sorted([*plan["outer_exports"], "exports"]),
                        },
                    }
                    inputs.verify_component(
                        proof, record, native=native, diagnostic_reread=diagnostic
                    )
                    verify_members(
                        proof,
                        record["bundle_catalog"],
                        cases,
                        diagnostic_reread=diagnostic,
                    )
                    changed = copy.deepcopy(proof)
                    changed["source_manifest"][0]["sha256"] = "0" * 64
                    with self.assertRaises(ValueError):
                        inputs.verify_component(
                            changed, record, native=native, diagnostic_reread=diagnostic
                        )
                    changed = copy.deepcopy(proof)
                    changed["imports"].append("unexpected:host/io@1.0.0")
                    with self.assertRaises(ValueError):
                        inputs.verify_component(
                            changed, record, native=native, diagnostic_reread=diagnostic
                        )


if __name__ == "__main__":
    unittest.main()
