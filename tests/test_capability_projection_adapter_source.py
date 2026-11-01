"""Source-only guards for the capability projection component boundary."""

import ast
import hashlib
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
PACK = ROOT / "packs/local.harness-capability-projection-python"


def _module(path: Path) -> tuple[str, ast.Module]:
    """Read and parse a source file without importing canonical runtime code."""
    source = path.read_text(encoding="utf-8")
    return source, ast.parse(source, filename=str(path))


def _function(tree: ast.Module, name: str) -> ast.FunctionDef:
    """Return one top-level function by name."""
    functions = [
        node for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == name
    ]
    if len(functions) != 1:
        raise AssertionError(f"expected one top-level {name} function")
    return functions[0]


class CapabilityProjectionAdapterSourceTests(unittest.TestCase):
    """Check delegation, schema preservation, and the minimal typed boundary."""

    def test_private_validator_preserves_prechange_bf8_ast_and_public_export(self) -> None:
        directory = ROOT / "tobkiri_runtime/ecosystem/defaultspack/defaultspack"
        _, private = _module(directory / "_public_view_input.py")
        function = _function(private, "validate_public_input")
        reserved = next(node for node in private.body if isinstance(node, ast.Assign)
                        and isinstance(node.targets[0], ast.Name)
                        and node.targets[0].id == "_RESERVED")
        # Fixed hashes of the real bf8 function and constant, not rewritten code.
        self.assertEqual(hashlib.sha256(ast.dump(function, include_attributes=False).encode()).hexdigest(),
                         "f4bb344fa058d5306855dfced62b65bb25c7135bc52c1fe477ea2e80af56d377")
        self.assertEqual(hashlib.sha256(ast.dump(reserved, include_attributes=False).encode()).hexdigest(),
                         "01b9e0c225cd7ef41d55e520a631da9b038d3c7f5179990953106a2214ceadaf")
        public_source, public = _module(directory / "v4_view_contract.py")
        self.assertFalse(any(isinstance(n, ast.FunctionDef) and n.name == "validate_public_input"
                             for n in public.body))
        exports = next(n for n in public.body if isinstance(n, ast.ImportFrom)
                       and n.module == "_public_view_input")
        self.assertIn(("validate_public_input", "validate_public_input"),
                      [(n.name, n.asname) for n in exports.names])
        self.assertIn('_SCHEMA_PATH.read_text("utf-8")', public_source)
        dynamic_source, _ = _module(directory / "http_dynamic_targets.py")
        self.assertIn("from ._public_view_input import validate_public_input", dynamic_source)
        self.assertNotIn(".v4_view_contract import", dynamic_source)

    def test_adapter_imports_original_records_and_calls_original_function_once(self) -> None:
        _, tree = _module(PACK / "python/projection_adapter.py")
        imports = {
            node.module: {alias.name for alias in node.names}
            for node in tree.body
            if isinstance(node, ast.ImportFrom)
        }
        self.assertEqual(
            imports.get("core_runtime.global_contracts.http_contract_dispatch"),
            {"HTTPContractBinding", "HTTPContractTarget"},
        )
        self.assertEqual(
            imports.get("ecosystem.defaultspack.defaultspack.http_dynamic_targets"),
            {"defaultspack_dynamic_capability_targets"},
        )

        projection = _function(tree, "project")
        original_calls = [
            node for node in ast.walk(projection)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "defaultspack_dynamic_capability_targets"
        ]
        self.assertEqual(len(original_calls), 1)
        self.assertEqual([item.arg for item in original_calls[0].keywords], ["catalog"])
        self.assertIsInstance(original_calls[0].args[0], ast.Name)
        self.assertEqual(original_calls[0].args[0].id, "binding")
        self.assertEqual(
            [node.name for node in tree.body if isinstance(node, ast.FunctionDef)],
            [
                "_invalid_request",
                "_object_without_duplicates",
                "_reject_nonfinite_number",
                "_parse_finite_float",
                "_is_exact_string_fields",
                "_decode_request",
                "_target_data",
                "project",
            ],
        )

    def test_adapter_has_strict_bounded_input_and_preserves_schema_bytes_as_text(self) -> None:
        source, tree = _module(PACK / "python/projection_adapter.py")
        assignments = {
            node.targets[0].id: node.value
            for node in tree.body
            if isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
        }
        expected_bound = ast.parse("4 * 1024 * 1024", mode="eval").body
        self.assertEqual(ast.dump(assignments["MAX_PAYLOAD_BYTES"]), ast.dump(expected_bound))
        self.assertEqual(ast.dump(assignments["MAX_OUTPUT_BYTES"]), ast.dump(expected_bound))
        self.assertIn("object_pairs_hook=_object_without_duplicates", source)
        self.assertIn("parse_constant=_reject_nonfinite_number", source)
        self.assertIn("parse_float=_parse_finite_float", source)
        self.assertIn('target.input_schema.decode("utf-8")', source)
        self.assertIn('"allowed_payload_keys": sorted(target.allowed_payload_keys)', source)
        self.assertIn('value.keys() != _ENVELOPE_FIELDS', source)
        self.assertIn('binding_data.keys() != _BINDING_FIELDS', source)
        self.assertIn('target_data, _TARGET_FIELDS', source)
        self.assertIn('"invalid-request"', source)
        self.assertIn('"projection-failed"', source)

    def test_output_contains_all_original_target_data_fields(self) -> None:
        _, tree = _module(PACK / "python/projection_adapter.py")
        target_data = _function(tree, "_target_data")
        returned = next(
            node.value for node in target_data.body if isinstance(node, ast.Return)
        )
        self.assertIsInstance(returned, ast.Dict)
        keys = [ast.literal_eval(key) for key in returned.keys]
        self.assertEqual(keys, [
            "contribution_id",
            "contract_id",
            "operation_id",
            "provider_id",
            "function_id",
            "allowed_payload_keys",
            "owner_pack_id",
            "artifact_digest",
            "input_schema",
            "read_only",
        ])

    def test_component_wrapper_and_wit_export_only_the_projection_interface(self) -> None:
        source, tree = _module(PACK / "python/app.py")
        self.assertIn("from projection_adapter import ProjectionError, project", source)
        self.assertIn("ErrorCode.INVALID", source)
        self.assertIn("ErrorCode.INTERNAL", source)
        classes = {node.name: node for node in tree.body if isinstance(node, ast.ClassDef)}
        self.assertEqual(set(classes), {"Lifecycle", "Projection"})
        projection_methods = {
            node.name for node in classes["Projection"].body
            if isinstance(node, ast.FunctionDef)
        }
        self.assertEqual(projection_methods, {"project"})

        wit = (PACK / "wit/pack.wit").read_text(encoding="utf-8")
        self.assertIn("package local:harness-capability-projection-python@1.0.0;", wit)
        self.assertIn("project: func(payload: string) -> result<string, error>;", wit)
        self.assertIn("include tobkiri:core/pack-minimal@1.0.0;", wit)
        self.assertIn("export projection;", wit)
        self.assertNotIn("export network", wit)
        self.assertNotIn("export fs", wit)
        self.assertNotIn("export secrets", wit)


if __name__ == "__main__":
    unittest.main()
