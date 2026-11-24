"""Producer plan checks; no component compiler is invoked by these tests."""

import base64
import hashlib
from importlib.metadata import EntryPoint, FileHash, PackagePath
import json
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import build_component
from build_component import (
    _assert_compiler_unchanged,
    _compiler_executable,
    _componentize_commands,
    _installed_tool,
    _tree_manifest,
    _verify_plan,
    destination,
)


FIXTURE = Path(__file__).resolve().parent / "fixtures/build/basic"
PARITY_FIXTURE = Path(__file__).resolve().parent / "fixtures/build/shell-policy"
REPO_ROOT = Path(__file__).resolve().parents[3]


class BuildPlanTests(unittest.TestCase):
    def basic_plan(self, root):
        shutil.copyfile(FIXTURE / "app.py", root / "app.py")
        shutil.copyfile(FIXTURE / "pack.wit", root / "pack.wit")
        return json.loads((FIXTURE / "plan.json").read_text())

    def write_plan(self, root, plan):
        path = root / "plan.json"
        path.write_text(json.dumps(plan), encoding="utf-8")
        return path

    def add_source(self, root, plan, destination_name, content=b"pass\n"):
        source_name = f"extra-{len(plan['sources'])}.py"
        (root / source_name).write_bytes(content)
        plan["sources"].append({
            "source": source_name,
            "destination": destination_name,
            "sha256": hashlib.sha256(content).hexdigest(),
        })

    def test_small_python_pack_plan_captures_entrypoint_and_wit(self):
        plan, raw, plan_hash, captures = _verify_plan(FIXTURE / "plan.json")
        self.assertEqual(plan["world"], "python-pack")
        self.assertEqual(plan["outer_exports"], ["ping"])
        self.assertEqual({str(path) for path, _ in captures}, {"app.py", "wit/pack.wit"})
        self.assertEqual(len(raw), (FIXTURE / "plan.json").stat().st_size)
        self.assertEqual(len(plan_hash), 64)

    def test_plan_cannot_claim_an_export_absent_from_its_wit_world(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            shutil.copyfile(FIXTURE / "app.py", root / "app.py")
            shutil.copyfile(FIXTURE / "pack.wit", root / "pack.wit")
            plan = json.loads((FIXTURE / "plan.json").read_text())
            plan["outer_exports"] = ["ping", "hidden"]
            (root / "plan.json").write_text(json.dumps(plan))
            with self.assertRaisesRegex(ValueError, "exactly match"):
                _verify_plan(root / "plan.json")

    def test_plan_rejects_source_symlink_and_non_normalized_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            shutil.copyfile(FIXTURE / "app.py", root / "app.py")
            shutil.copyfile(FIXTURE / "pack.wit", root / "pack.wit")
            (root / "app-link.py").symlink_to(root / "app.py")
            plan = json.loads((FIXTURE / "plan.json").read_text())
            plan["sources"][0]["source"] = "app-link.py"
            (root / "plan.json").write_text(json.dumps(plan))
            with self.assertRaisesRegex(ValueError, "symlinks"):
                _verify_plan(root / "plan.json")
        for path in ["../app.py", "app//main.py", "app/./main.py", "/tmp/app.py",
                     "C:/app.py", "app\0.py"]:
            with self.assertRaises(ValueError):
                destination(path)

    def test_plan_rejects_case_folded_source_and_destination_collisions(self):
        for alias_kind in ("source", "destination"):
            with self.subTest(alias_kind=alias_kind), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                plan = self.basic_plan(root)
                self.add_source(root, plan, "copy.py")
                plan["sources"][-1][alias_kind] = "APP.py"
                with self.assertRaisesRegex(ValueError, "case-folded"):
                    _verify_plan(self.write_plan(root, plan))

    def test_plan_rejects_case_folded_directories_and_file_directory_collisions(self):
        for destinations in [("package/a.py", "Package/b.py"),
                             ("package", "package/b.py"),
                             ("package/a.py", "package")]:
            with self.subTest(destinations=destinations), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                plan = self.basic_plan(root)
                for name in destinations:
                    self.add_source(root, plan, name)
                with self.assertRaisesRegex(ValueError, "collision"):
                    _verify_plan(self.write_plan(root, plan))

    def test_plan_reserves_generated_paths_under_case_folding(self):
        for name in ["Wit_World/helper.py", "COMPONENTIZE_PY_TYPES.PY",
                     "Outer.Wasm", "outer.wac/nested.py", "Component.Wasm",
                     "No-Ambient-Dependencies/foreign.wasm"]:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                plan = self.basic_plan(root)
                self.add_source(root, plan, name)
                with self.assertRaisesRegex(ValueError, "shadow generated"):
                    _verify_plan(self.write_plan(root, plan))

    def test_plan_rejects_annotated_private_initializer_export(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            plan = self.basic_plan(root)
            content = b"""package local:fixture@1.0.0;
                world python-pack {
                    export ping: func();
                    @since(version = 1.0.0) export exports: func();
                }
            """
            (root / "pack.wit").write_bytes(content)
            plan["sources"][1]["sha256"] = hashlib.sha256(content).hexdigest()
            with self.assertRaisesRegex(ValueError, "annotated"):
                _verify_plan(self.write_plan(root, plan))

    def test_plan_enforces_total_captured_byte_bound(self):
        with mock.patch.object(build_component, "MAX_TOTAL_SOURCE_BYTES", 1):
            with self.assertRaisesRegex(ValueError, "total captured"):
                _verify_plan(FIXTURE / "plan.json")

    def test_generated_bindings_enforce_file_and_cumulative_byte_bounds(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "one.py").write_bytes(b"1234")
            with mock.patch.object(build_component, "MAX_BINDING_FILE_BYTES", 3):
                with self.assertRaisesRegex(ValueError, "file exceeds"):
                    _tree_manifest(root)
            (root / "two.py").write_bytes(b"1234")
            with mock.patch.object(build_component, "MAX_TOTAL_BINDING_BYTES", 7):
                with self.assertRaisesRegex(ValueError, "cumulative"):
                    _tree_manifest(root)

    def test_shell_policy_fixture_pins_original_sources_and_91_case_oracle(self):
        plan, _, _, captures = _verify_plan(PARITY_FIXTURE / "plan.json")
        self.assertEqual(plan["world"], "pack")
        self.assertEqual(
            set(plan["outer_exports"]),
            {"tobkiri:core/lifecycle@1.0.0", "local:shell-policy-python/policy@1.0.0"},
        )
        self.assertEqual(len(captures), 17)
        self.assertEqual(
            {str(path) for path, _ in captures if path.suffix == ".wit"},
            {"wit/pack.wit", "wit/deps/core.wit"},
        )
        bindings_command, component_command = _componentize_commands(
            Path("componentize-py"), plan)
        self.assertEqual(bindings_command[1:3], ["-d", "wit"])
        self.assertEqual(component_command[1:3], ["-d", "wit"])
        self.assertNotIn("wit/pack.wit", bindings_command)
        self.assertNotIn("wit/pack.wit", component_command)

        origins = {
            "policy.py": "tobkiri_runtime/ecosystem/rumi_shell_policy_pack/runtime/policy.py",
            "app.py": "packs/local.shell-policy-python/python/app.py",
            "wit/pack.wit": "packs/local.shell-policy-python/wit/pack.wit",
            # The Pack-local copy is ignored generated output; bind the fixture
            # to the canonical checked-in WIT so clean checkouts are testable.
            "wit/deps/core.wit": "crates/tobkiri-wit/wit/core/core.wit",
        }
        origins.update({
            f"tobkiri_sdk/{path.name}": f"sdk/python/tobkiri_sdk/{path.name}"
            for path in (REPO_ROOT / "sdk/python/tobkiri_sdk").glob("*.py")
        })
        pins = {item["destination"]: item["sha256"] for item in plan["sources"]}
        self.assertEqual(set(pins), set(origins))
        for destination_name, repository_path in origins.items():
            original = (REPO_ROOT / repository_path).read_bytes()
            staged = (PARITY_FIXTURE / destination_name).read_bytes()
            self.assertEqual(staged, original, destination_name)
            self.assertEqual(hashlib.sha256(staged).hexdigest(), pins[destination_name])

        oracle = json.loads((PARITY_FIXTURE / "oracle.json").read_text(encoding="utf-8"))
        corpus_path = REPO_ROOT / oracle["source_file"]
        corpus_bytes = corpus_path.read_bytes()
        self.assertEqual(hashlib.sha256(corpus_bytes).hexdigest(), oracle["corpus_sha256"])
        self.assertEqual(len(corpus_bytes), oracle["bytes"])
        pack_manifest = json.loads(
            (REPO_ROOT / "packs/local.shell-policy-python/pack.json").read_text(
                encoding="utf-8"))
        frozen_entry = next(
            item for item in pack_manifest["files"]
            if item["path"] == "tests/inspection.json"
        )
        self.assertEqual(frozen_entry["sha256"], f"sha256:{oracle['corpus_sha256']}")
        self.assertEqual(frozen_entry["size"], oracle["bytes"])
        corpus = json.loads(corpus_bytes)
        cases = corpus["cases"]
        self.assertEqual(oracle["case_count"], 91)
        self.assertEqual((oracle["first_case_index"], oracle["last_case_index"]), (0, 90))
        self.assertEqual(corpus["source_sha256"], oracle["policy_source_sha256"])
        self.assertEqual(corpus["handler"], oracle["handler"])
        self.assertEqual(oracle["case_identity"],
                         "inspection.json#/cases/{zero_based_array_index}")
        self.assertEqual(
            {name: sum(case["input"]["operation"] == name for case in cases)
             for name in ("classify", "tokenize")},
            oracle["operations"],
        )
        self.assertEqual(
            {"ok": sum("ok" in case["expect"] for case in cases),
             "error_class:ValueError": sum(
                 case["expect"].get("error_class") == "ValueError" for case in cases
             )},
            oracle["outcomes"],
        )

    def test_wit_dependency_must_be_inside_the_captured_package_directory(self):
        for destination_name in ["outside/core.wit", "wit/extra/core.wit",
                                 "wit/deps/core/nested/core.wit"]:
            with self.subTest(destination=destination_name), tempfile.TemporaryDirectory() as directory:
                temporary_fixture = Path(directory) / "shell-policy"
                shutil.copytree(PARITY_FIXTURE, temporary_fixture)
                plan_path = temporary_fixture / "plan.json"
                plan = json.loads(plan_path.read_text(encoding="utf-8"))
                dependency = next(
                    item for item in plan["sources"]
                    if item["destination"] == "wit/deps/core.wit"
                )
                dependency["destination"] = destination_name
                plan_path.write_text(json.dumps(plan), encoding="utf-8")
                with self.assertRaisesRegex(ValueError, "not among the captured"):
                    _verify_plan(plan_path)

    def test_plan_rejects_unmodeled_encoded_wit_dependencies(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            plan = self.basic_plan(root)
            self.add_source(root, plan, "wit/deps/hidden.wasm")
            with self.assertRaisesRegex(ValueError, "encoded WIT"):
                _verify_plan(self.write_plan(root, plan))


class InstalledToolTests(unittest.TestCase):
    def fake_tool(self, root, extension_name="componentize_py.abi3.so"):
        tag = "cp39-abi3-macosx_11_0_arm64"
        metadata_dir = "componentize_py-0.25.0.dist-info"
        metadata = {"WHEEL": f"Tag: {tag}\n"}
        contents = {
            f"componentize_py/{extension_name}": b"extension",
            f"{metadata_dir}/WHEEL": metadata["WHEEL"].encode(),
            f"{metadata_dir}/RECORD": b"record",
        }
        files = []
        for name, content in contents.items():
            path = root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
            item = PackagePath(name)
            item.size = len(content)
            digest = base64.urlsafe_b64encode(hashlib.sha256(content).digest())
            item.hash = (None if name.endswith("/RECORD") else
                         FileHash(f"sha256={digest.rstrip(b'=').decode()}"))
            files.append(item)
        dist = mock.Mock()
        dist.version = "0.25.0"
        dist.files = files
        dist.locate_file.side_effect = lambda item: root / str(item)
        dist.read_text.side_effect = metadata.get
        dist.entry_points = [EntryPoint(
            name="componentize-py", value="componentize_py:script",
            group="console_scripts")]
        lock = {"tools": {"componentize-py": {
            "version": "0.25.0", "wheels": {tag: {
                "filename": "componentize_py-0.25.0-test.whl", "sha256": "a" * 64,
            }},
        }}}
        return dist, lock, metadata

    def inspect_tool(self, dist, lock):
        with (mock.patch.object(build_component, "distribution", return_value=dist),
              mock.patch.object(build_component, "version", return_value="0.25.0")):
            return _installed_tool("componentize-py", "0.25.0", lock, None)

    def test_record_consistency_and_extension_paths_do_not_claim_archive_authentication(self):
        for extension in ["componentize_py.abi3.so", "componentize_py.pyd"]:
            with self.subTest(extension=extension), tempfile.TemporaryDirectory() as directory:
                dist, lock, metadata = self.fake_tool(Path(directory), extension)
                metadata["direct_url.json"] = json.dumps({
                    "archive_info": {"hashes": {"sha256": "a" * 64}},
                })
                result = self.inspect_tool(dist, lock)
                self.assertTrue(result["installed_record_consistency_verified"])
                self.assertEqual(result["extension_path"], f"componentize_py/{extension}")
                self.assertFalse(result["wheel"]["archive_sha256_verified"])
                self.assertIsNone(result["wheel"]["archive_sha256"])
                self.assertFalse(result["installed_tree_authenticated_by_archive"])
                self.assertEqual(len(result["installed_record_unhashed_files"]), 1)

    def test_record_rejects_modified_installed_files_and_missing_hashes(self):
        for modification in ("hash", "size", "missing_hash"):
            with self.subTest(modification=modification), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                dist, lock, _ = self.fake_tool(root)
                extension = root / str(dist.files[0])
                if modification == "missing_hash":
                    dist.files[0].hash = None
                else:
                    extension.write_bytes(b"tampering" if modification == "hash" else b"x")
                with self.assertRaisesRegex(ValueError, "RECORD"):
                    self.inspect_tool(dist, lock)

    def test_console_launcher_is_selected_from_installed_scripts_metadata(self):
        for platform_name, filename in [("darwin", "componentize-py"),
                                        ("win32", "componentize-py.exe")]:
            with self.subTest(platform=platform_name), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                dist, _, _ = self.fake_tool(root)
                launcher = root / filename
                launcher.write_bytes(b"launcher")
                dist.files.append(PackagePath(filename))
                with (mock.patch.object(build_component, "distribution", return_value=dist),
                      mock.patch.object(build_component.sys, "platform", platform_name),
                      mock.patch.object(build_component.sysconfig, "get_path", return_value=str(root))):
                    self.assertEqual(_compiler_executable(), launcher)
                    dist.files.pop()
                    with self.assertRaisesRegex(ValueError, "installed manifest"):
                        _compiler_executable()

    def test_compiler_postrun_hash_rejects_mutation_and_symlink_swap(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            compiler = root / "componentize-py"
            compiler.write_bytes(b"before")
            digest = hashlib.sha256(compiler.read_bytes()).hexdigest()
            _assert_compiler_unchanged(compiler, digest)
            compiler.write_bytes(b"after")
            with self.assertRaisesRegex(ValueError, "changed during"):
                _assert_compiler_unchanged(compiler, digest)
            compiler.unlink()
            (root / "original").write_bytes(b"before")
            compiler.symlink_to(root / "original")
            with self.assertRaisesRegex(ValueError, "changed during"):
                _assert_compiler_unchanged(compiler, digest)


if __name__ == "__main__":
    unittest.main()
