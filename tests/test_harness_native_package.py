"""Source-only checks for the assembled Defaultspack Python package."""

from __future__ import annotations

import ast
import hashlib
import json
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [
    str(ROOT / "scripts/wasm"),
    str(ROOT / "tools/python313-producer"),
]

import prepare_harness_native as prepare  # noqa: E402 - source-tool path.
from harness_native.source_package import assemble  # noqa: E402
from harness_native import data_facade  # noqa: E402


COMPOSITION_PATH = (
    "source-package/tobkiri_runtime/ecosystem/defaultspack/defaultspack/"
    "profile_runtime_composition.py"
)
PROFILE_PORT_PATH = "source-package/tobkiri_runtime/core_runtime/profile_runtime_port.py"


def _source_extent(source: bytes, node: ast.AST) -> bytes:
    start_line = getattr(node, "lineno")
    end_line = getattr(node, "end_lineno")
    decorators = getattr(node, "decorator_list", ())
    if decorators:
        start_line = min(start_line, *(item.lineno for item in decorators))
    lines = source.splitlines(keepends=True)
    start = sum(len(line) for line in lines[:start_line - 1])
    end = sum(len(line) for line in lines[:end_line])
    return source[start:end]


def _rehashed_metadata(files: dict[str, bytes], metadata: dict[str, object]) -> None:
    unsigned = dict(metadata)
    unsigned.pop("metadata_sha256", None)
    canonical = json.dumps(
        unsigned,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    metadata["metadata_sha256"] = hashlib.sha256(canonical).hexdigest()
    files["provenance.json"] = (
        json.dumps(metadata, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")


class HarnessNativeSourcePackageTests(unittest.TestCase):
    """Check the generated modules against actual capsule source records."""

    def setUp(self) -> None:
        self.capsule = prepare.captured()
        self.package = assemble(self.capsule)
        self.metadata = json.loads(self.capsule["provenance.json"])

    def test_all_seventeen_methods_keep_source_bytes_signature_and_body(self) -> None:
        generated = self.package[COMPOSITION_PATH]
        tree = ast.parse(generated, filename=COMPOSITION_PATH)
        classes = [node for node in tree.body
                   if isinstance(node, ast.ClassDef)
                   and node.name == "DefaultspackProfileRuntime"]
        self.assertEqual(len(classes), 1)
        methods = [node for node in classes[0].body
                   if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))]
        self.assertEqual([node.name for node in methods], list(prepare.METHODS))

        records = [item for item in self.metadata["definitions"]
                   if item["source_path"] == prepare.COMPOSITION]
        self.assertEqual([item["symbol"] for item in records], list(prepare.METHODS))
        for record, generated_method in zip(records, methods, strict=True):
            original_bytes = self.capsule[record["stored_as"]]
            original_tree = ast.parse(b"class _SourceMethod:\n" + original_bytes)
            original_method = original_tree.body[0].body[0]
            self.assertEqual(
                _source_extent(generated, generated_method),
                original_bytes,
                msg=f"generated method bytes changed: {record['symbol']}",
            )
            self.assertEqual(
                ast.dump(generated_method, include_attributes=False),
                ast.dump(original_method, include_attributes=False),
                msg=f"generated signature or body changed: {record['symbol']}",
            )
            self.assertEqual(
                hashlib.sha256(original_bytes).hexdigest(), record["sha256"],
            )

    def test_decision_class_is_verbatim_shared_and_keeps_its_invariant(self) -> None:
        record, = [item for item in self.metadata["definitions"]
                   if item["source_path"] == prepare.PROFILE_PORT
                   and item["symbol"] == "SetupActivationDecision"]
        original = self.capsule[record["stored_as"]]
        generated = self.package[PROFILE_PORT_PATH]
        generated_tree = ast.parse(generated, filename=PROFILE_PORT_PATH)
        classes = [node for node in generated_tree.body
                   if isinstance(node, ast.ClassDef)
                   and node.name == "SetupActivationDecision"]
        self.assertEqual(len(classes), 1)
        self.assertEqual(_source_extent(generated, classes[0]), original)

        original_tree = ast.parse(original, filename="SetupActivationDecision.py")
        original_class = original_tree.body[0]
        self.assertEqual(
            ast.dump(classes[0], include_attributes=False),
            ast.dump(original_class, include_attributes=False),
        )
        self.assertIsInstance(classes[0].decorator_list[0], ast.Call)
        decorator = classes[0].decorator_list[0]
        self.assertEqual(decorator.func.id, "dataclass")
        self.assertEqual([(item.arg, item.value.value) for item in decorator.keywords],
                         [("frozen", True)])
        self.assertTrue(any(isinstance(item, ast.FunctionDef)
                            and item.name == "__post_init__"
                            for item in classes[0].body))

        composition_tree = ast.parse(self.package[COMPOSITION_PATH])
        self.assertFalse(any(isinstance(item, ast.ClassDef)
                             and item.name == "SetupActivationDecision"
                             for item in composition_tree.body))
        imports = [item for item in composition_tree.body
                   if isinstance(item, ast.ImportFrom)
                   and item.module == "core_runtime.profile_runtime_port"]
        self.assertEqual(len(imports), 1)
        self.assertIn("SetupActivationDecision", [alias.name for alias in imports[0].names])

    def test_full_captured_dependencies_are_copied_verbatim_and_native_deps_stay_missing(self) -> None:
        for source in self.metadata["sources"]:
            stored_as = source.get("stored_as")
            if stored_as is None:
                continue
            target = f"source-package/{source['path']}"
            self.assertIn(target, self.package)
            self.assertEqual(self.package[target], self.capsule[stored_as], source["path"])
            self.assertEqual(hashlib.sha256(self.package[target]).hexdigest(), source["sha256"])

        manifest = json.loads(self.package["source-package/provenance.json"])
        self.assertEqual(manifest["format"], "io.tobkiri.harness-native-source-package.v1")
        self.assertTrue(manifest["source_package_assembled"])
        self.assertFalse(manifest["build_performed"])
        self.assertFalse(manifest["wasm_execution_performed"])
        self.assertFalse(manifest["native_inspection_ports_connected"])
        self.assertFalse(manifest["whole_setup_compatible"])
        self.assertFalse(manifest["production_enabled"])
        self.assertFalse(manifest["dependency_closure_complete"])
        self.assertEqual(
            {item["source_path"] for item in manifest["unconnected_native_dependencies"]},
            set(prepare._DEFERRED_NATIVE_MODULES),
        )
        self.assertEqual(
            {item["source_path"] for item in manifest["unconnected_native_dependencies"]
             if item["package_path"] is not None},
            {
                "tobkiri_runtime/core_runtime/profile_content_projection.py",
                "tobkiri_runtime/core_runtime/profile_pack_projection.py",
                prepare.SERVICE_SOURCE,
            },
        )
        self.assertTrue(any(
            item["status"] == "native_source_deferred"
            for item in manifest["unconnected_native_dependencies"]
        ))
        self.assertTrue(manifest["external_runtime_dependencies"])
        self.assertTrue(manifest["uncaptured_package_initializers"])
        for item in manifest["unconnected_native_dependencies"]:
            if item["package_path"] is None:
                self.assertNotIn(f"source-package/{item['source_path']}", self.package)
        scan = manifest["dependency_scan"]
        self.assertTrue(scan["host_python_version"])
        self.assertIn("sys.stdlib_module_names", scan["classification_basis"])
        self.assertTrue(scan["source_paths_are_not_importability_proof"])
        self.assertIn("not prove", scan["stdlib_classification_limit"])
        missing_modules = set(scan["missing_modules"])
        self.assertTrue({
            "core_runtime.external_pack_catalog_v4",
            "core_runtime.resolved_profile_scope",
            "tobkiri_host.artifact_compiler",
            "tobkiri_protocol.composition",
            "tobkiri_protocol.migration",
        }.issubset(missing_modules))

    def test_generated_module_extents_bind_original_records_without_importing_modules(self) -> None:
        manifest = json.loads(self.package["source-package/provenance.json"])
        generated_modules = {item["path"]: item for item in manifest["generated_modules"]}
        self.assertEqual(set(generated_modules), {
            COMPOSITION_PATH, PROFILE_PORT_PATH,
            data_facade.SERVICE_PATH, data_facade.INITIALIZER_PATH,
        })
        for relative, module in generated_modules.items():
            source = self.package[relative]
            self.assertEqual(module["sha256"], hashlib.sha256(source).hexdigest())
            self.assertEqual(module["bytes"], len(source))
            ast.parse(source, filename=relative)
            extents = ([module["class_extent"]] if "class_extent" in module
                       else list(module.get("definition_extents", {}).values()))
            if relative == data_facade.SERVICE_PATH:
                self.assertEqual(set(module["definition_extents"]),
                                 {*data_facade.SERVICE_CLASSES, *data_facade.SERVICE_FUNCTIONS})
            elif relative == data_facade.INITIALIZER_PATH:
                self.assertNotIn("class", module)
                self.assertEqual(extents, [])
            else:
                self.assertEqual(len(extents), 1)
            for extent in extents:
                exact = source[extent["start_byte"]:extent["end_byte"]]
                self.assertEqual(hashlib.sha256(exact).hexdigest(), extent["sha256"])
        for definition in manifest["definitions"]:
            source = self.package[definition["generated_path"]]
            extent = definition["generated_extent"]
            lines = source.splitlines(keepends=True)
            exact = source[extent["start_byte"]:extent["end_byte"]]
            self.assertEqual(len(exact), extent["bytes"])
            self.assertEqual(hashlib.sha256(exact).hexdigest(), extent["sha256"])
            self.assertEqual(
                extent["start_byte"],
                sum(len(line) for line in lines[:extent["start_line"] - 1]),
            )
            self.assertEqual(
                extent["end_byte"],
                sum(len(line) for line in lines[:extent["end_line"]]),
            )

    def test_service_data_classes_and_launch_helper_keep_original_bytes_and_aliases(self) -> None:
        manifest = json.loads(self.package["source-package/provenance.json"])
        original = prepare.bounded_read(ROOT / prepare.SERVICE_SOURCE)
        service_records = [d for d in manifest["definitions"]
                           if d["source_path"] == prepare.SERVICE_SOURCE]
        self.assertEqual({r["symbol"] for r in service_records}, {
            *data_facade.SERVICE_CLASSES, *data_facade.SERVICE_FUNCTIONS,
            "bundle_catalog_reexports", "profile_resolution_reexports", "canonical_reexports",
        })
        for record in service_records:
            original_bytes = original[record["source_start_byte"]:record["source_end_byte"]]
            extent = record["generated_extent"]
            generated_bytes = self.package[record["generated_path"]][
                extent["start_byte"]:extent["end_byte"]]
            self.assertEqual(generated_bytes, original_bytes)
            self.assertEqual(hashlib.sha256(original).hexdigest(), record["source_module_sha256"])
        tree = ast.parse(self.package[data_facade.SERVICE_PATH])
        self.assertEqual([n.name for n in tree.body if isinstance(n, ast.ClassDef)],
                         list(prepare.SERVICE_DATA_CLASSES))
        self.assertFalse(any(isinstance(n, ast.ClassDef) and n.name in {
            "BundledCatalog", "ResolvedDefaultProfile", "ProfileResolutionDenied", "ActivationStore",
        } for n in ast.walk(tree)))
        reexports, = [n for n in tree.body if isinstance(n, ast.ImportFrom)
                      and n.module == "profile_resolution" and n.level == 2]
        self.assertEqual([(n.name, n.asname) for n in reexports.names],
                         [(n, n) for n in data_facade.RESOLUTION_EXPORTS])
        initializer = ast.parse(self.package[data_facade.INITIALIZER_PATH])
        reexports, = [n for n in initializer.body if isinstance(n, ast.ImportFrom)]
        self.assertEqual(reexports.module, "service")
        self.assertEqual(reexports.level, 1)
        self.assertEqual([n.name for n in reexports.names], list(data_facade.DATA_EXPORTS))
        self.assertNotIn("ActivationStore", [n.name for n in reexports.names])
        self.assertFalse(any(isinstance(n, (ast.ClassDef, ast.FunctionDef))
                             for n in initializer.body))

    def test_service_initializer_custody_and_remaining_native_gap_are_explicit(self) -> None:
        manifest = json.loads(self.package["source-package/provenance.json"])
        facade = manifest["service_data_facade"]
        path = facade["original_initializer_preserved_as"]
        self.assertEqual(self.package[path], prepare.bounded_read(ROOT / prepare.SERVICE_INITIALIZER))
        self.assertTrue(path.endswith(".txt"))
        self.assertEqual(facade["unconnected_native_package_exports"], ["ActivationStore"])
        for key in ("native_activation_state_in_guest", "original_class_aliases_executed",
                    "launch_selector_wit_connected", "whole_service_compatible"):
            self.assertIs(facade[key], False)
        self.assertIs(facade["native_state_transfer"]["connected"], False)
        for original in (prepare.SERVICE_SOURCE, prepare.SERVICE_INITIALIZER):
            row, = [r for r in manifest["sources"] if r["path"] == original]
            self.assertIsNone(row["package_path"])
            self.assertEqual(row["generated_data_facade_path"], f"source-package/{original}")
        service, = [r for r in manifest["unconnected_native_dependencies"]
                    if r["source_path"] == prepare.SERVICE_SOURCE]
        self.assertEqual(service["status"], "original_data_facade_native_exports_unconnected")
        self.assertNotIn("ecosystem.defaultspack.domain.runtime_v4.service",
                         manifest["dependency_scan"]["missing_modules"])
        self.assertFalse(manifest["dependency_closure_complete"])

    def test_service_class_reexport_and_initializer_drift_fail_after_checksum_rewrite(self) -> None:
        cases = (
            ("ActiveDefaultProfile", b"frozen=True", b"frozen=False"),
            ("profile_resolution_reexports", b"as ProfileResolutionDenied", b"as OtherError"),
            ("runtime_v4_package_initializer", b"__all__ = [", b"print('not permitted')\n\n__all__ = ["),
        )
        for symbol, before, after in cases:
            with self.subTest(symbol=symbol):
                capsule = dict(self.capsule)
                metadata = json.loads(capsule["provenance.json"])
                record, = [r for r in metadata["definitions"] if r["symbol"] == symbol]
                path = record["stored_as"]
                original = capsule[path]
                self.assertIn(before, original)
                changed = original.replace(before, after, 1)
                capsule[path] = changed
                record["bytes"] = len(changed)
                record["sha256"] = hashlib.sha256(changed).hexdigest()
                record["source_end_byte"] = record["source_start_byte"] + len(changed)
                record["source_end_line"] = record["source_start_line"] + len(changed.splitlines()) - 1
                _rehashed_metadata(capsule, metadata)
                with self.assertRaisesRegex(ValueError, "original (active DATA|service DATA|service package)"):
                    assemble(capsule)

    def test_generated_class_contains_no_host_registration_or_extra_authority_objects(self) -> None:
        forbidden_names = {
            "register_profile_runtime",
            "install_defaultspack_profile_runtime",
            "_PROFILE_RUNTIME",
            "SimpleNamespace",
            "FakeAuthority",
        }
        trees = [ast.parse(self.package[path], filename=path)
                 for path in (COMPOSITION_PATH, PROFILE_PORT_PATH)]
        self.assertFalse(any(isinstance(node, ast.Name) and node.id in forbidden_names
                             for tree in trees for node in ast.walk(tree)))
        composition = trees[0]
        runtime, = [node for node in composition.body
                    if isinstance(node, ast.ClassDef)
                    and node.name == "DefaultspackProfileRuntime"]
        self.assertEqual(
            [node.name for node in runtime.body if isinstance(node, ast.FunctionDef)],
            list(prepare.METHODS),
        )
        self.assertEqual(
            [node.name for node in trees[0].body if isinstance(node, ast.ClassDef)],
            ["DefaultspackProfileRuntime"],
        )

    def test_tampered_missing_reordered_and_bad_digest_capsules_fail_closed(self) -> None:
        bad_digest = dict(self.capsule)
        bad_digest["provenance.json"] = bad_digest["provenance.json"].replace(
            b'"production_enabled": false', b'"production_enabled": true', 1,
        )
        with self.assertRaisesRegex(ValueError, "metadata checksum is invalid"):
            assemble(bad_digest)

        missing_method = dict(self.capsule)
        missing_method.pop("definitions/profile_runtime/setup_preview.py")
        with self.assertRaisesRegex(ValueError, "definition bytes are missing"):
            assemble(missing_method)

        missing_record = dict(self.capsule)
        missing_metadata = json.loads(missing_record["provenance.json"])
        missing_metadata["definitions"] = [
            item for item in missing_metadata["definitions"]
            if not (item["source_path"] == prepare.COMPOSITION
                    and item["symbol"] == "setup_preview")
        ]
        missing_record.pop("definitions/profile_runtime/setup_preview.py")
        _rehashed_metadata(missing_record, missing_metadata)
        with self.assertRaisesRegex(ValueError, "method definition records are missing"):
            assemble(missing_record)

        missing_source_record = dict(self.capsule)
        source_metadata = json.loads(missing_source_record["provenance.json"])
        source_metadata["sources"] = [
            item for item in source_metadata["sources"]
            if item["path"] != "tobkiri_runtime/tobkiri_protocol/canonical.py"
        ]
        missing_source_record.pop("source/tobkiri_runtime/tobkiri_protocol/canonical.py")
        _rehashed_metadata(missing_source_record, source_metadata)
        with self.assertRaisesRegex(ValueError, "required source record is missing"):
            assemble(missing_source_record)

        tampered_method = dict(self.capsule)
        method_path = "definitions/profile_runtime/setup_listing.py"
        tampered_method[method_path] = tampered_method[method_path].replace(
            b"review_required", b"changed_value____", 1,
        )
        with self.assertRaisesRegex(ValueError, "definition bytes differ from their record"):
            assemble(tampered_method)

        reordered = dict(self.capsule)
        metadata = json.loads(reordered["provenance.json"])
        first = metadata["definitions"].index(next(
            item for item in metadata["definitions"]
            if item["source_path"] == prepare.COMPOSITION
            and item["symbol"] == prepare.METHODS[0]
        ))
        second = metadata["definitions"].index(next(
            item for item in metadata["definitions"]
            if item["source_path"] == prepare.COMPOSITION
            and item["symbol"] == prepare.METHODS[1]
        ))
        metadata["definitions"][first], metadata["definitions"][second] = (
            metadata["definitions"][second], metadata["definitions"][first],
        )
        _rehashed_metadata(reordered, metadata)
        with self.assertRaisesRegex(ValueError, "definition records are missing, extra, or reordered"):
            assemble(reordered)

        reordered_selected = dict(self.capsule)
        selected_metadata = json.loads(reordered_selected["provenance.json"])
        selected_metadata["selected_methods"][0:2] = selected_metadata["selected_methods"][1::-1]
        _rehashed_metadata(reordered_selected, selected_metadata)
        with self.assertRaisesRegex(ValueError, "selected method order differs"):
            assemble(reordered_selected)


if __name__ == "__main__":
    unittest.main()
