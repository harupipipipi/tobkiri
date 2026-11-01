"""Source-only custody checks for the Defaultspack nativeKit capsule."""

from __future__ import annotations

import ast
import hashlib
import json
from pathlib import Path
import stat
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts/wasm"))

import prepare_harness_native as prepare  # noqa: E402 - explicit source-tool path.


class HarnessNativeSourceTests(unittest.TestCase):
    """Check exact original bytes and explicitly bounded capsule scope."""

    def test_record_relocation_preserves_class_bytes_and_public_names(self) -> None:
        files = prepare.captured()
        provenance = json.loads(files["provenance.json"])
        # These hashes bind the complete definitions before this ownership move.
        expected_hashes = {
            "ProfileReconfirmationRequired": (
                "632da5e4e94847f63aeaa367449c3a106a039eb25c862fb0648b6d57a60edc50"
            ),
            "ActivationLockTimeout": (
                "d8b83bb8f7be9e907bc87163e7e786471cf6a335ff5ef85c98303fe2c57f1357"
            ),
            "ArtifactVerificationTimeout": (
                "8a376400961452552d7741410b9612a7a05febe4ac7667934afcf73543bda8ce"
            ),
            "ActiveDefaultProfile": (
                "2b5951da3f6ce3b048cc0021143c0cf384dc5da2795d1c08956f5b1c8c8664d6"
            ),
        }
        source = files[f"source/{prepare.PROFILE_RUNTIME_RECORDS}"]
        self.assertEqual(
            source, prepare.bounded_read(ROOT / prepare.PROFILE_RUNTIME_RECORDS),
        )
        records = [item for item in provenance["definitions"]
                   if item["source_path"] == prepare.PROFILE_RUNTIME_RECORDS]
        self.assertEqual({item["symbol"] for item in records}, set(expected_hashes))
        for record in records:
            exact = source[record["source_start_byte"]:record["source_end_byte"]]
            self.assertEqual(exact, files[record["stored_as"]])
            self.assertEqual(
                hashlib.sha256(exact).hexdigest(), expected_hashes[record["symbol"]],
            )
            self.assertEqual(
                record["source_module_sha256"], hashlib.sha256(source).hexdigest(),
            )
        tree = ast.parse(source)
        self.assertEqual(
            [node.name for node in tree.body if isinstance(node, ast.ClassDef)],
            list(prepare.SERVICE_DATA_CLASSES),
        )
        assignments = {node.targets[0].id: node.value for node in tree.body
                       if isinstance(node, ast.Assign) and len(node.targets) == 1
                       and isinstance(node.targets[0], ast.Name)}
        self.assertEqual(ast.literal_eval(assignments["_LEGACY_MODULE"]),
                         "ecosystem.defaultspack.domain.runtime_v4.service")
        self.assertEqual([node.id for node in assignments["_OWNED_DEFINITIONS"].elts],
                         list(prepare.SERVICE_DATA_CLASSES))
        loop, = [node for node in tree.body if isinstance(node, ast.For)]
        self.assertEqual(loop.iter.id, "_OWNED_DEFINITIONS")
        self.assertEqual(ast.dump(loop.body[0], include_attributes=False), ast.dump(
            ast.parse("_definition.__module__ = _LEGACY_MODULE").body[0],
            include_attributes=False,
        ))
        relocation = provenance["profile_runtime_record_relocation"]
        self.assertEqual(
            relocation["canonical_source"], prepare.PROFILE_RUNTIME_RECORDS,
        )
        self.assertEqual(
            relocation["previous_definition_source"], prepare.SERVICE_SOURCE,
        )
        self.assertFalse(relocation["class_bodies_rewritten"])
        self.assertFalse(relocation["aliases_executed"])
        self.assertIn("not execution evidence", relocation["source_byte_evidence"])

    def test_value_graph_and_whole_wit_are_captured_without_execution_or_authority(self) -> None:
        files = prepare.captured()
        provenance = json.loads(files["provenance.json"])
        transport = provenance["original_value_transport"]
        for key, path in (("guest_registry", prepare.VALUE_GRAPH_TRANSPORT),
                          ("wit", prepare.NATIVE_WIT),
                          ("native_binding_wit", prepare.NATIVE_BINDINGS_WIT)):
            self.assertEqual(transport[key], path)
            raw = prepare.bounded_read(ROOT / path)
            self.assertEqual(files[f"source/{path}"], raw)
            record, = [r for r in provenance["sources"] if r["path"] == path]
            self.assertEqual(record["sha256"], hashlib.sha256(raw).hexdigest())
        for key in ("original_classes_from_trusted_bootstrap_only",
                    "shared_identity_is_not_value_memoization",
                    "input_ownership_is_explicit"):
            self.assertIs(transport[key], True)
        for key in ("native_binding_refs_confer_authority", "wasm_execution_performed",
                    "whole_export_dispatch_connected", "actual_component_export_dispatch_executed",
                    "original_module_closure_connected", "exception_fields_disclosed",
                    "exception_reference_is_restoration_proof", "native_binding_imports_connected"):
            self.assertIs(transport[key], False)
        self.assertEqual(transport["dispatcher_sources"], list(prepare.DISPATCHER_SOURCES))
        for path in prepare.DISPATCHER_SOURCES:
            self.assertEqual(files[f"source/{path}"], prepare.bounded_read(ROOT / path))

    def test_exception_transport_pins_guest_and_rust_without_a_native_python_dependency(self) -> None:
        files = prepare.captured()
        provenance = json.loads(files["provenance.json"])
        transport = provenance["exception_transport"]
        self.assertEqual(transport["format"], "tobkiri.native-exception/1")
        self.assertEqual(transport["guest_materializer"], prepare.EXCEPTION_TRANSPORT)
        self.assertEqual(
            files[f"source/{prepare.EXCEPTION_TRANSPORT}"],
            prepare.bounded_read(ROOT / prepare.EXCEPTION_TRANSPORT),
        )
        records = {item["path"]: item for item in provenance["sources"]}
        for path in prepare._NATIVE_KIT_SOURCES:
            self.assertEqual(records[path]["role"], "native_exception_codec_source")
            self.assertEqual(records[path]["sha256"], hashlib.sha256(
                prepare.bounded_read(ROOT / path)).hexdigest())
            self.assertNotIn(f"source/{path}", files)
        for path in prepare._NATIVE_IO_SOURCES:
            self.assertEqual(records[path]["role"], "native_inspection_adapter_source")
            self.assertEqual(records[path]["sha256"], hashlib.sha256(
                prepare.bounded_read(ROOT / path)).hexdigest())
            self.assertNotIn(f"source/{path}", files)
        self.assertEqual(provenance["native_inspection_boundary"]["rust_adapter_sources"],
                         list(prepare._NATIVE_IO_SOURCES))
        self.assertIs(provenance["native_inspection_boundary"][
            "existing_fd_protocol_adapter_captured"], True)
        for key in ("actual_core_source_controller_connected", "fd_close_lifecycle_connected"):
            self.assertIs(provenance["native_inspection_boundary"][key], False)
        for path in prepare._NATIVE_IO_TEST_SOURCES:
            self.assertEqual(records[path]["role"], "native_inspection_adapter_test_source")
            self.assertEqual(records[path]["sha256"], hashlib.sha256(
                prepare.bounded_read(ROOT / path)).hexdigest())
            self.assertNotIn(f"source/{path}", files)
        for key in ("native_python_runtime_required", "arbitrary_native_classes_supported",
                    "host_path_fields_supported", "native_inspection_ports_connected",
                    "guest_materializer_executed_in_wasm"):
            self.assertIs(transport[key], False)

    def test_all_data_methods_and_resolution_definitions_keep_original_bytes(self) -> None:
        files = prepare.captured()
        provenance = json.loads(files["provenance.json"])
        source_records = {item["path"]: item for item in provenance["sources"]}
        definitions = provenance["definitions"]

        self.assertEqual(provenance["selected_methods"], list(prepare.METHODS))
        self.assertEqual(len(prepare.METHODS), 19)
        self.assertEqual(len(prepare._EXPECTED_RUNTIME_CLASS_METHODS),
                         len(set(prepare._EXPECTED_RUNTIME_CLASS_METHODS)))
        self.assertEqual(provenance["method_rewrites"], [])
        self.assertEqual(provenance["format"],
                         "io.tobkiri.harness-native-source-capsule.v1")

        for method in prepare.METHODS:
            record, = [item for item in definitions
                       if item["source_path"] == prepare.COMPOSITION
                       and item["symbol"] == method]
            original = prepare.bounded_read(ROOT / prepare.COMPOSITION)
            exact = original[record["source_start_byte"]:record["source_end_byte"]]
            self.assertEqual(
                files[record["stored_as"]], exact, msg=f"method bytes changed: {method}",
            )
            self.assertEqual(hashlib.sha256(exact).hexdigest(), record["sha256"])
            self.assertEqual(record["source_module_sha256"],
                             source_records[prepare.COMPOSITION]["sha256"])

        self.assertEqual(provenance["selected_runtime_module_definitions"], [
            "defaultspack_profile_bundle_root", "defaultspack_host_resource_root",
        ])
        original = prepare.bounded_read(ROOT / prepare.COMPOSITION)
        for helper in prepare._SELECTED_RUNTIME_MODULE_DEFINITIONS:
            record, = [item for item in definitions
                       if item["source_path"] == prepare.COMPOSITION
                       and item["symbol"] == helper]
            exact = original[record["source_start_byte"]:record["source_end_byte"]]
            self.assertEqual(files[record["stored_as"]], exact)
            self.assertEqual(hashlib.sha256(exact).hexdigest(), record["sha256"])
            self.assertEqual(ast.parse(exact).body[0].name, helper)
        getters = provenance["native_root_getters"]
        self.assertEqual(getters["result"], "opaque original NativePath reference")
        for key in ("getter_execution_proven", "final_parent_path_proven",
                    "subsequent_resource_access_proven"):
            self.assertIs(getters[key], False)

        decision, = [item for item in definitions
                     if item["symbol"] == "SetupActivationDecision"]
        decision_source = prepare.bounded_read(ROOT / prepare.PROFILE_PORT)
        self.assertEqual(
            files[decision["stored_as"]],
            decision_source[decision["source_start_byte"]:decision["source_end_byte"]],
        )

        resolution = f"source/{prepare.PROFILE_RESOLUTION}"
        resolution_source = prepare.bounded_read(ROOT / prepare.PROFILE_RESOLUTION)
        self.assertEqual(files[resolution], resolution_source)
        resolution_definitions = [item for item in definitions
                                  if item["source_path"] == prepare.PROFILE_RESOLUTION]
        self.assertEqual(
            [item["symbol"] for item in resolution_definitions],
            list(prepare._RESOLUTION_DEFINITIONS),
        )
        self.assertEqual(len(resolution_definitions), 13)
        for record in resolution_definitions:
            exact = resolution_source[
                record["source_start_byte"]:record["source_end_byte"]
            ]
            self.assertEqual(files[record["stored_as"]], resolution_source)
            self.assertEqual(hashlib.sha256(exact).hexdigest(), record["sha256"])

        self.assertEqual(
            files[f"source/{prepare.SETUP_CONTRACT}"],
            prepare.bounded_read(ROOT / prepare.SETUP_CONTRACT),
        )
        for record in provenance["sources"]:
            if record.get("stored_as"):
                raw = prepare.bounded_read(ROOT / record["path"])
                self.assertEqual(files[record["stored_as"]], raw)
                self.assertEqual(hashlib.sha256(raw).hexdigest(), record["sha256"])

    def test_catalog_and_dynamic_helpers_remain_verbatim_with_deferred_adapters(self) -> None:
        files = prepare.captured()
        provenance = json.loads(files["provenance.json"])
        composition_source = prepare.bounded_read(ROOT / prepare.COMPOSITION)
        records = {item["symbol"]: item for item in provenance["definitions"]
                   if item["source_path"] == prepare.COMPOSITION}

        for method in ("load_catalog", "catalog_with_packs", "catalog_with_profiles",
                       "resolve_profile", "dynamic_profile_edges"):
            record = records[method]
            exact = composition_source[
                record["source_start_byte"]:record["source_end_byte"]
            ]
            self.assertEqual(files[record["stored_as"]], exact)

        load_catalog = files[records["load_catalog"]["stored_as"]]
        catalog_with_packs = files[records["catalog_with_packs"]["stored_as"]]
        self.assertIn(b"BundledCatalog.load(root)", load_catalog)
        self.assertIn(b"load_verified_pack_executable_catalog", catalog_with_packs)
        adapters = {}
        for item in provenance["deferred_native_data_adapters"]:
            methods = item.get("methods", [item.get("method")])
            for method in methods:
                adapters[method] = item["required_behavior"]
        self.assertIn("original reachpoints", adapters["load_catalog"])
        self.assertIn("without prefetching", adapters["load_catalog"])
        self.assertIn("native-established finite DATA facade",
                      adapters["catalog_with_packs"])
        self.assertIn("do not stub or no-op",
                      adapters["catalog_with_packs"])
        self.assertIn("never substitute a synthetic catalog type",
                      adapters["setup_profile_preview"])
        captured_requirements = {
            item["module"]: item["required_behavior"]
            for item in provenance["captured_input_data_requirements"]
        }
        self.assertIn("no guest-selected ambient OS path",
                      captured_requirements[prepare.PROFILE_RESOLUTION])
        self.assertIn("reread and occurrence order",
                      captured_requirements[
                          f"{prepare.PROTOCOL_ROOT}/bundle_catalog.py"
                      ])

    def test_effectful_native_methods_are_excluded_and_no_wasm_claim_is_made(self) -> None:
        files = prepare.captured()
        provenance = json.loads(files["provenance.json"])
        names = set(files)
        self.assertEqual(provenance["excluded_runtime_methods"], [
            "activation_store",
            "is_reconfirmation_required", "is_resolution_denied",
            "is_activation_lock_timeout", "active_profile",
        ])
        self.assertEqual(provenance["excluded_runtime_module_definitions"], [
            "install_defaultspack_profile_runtime",
        ])
        for path in (prepare.COMPOSITION, prepare.PROFILE_PORT,
                     prepare.SERVICE_SOURCE, prepare.SERVICE_INITIALIZER,
                     *prepare.NATIVE_IMPORT_CUSTODY_SOURCES):
            self.assertIn(f"source/{path}", names)
            self.assertEqual(files[f"source/{path}"], prepare.bounded_read(ROOT / path))
        closure_paths = {record["path"] for record in provenance["sources"]}
        self.assertIn("tobkiri_runtime/tobkiri_host/artifact_compiler.py", closure_paths)
        self.assertIn("tobkiri_runtime/tobkiri_protocol/secure_persistence.py", closure_paths)
        self.assertEqual({path for path in names if path.endswith(".wit")},
                         {f"source/{prepare.NATIVE_WIT}", f"source/{prepare.NATIVE_BINDINGS_WIT}"})
        self.assertFalse(any(path.endswith(".wasm") for path in names))
        self.assertNotIn("plan.json", names)
        self.assertNotIn("build-plan.json", names)
        self.assertFalse(provenance["build_performed"])
        self.assertFalse(provenance["wasm_execution_performed"])
        self.assertFalse(provenance["production_enabled"])
        self.assertFalse(provenance["whole_paths_proof"])
        self.assertFalse(provenance["native_authority_module_created"])
        self.assertFalse(provenance["capsule_is_wasm_evidence"])

        for record in provenance["excluded_definitions"]:
            original = prepare.bounded_read(ROOT / record["source_path"])
            exact = original[record["source_start_byte"]:record["source_end_byte"]]
            self.assertEqual(hashlib.sha256(exact).hexdigest(), record["sha256"])
            self.assertNotIn("stored_as", record)
            self.assertEqual(record["source_module_sha256"],
                             next(item["sha256"] for item in provenance["sources"]
                                  if item["path"] == record["source_path"]))

        for path in prepare._DEFERRED_NATIVE_MODULES:
            record, = [item for item in provenance["sources"] if item["path"] == path]
            if path in prepare.PROJECTION_BOUNDARY_MODULES:
                self.assertEqual(record["role"], "verbatim_data_with_native_io_boundary")
                self.assertEqual(files[record["stored_as"]], prepare.bounded_read(ROOT / path))
            elif path == prepare.SERVICE_SOURCE:
                self.assertEqual(record["role"], "original_runtime_module")
                self.assertEqual(files[record["stored_as"]], prepare.bounded_read(ROOT / path))
            else:
                self.assertEqual(record["role"], "native_import_original_source")
                self.assertEqual(files[record["stored_as"]], prepare.bounded_read(ROOT / path))

    def test_metadata_hash_and_schema_inventory_bind_all_fixed_source_bytes(self) -> None:
        files = prepare.captured()
        provenance = json.loads(files["provenance.json"])
        metadata_digest = provenance.pop("metadata_sha256")
        self.assertEqual(
            hashlib.sha256(prepare._canonical_json_bytes(provenance)).hexdigest(),
            metadata_digest,
        )
        schema_records = [item for item in provenance["sources"]
                          if item["role"] == "protocol_schema"]
        actual_names = {Path(item["path"]).name for item in schema_records}
        schema_index = json.loads(files[f"source/{prepare.SCHEMA_HASHES}"])
        self.assertEqual(actual_names, set(schema_index["files"]))
        for item in schema_records:
            self.assertEqual(
                schema_index["files"][Path(item["path"]).name],
                "sha256:" + item["sha256"],
            )
        self.assertIn("protocol_schema_metadata", {
            item["role"] for item in provenance["sources"]
        })
        self.assertIn("project_metadata", {
            item["role"] for item in provenance["sources"]
        })

    def test_native_import_inventory_hash_pins_unported_projection_adapters(self) -> None:
        files = prepare.captured()
        provenance = json.loads(files["provenance.json"])
        imports = provenance["native_imports"]
        self.assertEqual(
            sorted(prepare._native_import_signature(item) for item in imports),
            sorted(prepare._EXPECTED_NATIVE_IMPORTS),
        )
        source_records = {item["path"]: item for item in provenance["sources"]}
        for item in imports:
            record = source_records[item["source_module_path"]]
            if item["source_module_path"] in prepare.PROJECTION_BOUNDARY_MODULES:
                self.assertEqual(record["role"], "verbatim_data_with_native_io_boundary")
                self.assertEqual(files[record["stored_as"]], prepare.bounded_read(
                    ROOT / item["source_module_path"]))
            elif item["source_module_path"] == prepare.SERVICE_SOURCE:
                self.assertEqual(record["role"], "original_runtime_module")
                self.assertEqual(files[record["stored_as"]], prepare.bounded_read(
                    ROOT / item["source_module_path"]))
            else:
                self.assertEqual(record["role"], "native_import_original_source")
                self.assertEqual(files[record["stored_as"]], prepare.bounded_read(
                    ROOT / item["source_module_path"]))

        adapters = {
            item.get("function"): item
            for item in provenance["deferred_native_data_adapters"]
            if item.get("function")
        }
        self.assertEqual(set(adapters), {
            "resolve_profile_projection", "selected_projection_roots",
            "admitted_projection_sources",
        })
        self.assertIn("original guest DATA", adapters["resolve_profile_projection"]
                      ["required_behavior"])
        self.assertIn("each original occurrence", adapters["selected_projection_roots"]
                      ["required_behavior"])
        self.assertIn("duplicate selection", adapters["admitted_projection_sources"]
                      ["required_behavior"])

        boundary = provenance["native_inspection_boundary"]
        self.assertEqual(boundary["projection_sources"],
                         list(prepare.PROJECTION_BOUNDARY_MODULES))
        self.assertTrue(boundary["guest_data_calculations_preserved"])
        self.assertFalse(boundary["native_precomputed_projection_results_allowed"])
        self.assertTrue(boundary["preserve_rereads_duplicate_occurrences_and_failure_order"])
        self.assertFalse(boundary["inspection_bindings_connected"])
        self.assertFalse(boundary["authority_granted_by_source_capture"])
        self.assertFalse(provenance["native_projection_io_ported"])
        self.assertFalse(provenance["native_pack_admission_ported"])
        self.assertFalse(provenance["dependency_closure_executable"])

    def test_package_identity_uses_only_genuine_package_initializers(self) -> None:
        files = prepare.captured()
        domain_init = f"source/{prepare.DOMAIN_PACKAGE_INIT}"
        defaults_init = f"source/{prepare.DEFAULTSPACK_PACKAGE_INIT}"
        self.assertEqual(files[domain_init], b"")
        self.assertEqual(
            files[defaults_init],
            prepare.bounded_read(ROOT / prepare.DEFAULTSPACK_PACKAGE_INIT),
        )
        self.assertNotIn("source/tobkiri_runtime/ecosystem/__init__.py", files)
        self.assertNotIn(
            "source/tobkiri_runtime/ecosystem/defaultspack/__init__.py", files,
        )

    def test_nested_lazy_native_imports_fail_closed(self) -> None:
        composition = prepare.bounded_read(ROOT / prepare.COMPOSITION)
        resolution = prepare.bounded_read(ROOT / prepare.PROFILE_RESOLUTION)
        self.assertEqual(
            sorted(prepare._native_import_signature(item)
                   for item in prepare.validate_native_import_sources(
                       composition, resolution,
                   )),
            sorted(prepare._EXPECTED_NATIVE_IMPORTS),
        )

        method_marker = b"        if not isinstance(catalog, BundledCatalog):"
        method_injected = (
            b"        def _hidden_lazy_native_import():\n"
            b"            from core_runtime.hidden_profile_source import load\n\n"
            + method_marker
        )
        changed_composition = composition.replace(method_marker, method_injected, 1)
        self.assertNotEqual(changed_composition, composition)
        with self.assertRaisesRegex(ValueError, "undeclared native core_runtime/service import"):
            prepare.validate_native_import_sources(changed_composition, resolution)

        resolver_marker = b"    from core_runtime.profile_content_projection import ("
        resolver_injected = (
            b"    def _hidden_lazy_native_import():\n"
            b"        from core_runtime.hidden_projection_source import load\n\n"
            + resolver_marker
        )
        changed_resolution = resolution.replace(resolver_marker, resolver_injected, 1)
        self.assertNotEqual(changed_resolution, resolution)
        with self.assertRaisesRegex(ValueError, "undeclared native core_runtime/service import"):
            prepare.validate_native_import_sources(composition, changed_resolution)

    def test_stage_is_fresh_private_and_source_changes_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary).resolve() / "capsule"
            files = prepare.captured()
            metadata = prepare.stage(output)
            self.assertTrue(output.is_dir())
            self.assertEqual(stat.S_IMODE(output.stat().st_mode), 0o700)
            self.assertEqual(json.loads((output / "provenance.json").read_bytes()), metadata)
            for relative, content in files.items():
                staged = output / relative
                self.assertEqual(staged.read_bytes(), content)
                self.assertEqual(stat.S_IMODE(staged.stat().st_mode), 0o600)
            before = {path: (output / path).read_bytes() for path in files}
            with self.assertRaises(FileExistsError):
                prepare.stage(output)
            self.assertEqual(
                {path: (output / path).read_bytes() for path in files}, before,
            )

        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary).resolve() / "must-not-publish"
            original_read = prepare.bounded_read
            changed_path = ROOT / prepare.COMPOSITION
            calls = 0

            def change_on_second_capture(path: Path, limit: int = 8 * 1024 * 1024) -> bytes:
                nonlocal calls
                raw = original_read(path, limit)
                if Path(path) == changed_path:
                    calls += 1
                    if calls >= 2:
                        return raw + b"\n"
                return raw

            with patch.object(prepare, "bounded_read", side_effect=change_on_second_capture):
                with self.assertRaisesRegex(ValueError, "changed during capsule staging"):
                    prepare.stage(output)
            self.assertFalse(output.exists())


if __name__ == "__main__":
    unittest.main()
