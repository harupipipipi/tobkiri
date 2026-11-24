"""Source-only staging tests; no original runtime, wheel or extension executes."""

import ast
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))

import component_inputs  # noqa: E402
import build_components  # noqa: E402
import harness_native_dependencies as dependencies  # noqa: E402
from fetch_sdk import inventory  # noqa: E402
from schema_probe.captured_loader import Tree  # noqa: E402


def identity(raw):
    return {"bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}


def fixture(root):
    """Tiny synthetic dependency files used only as inert source DATA."""
    schema, stdlib, rpds = (root / name for name in ("schema", "stdlib", "rpds"))
    pins = []
    for name in (
        "attrs",
        "jsonschema",
        "jsonschema-specifications",
        "referencing",
        "packaging",
        "rpds-py",
    ):
        native = name == "rpds-py"
        package = "rpds" if native else name.replace("-", "_")
        files = {package + "/__init__.py": b"# inert dependency fixture\n"}
        if not native:
            files[name + "-1.dist-info/METADATA"] = (
                "Name: " + name + "\nVersion: 1\n\n"
            ).encode()
        else:
            files[package + "/native.so"] = b"excluded native platform extension"
        base = schema / "packages" / ("rpds-py-native" if native else name)
        for relative, raw in files.items():
            target = base / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(raw)
        pins.append(
            {
                "distribution": name,
                "role": "native-oracle" if native else "pure",
                "files": {path: identity(raw) for path, raw in files.items()},
            }
        )
    original_stdlib = b"# inert original stdlib source fixture\n"
    target = stdlib / "source/subprocess.py"
    target.parent.mkdir(parents=True)
    target.write_bytes(original_stdlib)
    extension = rpds / "work/stage/rpds.abi3.so"
    extension.parent.mkdir(parents=True)
    extension.write_bytes(b"inert synthetic Wasm extension; never imported")
    verified = {
        "inputs": pins,
        "stdlib": {"files": {"subprocess.py": identity(original_stdlib)}},
        "extension": identity(extension.read_bytes()),
        "predecessors": {},
        "rpds_head": dependencies.build_schema.RPDS_HEAD,
        "ui_clock_variant": False,
    }
    return schema, stdlib, rpds, verified


class NativeHarnessDependencyTests(unittest.TestCase):
    def test_optional_trio_is_atomic_and_native_only(self):
        value = Path("/input")
        for enabled in (True, False):
            self.assertFalse(dependencies.selected(enabled, None, None, None))
        self.assertTrue(dependencies.selected(True, value, value, value))
        for enabled, paths in (
            (False, (value, value, value)),
            (True, (value, None, None)),
            (True, (value, value, None)),
            (True, (None, None, value)),
        ):
            with (
                self.subTest(enabled=enabled, paths=paths),
                self.assertRaises(ValueError),
            ):
                dependencies.selected(enabled, *paths)
        with (
            patch.object(build_components.sys, "platform", "linux"),
            patch.dict(
                build_components.os.environ,
                {"GITHUB_ACTIONS": "true"},
            ),
            patch.object(Path, "mkdir", side_effect=AssertionError("unexpected IO")),
        ):
            with self.assertRaisesRegex(ValueError, "--harness-native"):
                build_components.run(
                    None,
                    value,
                    value,
                    value,
                    all_five=True,
                    schema_inputs=value,
                    stdlib=value,
                    rpds=value,
                )

    def test_existing_verifier_is_reused_without_clock_variant_opt_in(self):
        roots = [
            Path("/") / name
            for name in ("schema", "producer", "rpds", "validation", "stdlib")
        ]
        with (
            patch.object(
                dependencies.build_schema, "verify_inputs", return_value={"pinned": {}}
            ) as verify,
            patch.object(
                dependencies.runtime_inputs,
                "report",
                side_effect=[{"inputs": ["wheel pins"]}, {"files": {}}],
            ),
            patch.object(
                dependencies, "fingerprint", return_value={"sha256": "actual"}
            ),
        ):
            result = dependencies.verify(*roots)
        verify.assert_called_once_with(*roots)
        self.assertFalse(result["ui_clock_variant"])
        self.assertEqual(
            result["rpds_head"], "53163ed815c62c4853bbed3d8a082c0764e3084f"
        )

    def test_source_plan_preserves_all_originals_and_captures_every_late_module(self):
        original_plans = component_inputs.plans(True)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            stage = root / "native"
            original = component_inputs.stage_harness_native(HERE.parents[1], stage)
            before = {name: (stage / name).read_bytes() for name in original["files"]}
            schema, stdlib, rpds, verified = fixture(root)
            result = dependencies.stage(stage, original, verified, schema, stdlib, rpds)
            plan = json.loads((stage / "plan.json").read_bytes())
            previous = sys.path[:]
            try:
                sys.path.insert(0, str(HERE.parents[1] / "sdk/python"))
                import build_component as sdk

                checked, _raw, plan_hash, captured = sdk._verify_plan(
                    stage / "plan.json"
                )
            finally:
                sys.path[:] = previous
            self.assertEqual(checked, plan)
            self.assertEqual(plan_hash, result["plan_sha256"])
            self.assertEqual(len(captured), len(plan["sources"]))
            self.assertEqual(plan["entrypoint"], "harness_native_entry")
            self.assertEqual(plan["allowed_imports"], dependencies.IMPORTS)
            self.assertEqual(plan["outer_exports"], dependencies.EXPORTS)
            for name, raw in before.items():
                if name != "plan.json":
                    self.assertEqual((stage / name).read_bytes(), raw, name)
            self.assertEqual(result["files"], inventory(stage))
            self.assertEqual(
                result["plan_sha256"],
                identity(
                    (stage / "plan.json").read_bytes(),
                )["sha256"],
            )
            data = ast.parse(
                (stage / "python/harness_native_dependency_data.py").read_bytes()
            )
            self.assertTrue(all(isinstance(node, ast.Assign) for node in data.body))
            values = {
                node.targets[0].id: ast.literal_eval(node.value) for node in data.body
            }
            self.assertIs(type(values["FILES"]), tuple)
            tree = Tree(values["FILES"], compressed=True)
            files = tree.files
            self.assertEqual(
                values["NAMESPACES"],
                ("ecosystem", "ecosystem.defaultspack", "harness_native"),
            )
            for name in (
                "ecosystem/__init__.py",
                "ecosystem/defaultspack/__init__.py",
                "harness_native/__init__.py",
            ):
                self.assertNotIn(name, files)
                self.assertFalse((stage / "python" / name).exists())
            for name, raw in before.items():
                if name.startswith("python/"):
                    self.assertEqual(files[name.removeprefix("python/")], raw)
            self.assertIn("plistlib", values["STDLIB_PREIMPORTS"])
            self.assertIn("subprocess", values["STDLIB_PREIMPORTS"])
            self.assertNotIn("ctypes", values["STDLIB_PREIMPORTS"])
            self.assertNotIn("rpds/native.so", files)
            self.assertNotIn("rpds/rpds.abi3.so", files)
            self.assertEqual(
                (stage / "python/rpds/rpds.abi3.so").read_bytes(),
                (rpds / "work/stage/rpds.abi3.so").read_bytes(),
            )
            proof = json.loads((stage / "python" / dependencies.METADATA).read_bytes())
            storage = proof["source_storage"]
            self.assertFalse(storage["expanded_member_cache"])
            self.assertFalse(storage["code_cache"])
            self.assertFalse(storage["decompression_after_restore_proven"])
            self.assertFalse(storage["artifact_size_within_limit_proven"])
            self.assertLess(
                storage["stored_payload_bytes"], proof["captured_source_bytes"]
            )
            self.assertGreater(
                tree.accounted_bytes, proof["captured_raw_admission_bytes"]
            )
            for name, codec, payload, length, digest in values["FILES"]:
                self.assertIn(codec, ("identity", "zlib"))
                self.assertEqual(
                    identity(files[name]), {"bytes": length, "sha256": digest}
                )
            self.assertEqual(proof["original_module_count"], 68)
            self.assertEqual(proof["original_schema_count"], 42)
            for flag in (
                "dependency_closure_complete",
                "all_seventeen_methods_compatible",
                "component_init_performed",
                "native_imports_connected",
                "production_enabled",
            ):
                self.assertFalse(proof[flag])
            self.assertEqual(
                proof["unprovided_external_dependencies"], ["cryptography"]
            )
            for item in plan["sources"]:
                self.assertEqual(
                    identity((stage / item["source"]).read_bytes())["sha256"],
                    item["sha256"],
                )
            manifest = [
                {
                    "source": item["source"],
                    "destination": item["destination"],
                    "sha256": item["sha256"],
                    "bytes": result["files"][item["source"]]["bytes"],
                }
                for item in plan["sources"]
            ]
            receipt = {
                "source_manifest": manifest,
                "source_manifest_sha256": hashlib.sha256(
                    json.dumps(
                        manifest,
                        sort_keys=True,
                        separators=(",", ":"),
                        ensure_ascii=False,
                    ).encode()
                ).hexdigest(),
                "build_plan_sha256": result["plan_sha256"],
                "world": "harness-native",
                "imports": sorted(dependencies.IMPORTS),
                "exports": sorted(dependencies.EXPORTS),
                "inner": {
                    "imports": sorted(dependencies.IMPORTS),
                    "exports": sorted([*dependencies.EXPORTS, "exports"]),
                },
                "harness_native_dependencies": proof,
            }
            dependencies.verify_component_receipt(receipt, result, stage)
            for field, value in (
                ("source_manifest_sha256", "forged"),
                ("imports", dependencies.IMPORTS + ["unexpected:io/host@1.0.0"]),
                ("exports", dependencies.EXPORTS[:1]),
            ):
                with self.subTest(field=field), self.assertRaises(ValueError):
                    dependencies.verify_component_receipt(
                        {**receipt, field: value},
                        result,
                        stage,
                    )
        self.assertEqual(component_inputs.plans(True), original_plans)

    def test_tampered_dependency_and_non_native_plan_fail_before_extension_staging(
        self,
    ):
        for changed in ("wheel", "boundary"):
            with (
                self.subTest(changed=changed),
                tempfile.TemporaryDirectory() as temporary,
            ):
                root = Path(temporary).resolve()
                stage = root / "native"
                original = component_inputs.stage_harness_native(HERE.parents[1], stage)
                schema, stdlib, rpds, verified = fixture(root)
                if changed == "wheel":
                    (schema / "packages/attrs/attrs/__init__.py").write_bytes(b"forged")
                else:
                    plan = json.loads((stage / "plan.json").read_bytes())
                    plan["world"] = "other"
                    (stage / "plan.json").write_text(json.dumps(plan))
                    original["files"] = inventory(stage)
                    original["plan_sha256"] = identity(
                        (stage / "plan.json").read_bytes()
                    )["sha256"]
                with self.assertRaises(ValueError):
                    dependencies.stage(stage, original, verified, schema, stdlib, rpds)
                self.assertFalse((stage / "python/rpds/rpds.abi3.so").exists())


if __name__ == "__main__":
    unittest.main()
