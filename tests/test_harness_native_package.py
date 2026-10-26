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
        source_record, = [item for item in self.metadata["sources"]
                          if item["path"] == prepare.COMPOSITION]
        self.assertEqual(generated, self.capsule[source_record["stored_as"]])
        tree = ast.parse(generated, filename=COMPOSITION_PATH)
        classes = [node for node in tree.body
                   if isinstance(node, ast.ClassDef)
                   and node.name == "DefaultspackProfileRuntime"]
        self.assertEqual(len(classes), 1)
        all_methods = [node for node in classes[0].body
                       if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))]
        methods = [node for node in all_methods if node.name in prepare.METHODS]
        self.assertEqual([node.name for node in methods], list(prepare.METHODS))
        self.assertGreater(len(all_methods), len(prepare.METHODS))

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
        source_record, = [item for item in self.metadata["sources"]
                          if item["path"] == prepare.PROFILE_PORT]
        self.assertEqual(generated, self.capsule[source_record["stored_as"]])
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

    def test_full_captured_dependencies_are_copied_verbatim_and_native_boundary_stays_open(self) -> None:
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
                prepare.NATIVE_IMPORT_CUSTODY_SOURCES[0],
                "tobkiri_runtime/core_runtime/profile_content_projection.py",
                "tobkiri_runtime/core_runtime/profile_pack_projection.py",
                prepare.SERVICE_SOURCE,
            },
        )
        self.assertTrue(all(
            item["status"] == "source_present_but_native_io_or_admission_unconnected"
            for item in manifest["unconnected_native_dependencies"]
        ))
        self.assertEqual(manifest["external_runtime_dependencies"],
                         ["cryptography", "jsonschema", "packaging"])
        self.assertEqual(manifest["uncaptured_package_initializers"], [])
        self.assertEqual(manifest["implicit_namespace_packages"], [
            "ecosystem", "ecosystem.defaultspack",
        ])
        for item in manifest["unconnected_native_dependencies"]:
            if item["package_path"] is None:
                self.assertNotIn(f"source-package/{item['source_path']}", self.package)
        scan = manifest["dependency_scan"]
        self.assertTrue(scan["host_python_version"])
        self.assertIn("sys.stdlib_module_names", scan["classification_basis"])
        self.assertTrue(scan["source_paths_are_not_importability_proof"])
        self.assertIn("not prove", scan["stdlib_classification_limit"])
        self.assertTrue(scan["module_initialization_complete"])
        self.assertEqual(scan["module_initialization_missing_modules"], [])
        missing_modules = set(scan["missing_modules"])
        self.assertTrue({
            "core_runtime.existing_host_extension_admission_v4",
            "core_runtime.pack_version_selection",
            "tobkiri_host.backends",
            "tobkiri_protocol.composition",
            "tobkiri_protocol.migration",
        }.issubset(missing_modules))
        unresolved = scan["unresolved_imports"]
        self.assertEqual({row["target"] for row in unresolved}, missing_modules)
        self.assertTrue(all(row["source_path"].startswith("source-package/tobkiri_runtime/")
                            and row["line"] > 0 for row in unresolved))
        self.assertIn("type_checking", {row["phase"] for row in unresolved})
        self.assertIn("runtime_lazy", {row["phase"] for row in unresolved})
        self.assertFalse(scan["target_stdlib_importability_proven"])
        self.assertTrue({"ctypes", "fcntl", "msvcrt"}.issubset(
            scan["host_stdlib_modules_seen"],
        ))
        self.assertTrue(scan["dynamic_import_sites"])

        schema_resources = manifest["schema_resources"]
        schema_index = json.loads(self.package[
            f"source-package/{prepare.SCHEMA_HASHES}"
        ])
        self.assertEqual({Path(item["path"]).name for item in schema_resources},
                         set(schema_index["files"]))
        for resource in schema_resources:
            content = self.package[resource["path"]]
            self.assertEqual(resource["sha256"], schema_index["files"][Path(resource["path"]).name])
            self.assertEqual(resource["bytes"], len(content))

    def test_generated_module_extents_bind_original_records_without_importing_modules(self) -> None:
        manifest = json.loads(self.package["source-package/provenance.json"])
        generated_modules = {item["path"]: item for item in manifest["generated_modules"]}
        expected_modules = {
            path for path in self.package
            if path.startswith("source-package/tobkiri_runtime/") and path.endswith(".py")
        }
        self.assertEqual(set(generated_modules), expected_modules)
        for relative, module in generated_modules.items():
            source = self.package[relative]
            self.assertEqual(module["sha256"], hashlib.sha256(source).hexdigest())
            self.assertEqual(module["bytes"], len(source))
            self.assertEqual(module["source_kind"], "verbatim_original")
            self.assertEqual(module["source_sha256"], module["sha256"])
            ast.parse(source, filename=relative)
            extents = list(module.get("definition_extents", {}).values())
            if "class_extent" in module:
                extents.append(module["class_extent"])
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

    def test_source_index_is_data_only_exact_and_binds_path_modules(self) -> None:
        path = "source-package/harness_native_source_index.py"
        source = self.package[path]
        tree = ast.parse(source, filename=path)
        self.assertTrue(all(isinstance(node, ast.Assign) for node in tree.body))
        values = {
            node.targets[0].id: ast.literal_eval(node.value)
            for node in tree.body
            if isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
        }
        self.assertEqual(set(values), {"MODULES", "PATH_MODULES", "INDEX_SHA256"})
        modules = values["MODULES"]
        paths = values["PATH_MODULES"]
        self.assertEqual(list(modules), sorted(modules))
        self.assertEqual(len({item[0] for item in modules}), len(modules))
        self.assertEqual({item[1] for item in modules}, {
            path.removeprefix("source-package/tobkiri_runtime/")
            for path in self.package
            if path.startswith("source-package/tobkiri_runtime/")
            and path.endswith(".py")
        })
        for module, runtime_path, digest in modules:
            package_path = f"source-package/tobkiri_runtime/{runtime_path}"
            self.assertEqual(hashlib.sha256(self.package[package_path]).hexdigest(), digest)
        actual_path_modules = []
        for module, runtime_path, _digest in modules:
            package_path = f"source-package/tobkiri_runtime/{runtime_path}"
            tree = ast.parse(self.package[package_path], filename=package_path)
            if any(
                (isinstance(node, ast.Import)
                 and any(alias.name == "pathlib" or alias.name.startswith("pathlib.")
                         for alias in node.names))
                or (isinstance(node, ast.ImportFrom)
                    and (node.module == "pathlib"
                         or (node.module or "").startswith("pathlib.")))
                for node in ast.walk(tree)
            ):
                actual_path_modules.append(module)
        self.assertEqual(list(paths), sorted(actual_path_modules))
        canonical = json.dumps(
            {"MODULES": modules, "PATH_MODULES": paths},
            ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":"),
        ).encode("utf-8")
        self.assertEqual(values["INDEX_SHA256"], hashlib.sha256(canonical).hexdigest())
        manifest = json.loads(self.package["source-package/provenance.json"])
        self.assertEqual(manifest["source_index"]["index_sha256"], values["INDEX_SHA256"])

    def test_service_data_classes_and_launch_helper_keep_original_bytes_and_aliases(self) -> None:
        manifest = json.loads(self.package["source-package/provenance.json"])
        original = prepare.bounded_read(ROOT / prepare.SERVICE_SOURCE)
        service_path = f"source-package/{prepare.SERVICE_SOURCE}"
        self.assertEqual(self.package[service_path], original)
        service_records = [d for d in manifest["definitions"]
                           if d["source_path"] == prepare.SERVICE_SOURCE]
        self.assertEqual({r["symbol"] for r in service_records}, {
            *data_facade.SERVICE_FUNCTIONS,
            "bundle_catalog_reexports", "profile_resolution_reexports", "canonical_reexports",
            "profile_runtime_record_reexports",
        })
        for record in service_records:
            original_bytes = original[record["source_start_byte"]:record["source_end_byte"]]
            extent = record["generated_extent"]
            generated_bytes = self.package[record["generated_path"]][
                extent["start_byte"]:extent["end_byte"]]
            self.assertEqual(generated_bytes, original_bytes)
            self.assertEqual(hashlib.sha256(original).hexdigest(), record["source_module_sha256"])
        tree = ast.parse(self.package[service_path])
        class_names = [n.name for n in tree.body if isinstance(n, ast.ClassDef)]
        self.assertIn("ActivationStore", class_names)
        self.assertTrue(set(class_names).isdisjoint(data_facade.SERVICE_CLASSES))
        self.assertGreater(len(class_names), len(prepare.SERVICE_DATA_CLASSES))
        record_import, = [n for n in tree.body if isinstance(n, ast.ImportFrom)
                          and n.module == "_profile_runtime_records" and n.level == 2]
        self.assertEqual({(n.name, n.asname) for n in record_import.names},
                         {(name, name) for name in data_facade.SERVICE_CLASSES})
        record_path = f"source-package/{prepare.PROFILE_RUNTIME_RECORDS}"
        original_records = prepare.bounded_read(ROOT / prepare.PROFILE_RUNTIME_RECORDS)
        self.assertEqual(self.package[record_path], original_records)
        relocated = [d for d in manifest["definitions"]
                     if d["source_path"] == prepare.PROFILE_RUNTIME_RECORDS]
        self.assertEqual(
            {d["symbol"] for d in relocated}, set(data_facade.SERVICE_CLASSES),
        )
        for record in relocated:
            self.assertEqual(record["generated_path"], record_path)
            exact = original_records[
                record["source_start_byte"]:record["source_end_byte"]
            ]
            extent = record["generated_extent"]
            self.assertEqual(exact, self.package[record_path][
                extent["start_byte"]:extent["end_byte"]
            ])
            self.assertEqual(record["sha256"], hashlib.sha256(exact).hexdigest())
        reexports, = [n for n in tree.body if isinstance(n, ast.ImportFrom)
                      and n.module == "profile_resolution" and n.level == 2]
        self.assertEqual([(n.name, n.asname) for n in reexports.names],
                         [(n, n) for n in data_facade.RESOLUTION_EXPORTS])
        initializer_path = f"source-package/{prepare.SERVICE_INITIALIZER}"
        self.assertEqual(self.package[initializer_path],
                         prepare.bounded_read(ROOT / prepare.SERVICE_INITIALIZER))
        initializer = ast.parse(self.package[initializer_path])
        reexports, = [n for n in initializer.body if isinstance(n, ast.ImportFrom)]
        self.assertEqual(reexports.module, "service")
        self.assertEqual(reexports.level, 1)
        self.assertIn("ActivationStore", [n.name for n in reexports.names])
        self.assertEqual([n.name for n in reexports.names], list(data_facade.PACKAGE_EXPORTS))
        self.assertEqual(
            ast.literal_eval(initializer.body[-1].value), list(data_facade.PACKAGE_EXPORTS),
        )

    def test_bootstrap_record_import_leaves_native_service_unreached(self) -> None:
        manifest = json.loads(self.package["source-package/provenance.json"])
        bootstrap = ast.parse(self.package[
            "source-package/tools/python313-producer/harness_native/bootstrap.py"
        ])
        load, = [node for node in bootstrap.body if isinstance(node, ast.FunctionDef)
                 and node.name == "_load_bound_runtime"]
        imports = [node for node in load.body if isinstance(node, ast.ImportFrom)
                   and node.level == 0]
        active_import, = [
            node for node in imports
            if any(name.name == "ActiveDefaultProfile" for name in node.names)
        ]
        self.assertEqual(active_import.module,
                         "ecosystem.defaultspack.domain._profile_runtime_records")
        package_modules = {row["module"] for row in manifest["generated_modules"]
                           if row["path"].endswith("/__init__.py")}
        edges = {}
        for row in manifest["dependency_scan"]["module_initialization_imports"]:
            edges.setdefault(row["module"], set()).add(row["target"])
        pending = [node.module for node in imports]
        reached = set()
        while pending:
            module = pending.pop()
            if module in reached:
                continue
            reached.add(module)
            pending.extend(edges.get(module, ()))
            pending.extend(".".join(module.split(".")[:i])
                           for i in range(1, len(module.split(".")))
                           if ".".join(module.split(".")[:i]) in package_modules)
        self.assertTrue({"tobkiri_protocol.validation", "jsonschema"}.issubset(reached))
        self.assertTrue(reached.isdisjoint({
            "core_runtime.profile_content_projection",
            "core_runtime.profile_pack_projection",
            "ecosystem.defaultspack.domain.runtime_v4",
            "ecosystem.defaultspack.domain.runtime_v4.service",
            "tobkiri_protocol.secure_persistence",
        }))
        self.assertFalse(any(
            module.startswith(("core_runtime.authority", "cryptography"))
            for module in reached
        ))
        self.assertFalse(manifest["dependency_closure_complete"])
        self.assertFalse(manifest["production_enabled"])

    def test_service_initializer_custody_and_remaining_native_gap_are_explicit(self) -> None:
        manifest = json.loads(self.package["source-package/provenance.json"])
        facade = manifest["original_service_package"]
        path = facade["original_initializer_preserved_as"]
        self.assertEqual(path, f"source-package/{prepare.SERVICE_INITIALIZER}")
        self.assertEqual(self.package[path], prepare.bounded_read(ROOT / prepare.SERVICE_INITIALIZER))
        self.assertEqual(facade["unconnected_native_package_exports"], ["ActivationStore"])
        self.assertTrue(facade["complete_original_modules_staged"])
        self.assertFalse(facade["facade_modules_used_for_execution"])
        self.assertEqual(
            facade["original_record_source"], prepare.PROFILE_RUNTIME_RECORDS,
        )
        self.assertEqual(facade["original_record_package_path"],
                         f"source-package/{prepare.PROFILE_RUNTIME_RECORDS}")
        self.assertFalse(facade["class_definitions_synthesized"])
        self.assertFalse(facade["record_relocation"]["aliases_executed"])
        for key in ("native_activation_state_in_guest", "original_class_aliases_executed",
                    "launch_selector_wit_connected", "whole_service_compatible"):
            self.assertIs(facade[key], False)
        self.assertIs(facade["native_state_transfer"]["connected"], False)
        for original in (prepare.SERVICE_SOURCE, prepare.SERVICE_INITIALIZER):
            row, = [r for r in manifest["sources"] if r["path"] == original]
            self.assertEqual(row["package_path"], f"source-package/{original}")
            self.assertFalse(row["native_import_source"])
        service, = [r for r in manifest["unconnected_native_dependencies"]
                    if r["source_path"] == prepare.SERVICE_SOURCE]
        self.assertEqual(service["status"],
                         "source_present_but_native_io_or_admission_unconnected")
        self.assertNotIn("ecosystem.defaultspack.domain.runtime_v4.service",
                         manifest["dependency_scan"]["missing_modules"])
        self.assertFalse(manifest["dependency_closure_complete"])

    def test_service_class_reexport_and_initializer_drift_fail_after_checksum_rewrite(self) -> None:
        cases = (
            ("ActiveDefaultProfile", b"frozen=True", b"frozen=False"),
            ("profile_resolution_reexports", b"as ProfileResolutionDenied", b"as OtherError"),
            ("profile_runtime_record_reexports", b"as ActiveDefaultProfile",
             b"as OtherRecord"),
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
                with self.assertRaisesRegex(
                    ValueError,
                    "(definition (extent differs from source|bytes differ from source extent)|original (active DATA|service DATA|service package))",
                ):
                    assemble(capsule)

    def test_original_class_keeps_its_module_imports_and_contains_no_fake_authority_objects(self) -> None:
        forbidden_names = {
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
        methods = [node.name for node in runtime.body if isinstance(node, ast.FunctionDef)]
        self.assertEqual(methods, list(prepare._EXPECTED_RUNTIME_CLASS_METHODS))
        self.assertEqual(
            [node.name for node in trees[0].body if isinstance(node, ast.ClassDef)],
            ["DefaultspackProfileRuntime"],
        )
        self.assertTrue(any(isinstance(node, ast.FunctionDef)
                            and node.name == "install_defaultspack_profile_runtime"
                            for node in trees[0].body))
        self.assertTrue(any(isinstance(node, ast.Call)
                            and isinstance(node.func, ast.Name)
                            and node.func.id == "register_profile_runtime"
                            for node in ast.walk(composition)))

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

        missing_eager_module = dict(self.capsule)
        missing_eager_module.pop(
            "source/tobkiri_runtime/tobkiri_host/artifact_compiler.py",
        )
        with self.assertRaisesRegex(ValueError, "stored source is missing"):
            assemble(missing_eager_module)

        changed_schema = dict(self.capsule)
        schema_metadata = json.loads(changed_schema["provenance.json"])
        schema_record, = [record for record in schema_metadata["sources"]
                          if record["path"].endswith("/common_v1.schema.json")]
        schema_source_key = schema_record["stored_as"]
        changed_schema[schema_source_key] += b"\n"
        schema_record["bytes"] = len(changed_schema[schema_source_key])
        schema_record["sha256"] = hashlib.sha256(
            changed_schema[schema_source_key],
        ).hexdigest()
        _rehashed_metadata(changed_schema, schema_metadata)
        with self.assertRaisesRegex(ValueError, "differs from its hash pin"):
            assemble(changed_schema)

        tampered_method = dict(self.capsule)
        method_path = "definitions/profile_runtime/setup_listing.py"
        tampered_method[method_path] = tampered_method[method_path].replace(
            b"review_required", b"changed_value____", 1,
        )
        with self.assertRaisesRegex(
            ValueError, "definition bytes differ from (their record|source extent)"
        ):
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
