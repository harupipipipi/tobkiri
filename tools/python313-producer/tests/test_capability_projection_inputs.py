"""Source staging and scope checks; no original runtime or component executes."""
import copy
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import capability_projection_inputs as inputs

ROOT = Path(__file__).resolve().parents[3]


class ProjectionInputsTests(unittest.TestCase):
    def test_complete_real_module_bodies_and_parents_are_captured_without_ui_io(self):
        files, modules, namespaces = inputs.runtime_sources(ROOT)
        self.assertIn("ecosystem.defaultspack.defaultspack._public_view_input", modules)
        self.assertNotIn("ecosystem.defaultspack.defaultspack.v4_view_contract", modules)
        self.assertIn("core_runtime.global_contracts", modules)
        self.assertIn("tobkiri_protocol", modules)
        self.assertIn("ecosystem", namespaces)
        for item in modules.values():
            raw = (ROOT / "tobkiri_runtime" / item["path"]).read_bytes()
            self.assertEqual(files[item["path"]], raw)
            self.assertEqual(item["sha256"], hashlib.sha256(raw).hexdigest())

    def test_two_new_worlds_have_same_adapter_source_and_closed_boundaries(self):
        # Inert source fixture exercises staging only. Never an ABI/extension proof.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            schema, stdlib, rpds = (root / name for name in ("schema", "stdlib", "rpds"))
            pins = []
            for i in range(5):
                name = f"distribution{i}"
                relative = name + ".dist-info/METADATA"
                raw = b"Metadata-Version: 2.1\n"
                p = schema / "packages" / name / relative
                p.parent.mkdir(parents=True)
                p.write_bytes(raw)
                pins.append({"distribution": name, "role": "pure-python", "files": {
                    relative: {"bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}}})
            p = schema / "packages/rpds-py-native/rpds/__init__.py"
            p.parent.mkdir(parents=True)
            p.write_bytes(b"# inert source fixture\n")
            pins.append({"distribution": "rpds-py", "role": "native-oracle", "files": {
                "rpds/__init__.py": {"bytes": p.stat().st_size,
                                     "sha256": hashlib.sha256(p.read_bytes()).hexdigest()}}})
            p = rpds / "work/stage/rpds.abi3.so"
            p.parent.mkdir(parents=True)
            p.write_bytes(b"not an executable extension: source-only fixture")
            verified = {"inputs": pins, "stdlib": {"files": {}}, "extension": {
                "bytes": p.stat().st_size, "sha256": hashlib.sha256(p.read_bytes()).hexdigest()}}
            records = []
            for native in (True, False):
                target = root / ("native" if native else "ordinary")
                record = inputs.stage(ROOT, target, verified, schema, stdlib, rpds, native=native)
                plan = json.loads((target / "plan.json").read_bytes())
                self.assertEqual(plan["allowed_imports"], [] if native else inputs.CORE_IMPORTS)
                self.assertTrue(plan["stub_wasi"])
                proof = {"source_manifest": [{**i, "bytes": record["files"][i["source"]]["bytes"]}
                                             for i in record["source_files"]],
                         "build_plan_sha256": record["plan_sha256"], "world": plan["world"],
                         "imports": sorted(plan["allowed_imports"]), "exports": sorted(plan["outer_exports"]),
                         "inner": {"imports": sorted(plan["allowed_imports"]),
                                   "exports": sorted([*plan["outer_exports"], "exports"])}}
                inputs.verify_component(proof, record, native=native)
                forged = copy.deepcopy(proof)
                forged["source_manifest"][0]["sha256"] = "0" * 64
                with self.assertRaisesRegex(ValueError, "source or typed boundary"):
                    inputs.verify_component(forged, record, native=native)
                records.append(record)
            self.assertEqual(records[0]["capability_projection"]["adapter_sha256"],
                             records[1]["capability_projection"]["adapter_sha256"])


if __name__ == "__main__":
    unittest.main()
