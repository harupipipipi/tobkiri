"""Actual source capture and inert dependency staging; no runtime or Wasm runs."""

import ast
import copy
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import bundle_catalog_inputs as inputs
import bundle_catalog_kernel_inputs as kernel_inputs
from bundle_catalog_resources import verify_members
from test_bundle_catalog_resources import fixture

ROOT = Path(__file__).resolve().parents[3]


class BundleInputsTests(unittest.TestCase):
    def test_embedded_identity_fast_path_keeps_json_types_and_order(self):
        adapter = ROOT / inputs.PACK / 'python/bundle_catalog_adapter.py'
        syntax = ast.parse(adapter.read_bytes())
        functions = ast.Module(body=[node for node in syntax.body
                                    if isinstance(node, ast.FunctionDef)
                                    and node.name in {'identity_text', '_matches_source_identity'}],
                               type_ignores=[])
        scope = {'json': json}
        exec(compile(functions, str(adapter), 'exec'), scope)
        matches = scope['_matches_source_identity']
        canonical = scope['identity_text']
        observed = fixture()['source_identity']
        reordered = {key: observed[key] for key in reversed(observed)}
        self.assertTrue(matches(observed, reordered))
        for field, alternate in [('index', False), ('index', 0.0),
                                 ('reread', 0), ('bytes', float(observed['reads'][0]['bytes']))]:
            expected = copy.deepcopy(observed)
            expected['reads'][0][field] = alternate
            with self.subTest(field=field, alternate=alternate):
                self.assertEqual(observed, expected)
                self.assertFalse(matches(observed, expected))
                self.assertNotEqual(canonical(observed), canonical(expected))
        expected = copy.deepcopy(observed)
        expected['reads'].reverse()
        self.assertFalse(matches(observed, expected))
        self.assertNotEqual(canonical(observed), canonical(expected))

    def test_cold_phase_derivation_preserves_the_original_store_call_and_output(self):
        source = (inputs.HERE / 'bundle_catalog/native.py').read_bytes()
        derived = kernel_inputs._observe_cold_call(source)

        def cold_branch(raw: bytes) -> ast.If:
            return next(node for node in ast.walk(ast.parse(raw))
                        if isinstance(node, ast.If)
                        and isinstance(node.test, ast.Compare)
                        and ast.literal_eval(node.test.comparators[0])
                        == 'tobkiri.cold-original-schema-store/1')

        before, after = cold_branch(source), cold_branch(derived)
        self.assertEqual(ast.dump(before.test), ast.dump(after.test))
        self.assertEqual(ast.dump(before.body[0]), ast.dump(after.body[0]))
        self.assertEqual(ast.unparse(after.body[1]), '_phases.begin_cold_schema_store()')
        guarded_call = after.body[2]
        self.assertIsInstance(guarded_call, ast.Try)
        self.assertEqual([ast.dump(node) for node in guarded_call.body],
                         [ast.dump(before.body[1])])
        self.assertEqual(ast.unparse(guarded_call.finalbody),
                         '_phases.end_cold_schema_store()')
        self.assertEqual([ast.dump(node) for node in before.body[2:]],
                         [ast.dump(node) for node in after.body[3:]])
        with self.assertRaisesRegex(ValueError, 'exact original App schema phase'):
            kernel_inputs._observe_cold_call(derived)

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
