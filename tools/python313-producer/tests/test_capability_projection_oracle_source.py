"""Source-only guards for the pinned CI capability projection oracle."""

import ast
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[3]
ORACLE = ROOT / "tools/python313-producer/capability_projection_oracle.py"


def _oracle() -> tuple[str, ast.Module]:
    """Parse the CLI source without importing it or canonical runtime modules."""
    source = ORACLE.read_text(encoding="utf-8")
    return source, ast.parse(source, filename=str(ORACLE))


def _function(tree: ast.Module, name: str) -> ast.FunctionDef:
    """Return one top-level function by name."""
    functions = [
        node for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == name
    ]
    if len(functions) != 1:
        raise AssertionError(f"expected one top-level {name} function")
    return functions[0]


class CapabilityProjectionOracleSourceTests(unittest.TestCase):
    """Guard CI-only loading, fixed provenance, and honest oracle boundaries."""

    def test_cli_refuses_non_linux_or_non_github_actions_before_import(self) -> None:
        source, tree = _oracle()
        require_ci = _function(tree, "_require_ci")
        self.assertIn("sys.platform !=", ast.unparse(require_ci))
        self.assertIn('"linux"', source)
        self.assertIn('"GITHUB_ACTIONS") != "true"', source)

        main = _function(tree, "main")
        guarded_calls = [
            node for statement in main.body if isinstance(statement, ast.Try)
            for node in ast.walk(statement)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id in {
                "_require_ci", "_physical_source", "_new_output",
                "_verify_source_hashes", "_load_original",
            }
        ]
        self.assertEqual(
            [node.func.id for node in guarded_calls],
            [
                "_require_ci", "_physical_source", "_new_output",
                "_verify_source_hashes", "_load_original",
            ],
        )
        self.assertIn("Linux GitHub Actions", source)

    def test_pinned_commit_hashes_match_the_prechange_sources(self) -> None:
        _, tree = _oracle()
        assignments = {
            node.targets[0].id: node.value
            for node in tree.body
            if isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
        }
        self.assertEqual(
            ast.literal_eval(assignments["ORACLE_COMMIT"]),
            "bf8cf14664580b7ca226b56d7bd5a311a19f44b4",
        )
        self.assertEqual(ast.literal_eval(assignments["_SOURCE_HASHES"]), {
            "ecosystem/defaultspack/defaultspack/http_dynamic_targets.py": (
                "0e147f3a9ac3abe33ce92590b402d712c3b155c99228d91ea05169af3517f4e5"
            ),
            "ecosystem/defaultspack/defaultspack/v4_view_contract.py": (
                "8df8a3c9ddb0eda98d1573201bee22f4487baf6b69fab9b9c673afa93359f76e"
            ),
            "core_runtime/global_contracts/http_contract_dispatch.py": (
                "d8be5fe03201c7fd35169bad5914339beceadc8fea2d7a4467ed959f878e99e2"
            ),
        })

    def test_actual_import_is_dynamic_from_verified_source_and_never_adapter(self) -> None:
        source, tree = _oracle()
        imports = [
            node for node in tree.body
            if isinstance(node, (ast.Import, ast.ImportFrom))
        ]
        self.assertFalse(any(
            isinstance(node, ast.ImportFrom)
            and node.module
            and node.module.startswith(("core_runtime", "ecosystem"))
            for node in imports
        ))
        self.assertNotIn("projection_adapter", source)

        loader = ast.unparse(_function(tree, "_load_original"))
        self.assertIn("sys.path.insert(0, str(source))", loader)
        self.assertIn("importlib.import_module", loader)
        self.assertIn("HTTPContractBinding", loader)
        self.assertIn("HTTPContractTarget", loader)
        self.assertIn("defaultspack_dynamic_capability_targets", loader)
        self.assertIn("_verify_source_hashes(source)", ast.unparse(_function(tree, "main")))

    def test_cases_are_original_data_comparisons_and_boundary_errors_are_marked(self) -> None:
        source, tree = _oracle()
        target_data = _function(tree, "_target_data")
        returned = next(
            node.value for node in target_data.body if isinstance(node, ast.Return)
        )
        self.assertIsInstance(returned, ast.Dict)
        fields = [ast.literal_eval(key) for key in returned.keys]
        self.assertEqual(fields, [
            "contribution_id", "contract_id", "operation_id", "provider_id",
            "function_id", "allowed_payload_keys", "owner_pack_id", "artifact_digest",
            "input_schema", "read_only",
        ])
        target_source = ast.unparse(target_data)
        self.assertIn("target.input_schema.decode", target_source)
        self.assertIn("sorted(target.allowed_payload_keys)", target_source)

        self.assertIn('"unicode_schema_bytes"', source)
        self.assertIn('"explicit_exact_target_suppresses_catalog_alias"', source)
        self.assertIn('"duplicate_contribution_id_rejects_all"', source)
        self.assertIn('"external_schema_reference_uses_original_fallback"', source)
        self.assertIn('"approved_integer_one_is_not_true"', source)
        self.assertIn('"enabled_integer_one_is_not_true"', source)
        self.assertIn('"schema_with_more_than_64_properties_uses_original_fallback"', source)
        self.assertIn('"different_path_returns_no_dynamic_targets"', source)
        self.assertIn('"expected_error": "invalid-request"', source)
        self.assertIn('"oracle_scope": _BOUNDARY_ONLY_SCOPE', source)
        self.assertIn("not generated by the original factory", source)
        self.assertIn('"format": OUTPUT_FORMAT', source)
        self.assertIn('"oracle_commit": ORACLE_COMMIT', source)
        self.assertIn('"source_files": source_hashes', source)
        self.assertIn('"cases": cases', source)
        self.assertIn("MAX_CASE_BYTES = 4 * 1024 * 1024", source)


if __name__ == "__main__":
    unittest.main()
