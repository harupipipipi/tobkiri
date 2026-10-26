"""Capture verbatim Defaultspack Setup DATA sources; never build or execute.

This creates a source capsule only.  It does not make Wasm, assert a complete
nativeKit path proof, or grant native authority.  Native file/catalog adapters
remain explicit inputs to a later integration.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
from typing import Any

from prepare_gateway_tool_caller import ROOT, _directory, bounded_read

COMPOSITION = (
    "tobkiri_runtime/ecosystem/defaultspack/defaultspack/"
    "profile_runtime_composition.py"
)
PROFILE_PORT = "tobkiri_runtime/core_runtime/profile_runtime_port.py"
PROFILE_RESOLUTION = (
    "tobkiri_runtime/ecosystem/defaultspack/domain/profile_resolution.py"
)
SERVICE_SOURCE = (
    "tobkiri_runtime/ecosystem/defaultspack/domain/runtime_v4/service.py"
)
SERVICE_INITIALIZER = (
    "tobkiri_runtime/ecosystem/defaultspack/domain/runtime_v4/__init__.py"
)
SERVICE_DATA_CLASSES = (
    "ProfileReconfirmationRequired", "ActivationLockTimeout",
    "ArtifactVerificationTimeout", "ActiveDefaultProfile",
)
SETUP_CONTRACT = (
    "tobkiri_runtime/ecosystem/defaultspack/defaultspack/setup_contract.py"
)
DOMAIN_PACKAGE_INIT = "tobkiri_runtime/ecosystem/defaultspack/domain/__init__.py"
DEFAULTSPACK_PACKAGE_INIT = (
    "tobkiri_runtime/ecosystem/defaultspack/defaultspack/__init__.py"
)
PROTOCOL_ROOT = "tobkiri_runtime/tobkiri_protocol"
SCHEMA_DIRECTORY = f"{PROTOCOL_ROOT}/schemas"
SCHEMA_HASHES = f"{PROTOCOL_ROOT}/schema_hashes.json"
PROJECT_METADATA = "tobkiri_runtime/pyproject.toml"
EXCEPTION_TRANSPORT = (
    "tools/python313-producer/harness_native/exception_graph.py"
)
VALUE_GRAPH_TRANSPORT = "tools/python313-producer/harness_native/value_graph.py"
NATIVE_WIT = "tools/python313-producer/harness_native/native.wit"
DISPATCHER_SOURCES = (
    "tools/python313-producer/harness_native/dispatcher.py",
    "tools/python313-producer/harness_native/bootstrap.py",
    "tools/python313-producer/harness_native/app.py",
)
DATA_FACADE_SOURCE = "tools/python313-producer/harness_native/data_facade.py"
_NATIVE_KIT_SOURCES = (
    "tobkiri_launcher/harness-native/Cargo.toml",
    "tobkiri_launcher/harness-native/src/lib.rs",
    "tobkiri_launcher/harness-native/src/exception_graph.rs",
)

PROTOCOL_MODULES = (
    "__init__.py",
    "bundle_catalog.py",
    "canonical.py",
    "captured_inputs.py",
    "errors.py",
    "executable_catalog.py",
    "ids.py",
    "platform_artifact.py",
    "profile_scope.py",
    "validation.py",
)

METHODS = (
    "bootstrap_profile_id",
    "setup_profile_preview",
    "_confirmation",
    "bootstrap_confirmation",
    "profile_confirmation",
    "setup_listing",
    "setup_preview",
    "setup_activation_decision",
    "setup_activation_success",
    "setup_activation_failure",
    "retired_setup_response",
    "load_catalog",
    "catalog_with_packs",
    "catalog_with_profiles",
    "resolve_profile",
    "dynamic_profile_edges",
    "denied",
)

_EXPECTED_RUNTIME_CLASS_METHODS = (
    "bundled_profile_root",
    "host_resource_root",
    *METHODS[:-1],
    "activation_store",
    METHODS[-1],
    "is_reconfirmation_required",
    "is_resolution_denied",
    "is_activation_lock_timeout",
    "active_profile",
)

_EXCLUDED_RUNTIME_METHODS = (
    "bundled_profile_root",
    "host_resource_root",
    "activation_store",
    "is_reconfirmation_required",
    "is_resolution_denied",
    "is_activation_lock_timeout",
    "active_profile",
)

_EXCLUDED_RUNTIME_MODULE_DEFINITIONS = (
    "defaultspack_profile_bundle_root",
    "defaultspack_host_resource_root",
    "install_defaultspack_profile_runtime",
)

_RESOLUTION_DEFINITIONS = (
    "ProfileResolutionDenied",
    "_sha256_bytes",
    "_bundle_digest",
    "_require_digest",
    "_require_optional_pin",
    "ResolvedDefaultProfile",
    "_application_launch_identity",
    "_edge_key",
    "_authority_mode",
    "_provider_candidates",
    "dynamic_profile_edges",
    "_exact_executable_variant",
    "resolve_default_profile",
)

_RUNTIME_V4_PREFIX = "ecosystem.defaultspack.domain.runtime_v4"

_EXPECTED_NATIVE_IMPORTS = (
    (COMPOSITION, f"{COMPOSITION}::DefaultspackProfileRuntime.load_catalog",
     "from", _RUNTIME_V4_PREFIX + ".service", 0, (("BundledCatalog", ""),)),
    (COMPOSITION, f"{COMPOSITION}::DefaultspackProfileRuntime.catalog_with_packs",
     "from", _RUNTIME_V4_PREFIX + ".service", 0, (("BundledCatalog", ""),)),
    (COMPOSITION, f"{COMPOSITION}::DefaultspackProfileRuntime.catalog_with_packs",
     "from", "core_runtime.external_pack_catalog_v4", 0,
     (("load_verified_pack_executable_catalog", ""),)),
    (COMPOSITION, f"{COMPOSITION}::DefaultspackProfileRuntime.catalog_with_profiles",
     "from", _RUNTIME_V4_PREFIX + ".service", 0, (("BundledCatalog", ""),)),
    (COMPOSITION, f"{COMPOSITION}::DefaultspackProfileRuntime.resolve_profile",
     "from", _RUNTIME_V4_PREFIX + ".service", 0,
     (("resolve_default_profile", ""),)),
    (COMPOSITION, f"{COMPOSITION}::DefaultspackProfileRuntime.dynamic_profile_edges",
     "from", _RUNTIME_V4_PREFIX + ".service", 0,
     (("dynamic_profile_edges", ""),)),
    (PROFILE_RESOLUTION, f"{PROFILE_RESOLUTION}::<full AST>", "from",
     "core_runtime.profile_content_projection", 0,
     (("resolve_profile_projection", ""), ("selected_projection_roots", ""))),
    (PROFILE_RESOLUTION, f"{PROFILE_RESOLUTION}::<full AST>", "from",
     "core_runtime.profile_pack_projection", 0,
     (("admitted_projection_sources", ""),)),
)

# The source is no-follow/hash-pinned here. Native implementations stay outside
# this DATA capsule and require explicit adapters; none is copied or executed.
_DEFERRED_NATIVE_MODULES = (
    "tobkiri_runtime/ecosystem/defaultspack/domain/runtime_v4/service.py",
    "tobkiri_runtime/core_runtime/external_pack_catalog_v4.py",
    "tobkiri_runtime/core_runtime/profile_content_projection.py",
    "tobkiri_runtime/core_runtime/profile_pack_projection.py",
)

_NATIVE_IMPORT_SOURCE_PATHS = {
    "ecosystem.defaultspack.domain.runtime_v4.service":
        "tobkiri_runtime/ecosystem/defaultspack/domain/runtime_v4/service.py",
    "core_runtime.external_pack_catalog_v4":
        "tobkiri_runtime/core_runtime/external_pack_catalog_v4.py",
    "core_runtime.profile_content_projection":
        "tobkiri_runtime/core_runtime/profile_content_projection.py",
    "core_runtime.profile_pack_projection":
        "tobkiri_runtime/core_runtime/profile_pack_projection.py",
}

_ROOT_SOURCE_MODULES = (COMPOSITION, PROFILE_PORT)
# These modules mix original DATA calculation with native filesystem/admission
# reachpoints. Keep their complete source, rather than moving projection hashes,
# declarations or ordering into a native precomputed result.
PROJECTION_BOUNDARY_MODULES = (
    "tobkiri_runtime/core_runtime/profile_content_projection.py",
    "tobkiri_runtime/core_runtime/profile_pack_projection.py",
)
_CAPSULE_SOURCE_FILES = (
    EXCEPTION_TRANSPORT,
    VALUE_GRAPH_TRANSPORT,
    NATIVE_WIT,
    *DISPATCHER_SOURCES,
    DATA_FACADE_SOURCE,
    PROFILE_RESOLUTION,
    SETUP_CONTRACT,
    DOMAIN_PACKAGE_INIT,
    DEFAULTSPACK_PACKAGE_INIT,
    *PROJECTION_BOUNDARY_MODULES,
    *(f"{PROTOCOL_ROOT}/{name}" for name in PROTOCOL_MODULES),
    SCHEMA_HASHES,
    PROJECT_METADATA,
)


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _json_bytes(value: Any) -> bytes:
    return (json.dumps(value, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, allow_nan=False, sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _record(
    path: str,
    content: bytes,
    *,
    role: str,
    stored_as: str | None = None,
) -> dict[str, Any]:
    result = {"path": path, "role": role, "sha256": _sha256(content),
              "bytes": len(content)}
    if stored_as is not None:
        result["stored_as"] = stored_as
    return result


def _definition_slice(
    path: str,
    content: bytes,
    node: ast.AST,
    name: str,
    *,
    source_module_sha256: str,
    stored_as: str | None,
) -> tuple[bytes, dict[str, Any]]:
    start = 1 if isinstance(node, ast.Module) else getattr(node, "lineno", None)
    end = len(content.splitlines()) if isinstance(node, ast.Module) else getattr(node, "end_lineno", None)
    if start is None or end is None:
        raise ValueError(f"original source extent is missing for {name}")
    decorators = getattr(node, "decorator_list", ())
    if decorators:
        start = min(start, *(item.lineno for item in decorators))
    lines = content.splitlines(keepends=True)
    begin = sum(len(line) for line in lines[: start - 1])
    finish = sum(len(line) for line in lines[:end])
    source_bytes = content[begin:finish]
    if not source_bytes:
        raise ValueError(f"original source is empty for {name}")
    record = {
        "symbol": name,
        "source_path": path,
        "source_module_sha256": source_module_sha256,
        "source_start_line": start,
        "source_end_line": end,
        "source_start_byte": begin,
        "source_end_byte": finish,
        "sha256": _sha256(source_bytes),
        "bytes": len(source_bytes),
    }
    if stored_as is not None:
        record["stored_as"] = stored_as
    return source_bytes, record


def _one_class(tree: ast.Module, name: str, path: str) -> ast.ClassDef:
    matches = [item for item in ast.walk(tree)
               if isinstance(item, ast.ClassDef) and item.name == name]
    if len(matches) != 1 or matches[0] not in tree.body:
        raise ValueError(f"exactly one original class is required: {path}:{name}")
    return matches[0]


def _is_native_import(module: str) -> bool:
    return (
        module == "core_runtime"
        or module.startswith("core_runtime.")
        or module == _RUNTIME_V4_PREFIX
        or module.startswith(_RUNTIME_V4_PREFIX + ".")
    )


def _native_imports_in(node: ast.AST, path: str, scope: str) -> list[dict[str, Any]]:
    imports: list[dict[str, Any]] = []
    for child in ast.walk(node):
        if isinstance(child, ast.Import):
            for alias in child.names:
                if _is_native_import(alias.name):
                    imports.append({
                        "source_path": path,
                        "scope": scope,
                        "kind": "import",
                        "module": alias.name,
                        "level": 0,
                        "names": [[alias.name, alias.asname or ""]],
                        "source_line": child.lineno,
                    })
        elif isinstance(child, ast.ImportFrom):
            module = child.module or ""
            if _is_native_import(module):
                imports.append({
                    "source_path": path,
                    "scope": scope,
                    "kind": "from",
                    "module": module,
                    "level": child.level,
                    "names": [[alias.name, alias.asname or ""]
                              for alias in child.names],
                    "source_line": child.lineno,
                })
        elif isinstance(child, ast.Call):
            function = child.func
            dynamic_import = (
                isinstance(function, ast.Name)
                and function.id in {"__import__", "import_module"}
            ) or (
                isinstance(function, ast.Attribute)
                and function.attr == "import_module"
            )
            if dynamic_import:
                if not child.args or not isinstance(child.args[0], ast.Constant) \
                        or not isinstance(child.args[0].value, str):
                    raise ValueError(
                        f"unbounded dynamic import in Setup DATA source: {path}:{child.lineno}"
                    )
                module = child.args[0].value
                if _is_native_import(module):
                    imports.append({
                        "source_path": path,
                        "scope": scope,
                        "kind": "dynamic",
                        "module": module,
                        "level": 0,
                        "names": [],
                        "source_line": child.lineno,
                    })
    return imports


def _native_import_signature(record: dict[str, Any]) -> tuple[Any, ...]:
    return (
        record["source_path"],
        record["scope"],
        record["kind"],
        record["module"],
        record["level"],
        tuple(tuple(item) for item in record["names"]),
    )


def _validate_native_import_inventory(
    composition_tree: ast.Module,
    runtime: ast.ClassDef,
    resolution_tree: ast.Module,
) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for name in METHODS:
        matches = [node for node in runtime.body
                   if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                   and node.name == name]
        if len(matches) != 1:
            raise ValueError(f"native import scan cannot select method: {name}")
        records.extend(_native_imports_in(
            matches[0], COMPOSITION,
            f"{COMPOSITION}::DefaultspackProfileRuntime.{name}",
        ))
    records.extend(_native_imports_in(
        resolution_tree, PROFILE_RESOLUTION, f"{PROFILE_RESOLUTION}::<full AST>",
    ))
    records.sort(key=lambda item: (
        item["source_path"], item["scope"], item["source_line"],
        item["module"], item["kind"],
    ))

    actual = sorted(_native_import_signature(item) for item in records)
    if actual != sorted(_EXPECTED_NATIVE_IMPORTS):
        raise ValueError(
            "undeclared native core_runtime/service import in Setup DATA source"
        )
    for record in records:
        source_path = _NATIVE_IMPORT_SOURCE_PATHS.get(record["module"])
        if source_path is None:
            raise ValueError(
                f"native import has no declared source pin: {record['module']}"
            )
        record["source_module_path"] = source_path
    required_paths = {item["source_module_path"] for item in records}
    if required_paths != set(_DEFERRED_NATIVE_MODULES):
        raise ValueError("native import source pin inventory is incomplete")
    return records


def validate_native_import_sources(
    composition_source: bytes,
    resolution_source: bytes,
) -> list[dict[str, Any]]:
    """Fail closed if selected source adds an undeclared native lazy import."""
    composition_tree = ast.parse(composition_source, filename=COMPOSITION)
    runtime = _one_class(
        composition_tree, "DefaultspackProfileRuntime", COMPOSITION,
    )
    resolution_tree = ast.parse(resolution_source, filename=PROFILE_RESOLUTION)
    return _validate_native_import_inventory(
        composition_tree, runtime, resolution_tree,
    )


def _schema_paths() -> tuple[str, ...]:
    directory = _directory(ROOT / SCHEMA_DIRECTORY)
    try:
        names = sorted(
            name for name in os.listdir(directory)
            if name.endswith(".schema.json")
        )
    finally:
        os.close(directory)
    if not names:
        raise ValueError("protocol schema directory is empty")
    return tuple(f"{SCHEMA_DIRECTORY}/{name}" for name in names)


def _read_sources() -> dict[str, bytes]:
    paths = (
        *_ROOT_SOURCE_MODULES,
        *_CAPSULE_SOURCE_FILES,
        *(path for path in _DEFERRED_NATIVE_MODULES
          if path not in PROJECTION_BOUNDARY_MODULES),
        *_NATIVE_KIT_SOURCES,
        SERVICE_INITIALIZER,
        *_schema_paths(),
    )
    if len(paths) != len(set(paths)):
        raise ValueError("source capsule input list contains duplicate paths")
    return {path: bounded_read(ROOT / path) for path in paths}


def _check_schema_hash_metadata(sources: dict[str, bytes]) -> tuple[str, ...]:
    try:
        metadata = json.loads(sources[SCHEMA_HASHES])
        pins = metadata["files"]
    except (KeyError, TypeError, json.JSONDecodeError) as error:
        raise ValueError("protocol schema hash metadata is malformed") from error
    schema_paths = _schema_paths()
    schema_names = {Path(path).name for path in schema_paths}
    if not isinstance(pins, dict) or set(pins) != schema_names:
        raise ValueError("protocol schema inventory differs from schema hash metadata")
    for path in schema_paths:
        expected = pins.get(Path(path).name)
        actual = "sha256:" + _sha256(sources[path])
        if expected != actual:
            raise ValueError(f"protocol schema hash metadata is stale: {path}")
    return schema_paths


def _extract(sources: dict[str, bytes]) -> dict[str, bytes]:
    schema_paths = set(_schema_paths())
    composition_tree = ast.parse(sources[COMPOSITION], filename=COMPOSITION)
    runtime = _one_class(
        composition_tree, "DefaultspackProfileRuntime", COMPOSITION,
    )
    original_methods = [node.name for node in runtime.body
                        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))]
    if tuple(original_methods) != _EXPECTED_RUNTIME_CLASS_METHODS:
        raise ValueError("Defaultspack Profile runtime method inventory changed")

    port_tree = ast.parse(sources[PROFILE_PORT], filename=PROFILE_PORT)
    decision_class = _one_class(port_tree, "SetupActivationDecision", PROFILE_PORT)
    resolution_tree = ast.parse(sources[PROFILE_RESOLUTION], filename=PROFILE_RESOLUTION)
    resolution_defs = [
        node for node in resolution_tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
    ]
    if tuple(node.name for node in resolution_defs) != _RESOLUTION_DEFINITIONS:
        raise ValueError("Profile resolution top-level definition inventory changed")
    native_imports = _validate_native_import_inventory(
        composition_tree, runtime, resolution_tree,
    )

    output: dict[str, bytes] = {
        f"source/{path}": sources[path]
        for path in (*_CAPSULE_SOURCE_FILES, *sorted(schema_paths))
    }
    definitions: list[dict[str, Any]] = []
    excluded_definitions: list[dict[str, Any]] = []
    for name in METHODS:
        matches = [node for node in runtime.body
                   if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                   and node.name == name]
        if len(matches) != 1 or not isinstance(matches[0], ast.FunctionDef):
            raise ValueError(f"exactly one synchronous original method is required: {name}")
        stored_as = f"definitions/profile_runtime/{name}.py"
        content, record = _definition_slice(
            COMPOSITION, sources[COMPOSITION], matches[0], name,
            source_module_sha256=_sha256(sources[COMPOSITION]),
            stored_as=stored_as,
        )
        ast.parse(b"class _CapsuleSyntaxOnly:\n" + content, filename=stored_as)
        output[stored_as] = content
        definitions.append(record)

    for name in _EXCLUDED_RUNTIME_METHODS:
        matches = [node for node in runtime.body
                   if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                   and node.name == name]
        if len(matches) != 1:
            raise ValueError(f"exactly one excluded runtime method is required: {name}")
        _, record = _definition_slice(
            COMPOSITION, sources[COMPOSITION], matches[0], name,
            source_module_sha256=_sha256(sources[COMPOSITION]),
            stored_as=None,
        )
        record["disposition"] = "excluded_host_or_native_integration"
        excluded_definitions.append(record)

    for name in _EXCLUDED_RUNTIME_MODULE_DEFINITIONS:
        matches = [node for node in composition_tree.body
                   if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                   and node.name == name]
        if len(matches) != 1:
            raise ValueError(f"exactly one excluded runtime module definition is required: {name}")
        _, record = _definition_slice(
            COMPOSITION, sources[COMPOSITION], matches[0], name,
            source_module_sha256=_sha256(sources[COMPOSITION]),
            stored_as=None,
        )
        record["disposition"] = "excluded_native_root_or_registration"
        excluded_definitions.append(record)

    decision_bytes, record = _definition_slice(
        PROFILE_PORT, sources[PROFILE_PORT], decision_class,
        "SetupActivationDecision",
        source_module_sha256=_sha256(sources[PROFILE_PORT]),
        stored_as="definitions/SetupActivationDecision.py",
    )
    ast.parse(decision_bytes, filename=record["stored_as"])
    output[record["stored_as"]] = decision_bytes
    definitions.append(record)

    service_tree = ast.parse(sources[SERVICE_SOURCE], filename=SERVICE_SOURCE)
    service_nodes = [(name, _one_class(service_tree, name, SERVICE_SOURCE))
                     for name in SERVICE_DATA_CLASSES]
    selector, = [node for node in service_tree.body
                 if isinstance(node, ast.FunctionDef)
                 and node.name == "project_runtime_launch_selector"]
    service_nodes.append((selector.name, selector))
    for symbol, module, level in (
        ("bundle_catalog_reexports", "tobkiri_protocol.bundle_catalog", 0),
        ("profile_resolution_reexports", "profile_resolution", 2),
        ("canonical_reexports", "tobkiri_protocol.canonical", 0),
    ):
        matches = [node for node in service_tree.body
                   if isinstance(node, ast.ImportFrom)
                   and node.module == module and node.level == level]
        if len(matches) != 1:
            raise ValueError(f"original service DATA import changed: {symbol}")
        service_nodes.append((symbol, matches[0]))
    for symbol, node in service_nodes:
        stored_as = f"definitions/service_data/{symbol}.py"
        content, record = _definition_slice(
            SERVICE_SOURCE, sources[SERVICE_SOURCE], node, symbol,
            source_module_sha256=_sha256(sources[SERVICE_SOURCE]), stored_as=stored_as,
        )
        ast.parse(content, filename=stored_as)
        output[stored_as] = content
        definitions.append(record)
    initializer = ast.parse(sources[SERVICE_INITIALIZER], filename=SERVICE_INITIALIZER)
    content, record = _definition_slice(
        SERVICE_INITIALIZER, sources[SERVICE_INITIALIZER], initializer,
        "runtime_v4_package_initializer",
        source_module_sha256=_sha256(sources[SERVICE_INITIALIZER]),
        stored_as="definitions/service_data/runtime_v4_package_initializer.py",
    )
    output[record["stored_as"]] = content
    definitions.append(record)

    resolution_output = f"source/{PROFILE_RESOLUTION}"
    for node in resolution_defs:
        content, record = _definition_slice(
            PROFILE_RESOLUTION, sources[PROFILE_RESOLUTION], node, node.name,
            source_module_sha256=_sha256(sources[PROFILE_RESOLUTION]),
            stored_as=resolution_output,
        )
        if sources[PROFILE_RESOLUTION][
            record["source_start_byte"]:record["source_end_byte"]
        ] != content:
            raise ValueError(f"Profile resolution source slice changed: {node.name}")
        definitions.append(record)

    # Keep all protocol schemas which the original validator's schema store
    # can inspect. The `schema_hashes.json` pins the exact complete inventory.
    provenance_sources = []
    for path in sorted(sources):
        stored_as = (
            f"source/{path}"
            if path in _CAPSULE_SOURCE_FILES or path in schema_paths
            else None
        )
        role = (
            "selected_original_module" if path in _ROOT_SOURCE_MODULES else
            "protocol_schema" if path in schema_paths else
            "protocol_schema_metadata" if path == SCHEMA_HASHES else
            "project_metadata" if path == PROJECT_METADATA else
            "native_exception_codec_source" if path in _NATIVE_KIT_SOURCES else
            "mixed_source_with_captured_data_facade"
            if path in (SERVICE_SOURCE, SERVICE_INITIALIZER) else
            "verbatim_data_with_native_io_boundary"
            if path in PROJECTION_BOUNDARY_MODULES else
            "verbatim_data_dependency" if stored_as is not None else
            "deferred_native_adapter_source"
        )
        provenance_sources.append(_record(path, sources[path], role=role,
                                          stored_as=stored_as))

    metadata: dict[str, Any] = {
        "format": "io.tobkiri.harness-native-source-capsule.v1",
        "generator": "scripts/wasm/prepare_harness_native.py",
        "scope": (
            "verbatim selected Setup DATA source capsule; native imports "
            "remain hash-pinned and unconnected"
        ),
        "sources": provenance_sources,
        "definitions": definitions,
        "excluded_definitions": excluded_definitions,
        "service_data_facade": {
            "source": SERVICE_SOURCE,
            "original_package_initializer": SERVICE_INITIALIZER,
            "generator": DATA_FACADE_SOURCE,
            "selected_classes": list(SERVICE_DATA_CLASSES),
            "selected_functions": ["project_runtime_launch_selector"],
            "original_data_reexports_retained": True,
            "native_activation_state_in_guest": False,
            "native_exports_connected": False,
            "whole_service_compatible": False,
        },
        "native_imports": native_imports,
        "exception_transport": {
            "format": "tobkiri.native-exception/1",
            "guest_materializer": EXCEPTION_TRANSPORT,
            "rust_sources": list(_NATIVE_KIT_SOURCES),
            "native_python_runtime_required": False,
            "arbitrary_native_classes_supported": False,
            "host_path_fields_supported": False,
            "native_inspection_ports_connected": False,
            "guest_materializer_executed_in_wasm": False,
        },
        "selected_methods": list(METHODS),
        "excluded_runtime_methods": list(_EXCLUDED_RUNTIME_METHODS),
        "excluded_runtime_module_definitions": list(
            _EXCLUDED_RUNTIME_MODULE_DEFINITIONS
        ),
        "deferred_native_data_adapters": [
            {
                "methods": [
                    "setup_profile_preview", "load_catalog", "catalog_with_packs",
                    "catalog_with_profiles",
                ],
                "import": "ecosystem.defaultspack.domain.runtime_v4.service.BundledCatalog",
                "required_behavior": (
                    "bind the original data catalog class; never substitute a "
                    "synthetic catalog type"
                ),
            },
            {
                "method": "load_catalog",
                "import": "ecosystem.defaultspack.domain.runtime_v4.service.BundledCatalog.load",
                "required_behavior": (
                    "use the explicit ordinal DATA reader adapter; "
                    "preserve native validation"
                ),
            },
            {
                "method": "catalog_with_packs",
                "import": (
                    "core_runtime.external_pack_catalog_v4."
                    "load_verified_pack_executable_catalog"
                ),
                "required_behavior": (
                    "connect the native-established finite DATA facade; "
                    "do not stub or no-op, or verify/grant in guest"
                ),
            },
            {
                "methods": ["resolve_profile", "dynamic_profile_edges"],
                "import": "ecosystem.defaultspack.domain.runtime_v4.service",
                "required_behavior": (
                    "bind the verbatim profile_resolution.py functions "
                    "and captured catalog DATA"
                ),
            },
            {
                "method": "resolve_profile",
                "function": "resolve_profile_projection",
                "import": "core_runtime.profile_content_projection.resolve_profile_projection",
                "required_behavior": (
                    "preserve the original guest DATA inventory, metadata and "
                    "digest calculations; perform only real IO and identity "
                    "inspection natively at the original occurrence"
                ),
            },
            {
                "method": "resolve_profile",
                "function": "selected_projection_roots",
                "import": "core_runtime.profile_content_projection.selected_projection_roots",
                "required_behavior": (
                    "preserve original selection order and guest DATA checks; "
                    "perform actual root IO and repeated identity verification "
                    "natively at each original occurrence"
                ),
            },
            {
                "method": "resolve_profile",
                "function": "admitted_projection_sources",
                "import": "core_runtime.profile_pack_projection.admitted_projection_sources",
                "required_behavior": (
                    "keep the original guest loop, including duplicate selection "
                    "occurrences; native Pack admission remains unconnected "
                    "until bound to its actual original owner and reachpoint"
                ),
            },
        ],
        "captured_input_data_requirements": [
            {
                "module": PROFILE_RESOLUTION,
                "required_behavior": (
                    "supply immutable CapturedPath inputs for bundle lock "
                    "and platform artifacts; never convert them to OS paths"
                ),
            },
            {
                "module": f"{PROTOCOL_ROOT}/bundle_catalog.py",
                "required_behavior": (
                    "route catalog entries through the explicit ordinal DATA "
                    "reader while retaining the original catalog checks"
                ),
            },
        ],
        "external_runtime_dependencies": ["jsonschema", "packaging"],
        "native_inspection_boundary": {
            "projection_sources": list(PROJECTION_BOUNDARY_MODULES),
            "guest_data_calculations_preserved": True,
            "native_precomputed_projection_results_allowed": False,
            "native_observations_at_original_reachpoints": True,
            "preserve_rereads_duplicate_occurrences_and_failure_order": True,
            "inspection_bindings_connected": False,
            "authority_granted_by_source_capture": False,
        },
        "original_value_transport": {
            "guest_registry": VALUE_GRAPH_TRANSPORT,
            "wit": NATIVE_WIT,
            "original_classes_from_trusted_bootstrap_only": True,
            "native_binding_refs_confer_authority": False,
            "shared_identity_is_not_value_memoization": True,
            "input_ownership_is_explicit": True,
            "wasm_execution_performed": False,
            "whole_export_dispatch_connected": False,
            "dispatcher_sources": list(DISPATCHER_SOURCES),
            "fixed_method_dispatch_source_present": True,
            "actual_component_export_dispatch_executed": False,
            "original_module_closure_connected": False,
            "exception_fields_disclosed": False,
            "exception_reference_is_restoration_proof": False,
        },
        "source_capsule_only": True,
        "dependency_closure_executable": False,
        "native_projection_io_ported": False,
        "native_pack_admission_ported": False,
        "build_performed": False,
        "wasm_execution_performed": False,
        "production_enabled": False,
        "whole_paths_proof": False,
        "native_authority_module_created": False,
        "capsule_is_wasm_evidence": False,
        "method_rewrites": [],
    }
    metadata["metadata_sha256"] = _sha256(_canonical_json_bytes(metadata))
    output["provenance.json"] = _json_bytes(metadata)
    return output


def captured() -> dict[str, bytes]:
    """Return a byte-exact source capsule dictionary without executing source."""
    sources = _read_sources()
    _check_schema_hash_metadata(sources)
    return _extract(sources)


def _write(descriptor: int, relative: str, content: bytes) -> None:
    """Create one new capsule file through an anchored directory descriptor."""
    if (not relative or relative.startswith("/") or "\\" in relative or "\0" in relative
            or any(part in ("", ".", "..") for part in relative.split("/"))):
        raise ValueError("capsule file path must be relative and normalized")
    path = PurePosixPath(relative)
    directory = os.dup(descriptor)
    target = None
    try:
        for part in path.parts[:-1]:
            try:
                os.mkdir(part, mode=0o700, dir_fd=directory)
            except FileExistsError:
                pass
            child = os.open(
                part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                dir_fd=directory,
            )
            os.close(directory)
            directory = child
        target = os.open(
            path.name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600, dir_fd=directory,
        )
        remaining = memoryview(content)
        while remaining:
            written = os.write(target, remaining)
            if written <= 0:
                raise ValueError("incomplete source capsule write")
            remaining = remaining[written:]
    finally:
        if target is not None:
            os.close(target)
        os.close(directory)


def _publish_fresh(output: Path, files: dict[str, bytes]) -> None:
    if ".." in output.parts or any(character in str(output) for character in "\\:\0"):
        raise ValueError("fresh normalized capsule destination required")
    output = output.absolute()
    parent = descriptor = None
    try:
        parent = _directory(output.parent)
        try:
            os.stat(output.name, dir_fd=parent, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            raise FileExistsError("source capsule destination must be fresh")
        os.mkdir(output.name, mode=0o700, dir_fd=parent)
        descriptor = os.open(
            output.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
            dir_fd=parent,
        )
        for name, data in sorted(files.items()):
            _write(descriptor, name, data)
        actual = os.stat(output.name, dir_fd=parent, follow_symlinks=False)
        opened = os.fstat(descriptor)
        if (actual.st_dev, actual.st_ino) != (opened.st_dev, opened.st_ino):
            raise ValueError("source capsule stage changed during publication")
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if parent is not None:
            os.close(parent)


def stage(output: Path) -> dict[str, Any]:
    """Publish a fresh private source capsule, not a build plan or artifact."""
    files = captured()
    if captured() != files:
        raise ValueError("original Setup sources changed during capsule staging")
    _publish_fresh(Path(output), files)
    return json.loads(files["provenance.json"])


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    metadata = stage(args.output)
    print(json.dumps({
        "source_capsule_staged": True,
        "metadata_sha256": metadata["metadata_sha256"],
        "build_performed": False,
        "wasm_execution_performed": False,
        "production_enabled": False,
        "whole_paths_proof": False,
        "capsule_is_wasm_evidence": False,
    }, sort_keys=True))


if __name__ == "__main__":
    main()
