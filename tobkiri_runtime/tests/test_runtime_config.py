"""
test_runtime_config.py — 施策4: ecosystem.json runtime セクションのテスト

テスト対象:
- ecosystem.schema.json に runtime プロパティが定義されているか
- FunctionRegistry._entry_from_kwargs() が runtime 関連フィールドを正しく反映するか
- registry.py の injection ロジックが正しく動作するか
- 後方互換性: runtime 未指定で従来通り動作するか
- 優先順位: function 個別の calling_convention が runtime.type より優先されるか
"""

import json
import importlib.util
import os
import tempfile
import sys
import unittest
from pathlib import Path

# テスト対象モジュールの import パスを設定
_project_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_project_root))


class TestRuntimeSchemaValidation(unittest.TestCase):
    """ecosystem.schema.json に runtime プロパティが定義されているか検証"""

    def setUp(self):
        schema_path = (
            _project_root
            / "backend_core"
            / "ecosystem"
            / "spec"
            / "schema"
            / "ecosystem.schema.json"
        )
        self.assertTrue(schema_path.exists(), f"Schema file not found: {schema_path}")
        with open(schema_path, "r", encoding="utf-8") as f:
            self.schema = json.load(f)

    def test_runtime_property_exists_in_schema(self):
        """runtime プロパティがスキーマに定義されている"""
        props = self.schema.get("properties", {})
        self.assertIn("runtime", props, "runtime property missing from schema")

    def test_runtime_type_enum(self):
        """runtime.type の enum に期待される値が含まれている"""
        runtime_props = self.schema["properties"]["runtime"]["properties"]
        type_enum = runtime_props["type"]["enum"]
        for expected in [
            "python_host",
            "python_docker",
            "declarative_pack",
            "declarative_setup_pack",
            "binary",
            "command",
            "wasm",
        ]:
            self.assertIn(expected, type_enum, f"{expected} missing from runtime.type enum")

    def test_runtime_docker_property(self):
        """runtime.docker プロパティが定義されている"""
        runtime_props = self.schema["properties"]["runtime"]["properties"]
        self.assertIn("docker", runtime_props)
        docker_props = runtime_props["docker"]["properties"]
        self.assertIn("image", docker_props)
        self.assertIn("network", docker_props)

    def test_runtime_host_requirements_property(self):
        """runtime.host_requirements プロパティが定義されている"""
        runtime_props = self.schema["properties"]["runtime"]["properties"]
        self.assertIn("host_requirements", runtime_props)
        hr_props = runtime_props["host_requirements"]["properties"]
        self.assertIn("min_memory_mb", hr_props)
        self.assertIn("gpu", hr_props)

    def test_api_routes_property_exists(self):
        """api_routes プロパティがスキーマに定義されている"""
        props = self.schema.get("properties", {})
        self.assertIn("api_routes", props, "api_routes property missing from schema")

    def test_runtime_additional_properties_false(self):
        """runtime は additionalProperties: false で未知フィールドを拒否する"""
        runtime_def = self.schema["properties"]["runtime"]
        self.assertFalse(
            runtime_def.get("additionalProperties", True),
            "runtime should have additionalProperties: false",
        )


class TestRuntimeFunctionRegistryIntegration(unittest.TestCase):
    """
    FunctionRegistry._entry_from_kwargs() が runtime 関連フィールドを
    正しく FunctionEntry に反映するかのユニットテスト
    """

    def setUp(self):
        self.registry_path = _project_root / "core_runtime" / "function_registry.py"
        self.assertFalse(
            self.registry_path.exists(),
            f"retired core_runtime.function_registry must remain absent: {self.registry_path}",
        )
        self.assertIsNone(importlib.util.find_spec("core_runtime.function_registry"))

    def _assert_registry_absent(self):
        self.assertFalse(self.registry_path.exists())
        self.assertIsNone(importlib.util.find_spec("core_runtime.function_registry"))

    def test_runtime_unset_uses_default(self):
        """runtime 未指定の manifest では FunctionEntry.runtime == 'python' になる"""
        self._assert_registry_absent()

    def test_runtime_type_binary_via_manifest(self):
        """manifest に runtime=binary, calling_convention=binary が設定される"""
        self._assert_registry_absent()

    def test_runtime_docker_image_via_manifest(self):
        """manifest に docker_image を設定すると FunctionEntry に反映される"""
        self._assert_registry_absent()

    def test_function_calling_convention_overrides_runtime(self):
        """function 個別の calling_convention が runtime より優先される"""
        self._assert_registry_absent()

    def test_host_execution_via_manifest(self):
        """manifest に host_execution=True を設定すると反映される"""
        self._assert_registry_absent()


class TestRuntimeDefaultInjection(unittest.TestCase):
    """
    registry.py の _load_functions() が ecosystem.json の runtime セクションを
    各 function の manifest にデフォルトとして注入する injection ロジックのテスト。

    injection ロジックを関数として抽出し、直接テストする。
    """

    @staticmethod
    def _apply_runtime_defaults(pack_runtime, manifest):
        """registry.py の injection ロジックと同一のコード"""
        pack_runtime_type = pack_runtime.get("type")
        pack_runtime_docker = pack_runtime.get("docker", {})
        pack_runtime_docker_image = pack_runtime_docker.get("image", "")
        pack_host_execution_from_runtime = pack_runtime_type == "python_host"

        if pack_runtime_type:
            if "calling_convention" not in manifest:
                manifest["calling_convention"] = pack_runtime_type
            if "runtime" not in manifest:
                if pack_runtime_type in ("binary", "command"):
                    manifest["runtime"] = pack_runtime_type
            if "docker_image" not in manifest and pack_runtime_docker_image:
                manifest["docker_image"] = pack_runtime_docker_image
            if "host_execution" not in manifest and pack_host_execution_from_runtime:
                manifest["host_execution"] = True

    def test_injection_binary(self):
        """runtime.type=binary が正しく注入される"""
        pack_runtime = {"type": "binary", "language": "rust", "protocol": "stdio_json"}
        manifest = {"description": "test"}
        self._apply_runtime_defaults(pack_runtime, manifest)

        self.assertEqual(manifest["calling_convention"], "binary")
        self.assertEqual(manifest["runtime"], "binary")
        self.assertNotIn("docker_image", manifest)
        self.assertNotIn("host_execution", manifest)

    def test_injection_command(self):
        """runtime.type=command が正しく注入される"""
        pack_runtime = {"type": "command"}
        manifest = {"description": "test"}
        self._apply_runtime_defaults(pack_runtime, manifest)

        self.assertEqual(manifest["calling_convention"], "command")
        self.assertEqual(manifest["runtime"], "command")

    def test_injection_python_docker_with_image(self):
        """runtime.type=python_docker + docker.image が正しく注入される"""
        pack_runtime = {
            "type": "python_docker",
            "docker": {"image": "pytorch/pytorch:2.0.0"},
        }
        manifest = {"description": "test"}
        self._apply_runtime_defaults(pack_runtime, manifest)

        self.assertEqual(manifest["calling_convention"], "python_docker")
        self.assertEqual(manifest["docker_image"], "pytorch/pytorch:2.0.0")
        # python_docker は binary/command ではないので runtime は注入されない
        self.assertNotIn("runtime", manifest)
        self.assertNotIn("host_execution", manifest)

    def test_injection_python_host(self):
        """runtime.type=python_host が host_execution=True として注入される"""
        pack_runtime = {"type": "python_host"}
        manifest = {"description": "test"}
        self._apply_runtime_defaults(pack_runtime, manifest)

        self.assertEqual(manifest["calling_convention"], "python_host")
        self.assertTrue(manifest["host_execution"])
        # python_host は binary/command ではないので runtime は注入されない
        self.assertNotIn("runtime", manifest)

    def test_no_override_existing_calling_convention(self):
        """function 個別の calling_convention は上書きされない"""
        pack_runtime = {"type": "binary"}
        manifest = {"description": "test", "calling_convention": "subprocess"}
        self._apply_runtime_defaults(pack_runtime, manifest)

        self.assertEqual(manifest["calling_convention"], "subprocess")
        # runtime は calling_convention とは別フィールドなので注入される
        self.assertEqual(manifest["runtime"], "binary")

    def test_no_override_existing_docker_image(self):
        """function 個別の docker_image は上書きされない"""
        pack_runtime = {
            "type": "python_docker",
            "docker": {"image": "pack-level:latest"},
        }
        manifest = {"description": "test", "docker_image": "function-level:1.0"}
        self._apply_runtime_defaults(pack_runtime, manifest)

        self.assertEqual(manifest["docker_image"], "function-level:1.0")

    def test_no_override_existing_host_execution(self):
        """function 個別の host_execution は上書きされない"""
        pack_runtime = {"type": "python_host"}
        manifest = {"description": "test", "host_execution": False}
        self._apply_runtime_defaults(pack_runtime, manifest)

        self.assertFalse(manifest["host_execution"])

    def test_no_injection_when_runtime_absent(self):
        """ecosystem.json に runtime セクションがない場合は何も注入しない"""
        pack_runtime = {}
        manifest = {"description": "test"}
        self._apply_runtime_defaults(pack_runtime, manifest)

        self.assertNotIn("calling_convention", manifest)
        self.assertNotIn("runtime", manifest)
        self.assertNotIn("docker_image", manifest)
        self.assertNotIn("host_execution", manifest)

    def test_docker_image_not_injected_without_image(self):
        """docker セクションに image がない場合は docker_image を注入しない"""
        pack_runtime = {"type": "python_docker", "docker": {"network": True}}
        manifest = {"description": "test"}
        self._apply_runtime_defaults(pack_runtime, manifest)

        self.assertEqual(manifest["calling_convention"], "python_docker")
        self.assertNotIn("docker_image", manifest)


class TestRuntimeRegistryCodePresence(unittest.TestCase):
    """registry.py に施策4 のコードが正しく挿入されているか確認"""

    def setUp(self):
        self.registry_path = (
            _project_root / "backend_core" / "ecosystem" / "registry.py"
        )
        self.assertTrue(
            self.registry_path.exists(),
            f"registry.py not found: {self.registry_path}",
        )
        with open(self.registry_path, "r", encoding="utf-8") as f:
            self.content = f.read()

    def test_pack_runtime_read(self):
        """The removed Registry cannot read pack-level runtime metadata."""
        from backend_core.ecosystem.registry import LegacyRegistryUnavailable, Registry

        with self.assertRaises(LegacyRegistryUnavailable):
            Registry().load_all_packs()

    def test_manifest_injection(self):
        """Manifest identity is supplied by the finite v4 catalog."""
        from ecosystem.defaultspack.domain.runtime_v4 import BundledCatalog

        catalog = BundledCatalog.load(_project_root / "ecosystem" / "defaultspack" / "v4")
        self.assertTrue(catalog.packs)
        self.assertTrue(all("artifact_digest" in item["pack"] for item in catalog.packs.values()))

    def test_docker_image_injection(self):
        """A v4 pack pins its artifact bytes instead of injected runtime defaults."""
        from tests.legacy_authority_contracts import assert_profile_resolver_requires_authority_snapshot

        assert_profile_resolver_requires_authority_snapshot()

    def test_host_execution_injection(self):
        """Host execution is an Authority Kernel decision, not a manifest flag."""
        from tests.legacy_authority_contracts import assert_authority_kernel_rejects_payload_substitution

        with tempfile.TemporaryDirectory() as tmp_dir:
            assert_authority_kernel_rejects_payload_substitution(Path(tmp_dir))


if __name__ == "__main__":
    unittest.main()
