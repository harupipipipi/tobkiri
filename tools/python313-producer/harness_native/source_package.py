"""Assemble captured Defaultspack source slices; do not execute generated modules."""

from __future__ import annotations

import ast
import hashlib
import json
import platform
import re
import sys
from collections.abc import Mapping
from importlib.util import resolve_name
from typing import Any


_CAPSULE_FORMAT = "io.tobkiri.harness-native-source-capsule.v1"
_PACKAGE_FORMAT = "io.tobkiri.harness-native-source-package.v1"
_COMPOSITION_SOURCE = (
    "tobkiri_runtime/ecosystem/defaultspack/defaultspack/"
    "profile_runtime_composition.py"
)
_PROFILE_PORT_SOURCE = "tobkiri_runtime/core_runtime/profile_runtime_port.py"
_RESOLUTION_SOURCE = "tobkiri_runtime/ecosystem/defaultspack/domain/profile_resolution.py"
_CANONICAL_SOURCE = "tobkiri_runtime/tobkiri_protocol/canonical.py"
_SETUP_CONTRACT_SOURCE = (
    "tobkiri_runtime/ecosystem/defaultspack/defaultspack/setup_contract.py"
)
_SCHEMA_HASHES_SOURCE = "tobkiri_runtime/tobkiri_protocol/schema_hashes.json"
_COMPOSITION_PATH = (
    "source-package/tobkiri_runtime/ecosystem/defaultspack/defaultspack/"
    "profile_runtime_composition.py"
)
_PROFILE_PORT_PATH = "source-package/tobkiri_runtime/core_runtime/profile_runtime_port.py"
_DECISION_SYMBOL = "SetupActivationDecision"
_RUNTIME_SYMBOL = "DefaultspackProfileRuntime"
_METHODS = (
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
_REQUIRED_STORED_SOURCES = (
    _RESOLUTION_SOURCE,
    _CANONICAL_SOURCE,
    _SETUP_CONTRACT_SOURCE,
    _SCHEMA_HASHES_SOURCE,
)
_MISSING_PACKAGE_INITIALIZERS = (
    "tobkiri_runtime/ecosystem/__init__.py",
    "tobkiri_runtime/ecosystem/defaultspack/__init__.py",
)
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _safe_path(value: Any) -> str:
    if (not isinstance(value, str) or not value or value.startswith("/")
            or "\\" in value or "\0" in value or ":" in value
            or any(part in ("", ".", "..") for part in value.split("/"))):
        raise ValueError("capsule paths must be normalized relative paths")
    return value


def _json_bytes(value: Any) -> bytes:
    return (json.dumps(value, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, allow_nan=False,
                      sort_keys=True, separators=(",", ":")).encode("utf-8")


def _metadata(capsule: Mapping[str, bytes]) -> tuple[dict[str, Any], str]:
    raw = capsule.get("provenance.json")
    if not isinstance(raw, bytes):
        raise ValueError("capsule provenance.json is missing")
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("capsule provenance.json is malformed") from error
    if not isinstance(value, dict) or value.get("format") != _CAPSULE_FORMAT:
        raise ValueError("unsupported source capsule format")
    checksum = value.get("metadata_sha256")
    if not isinstance(checksum, str) or _SHA256.fullmatch(checksum) is None:
        raise ValueError("capsule metadata checksum is missing or malformed")
    unsigned = dict(value)
    del unsigned["metadata_sha256"]
    if _digest(_canonical_json_bytes(unsigned)) != checksum:
        raise ValueError("capsule metadata checksum is invalid")
    return value, checksum


def _validate_capsule(
    capsule: Mapping[str, bytes], metadata: dict[str, Any]
) -> dict[str, dict[str, Any]]:
    source_rows = metadata.get("sources")
    definition_rows = metadata.get("definitions")
    if not isinstance(source_rows, list) or not isinstance(definition_rows, list):
        raise ValueError("capsule source or definition inventory is malformed")

    sources: dict[str, dict[str, Any]] = {}
    expected_files = {"provenance.json"}
    for record in source_rows:
        if not isinstance(record, dict):
            raise ValueError("capsule source record is malformed")
        path = _safe_path(record.get("path"))
        if path in sources:
            raise ValueError(f"duplicate source record: {path}")
        digest, size = record.get("sha256"), record.get("bytes")
        if (not isinstance(digest, str) or _SHA256.fullmatch(digest) is None
                or not isinstance(size, int) or isinstance(size, bool) or size < 0):
            raise ValueError(f"invalid source hash or size: {path}")
        stored_as = record.get("stored_as")
        if stored_as is not None:
            stored_as = _safe_path(stored_as)
            content = capsule.get(stored_as)
            if not isinstance(content, bytes):
                raise ValueError(f"stored source is missing: {stored_as}")
            if len(content) != size or _digest(content) != digest:
                raise ValueError(f"stored source does not match its record: {path}")
            expected_files.add(stored_as)
        if not isinstance(record.get("role"), str):
            raise ValueError(f"source role is missing: {path}")
        sources[path] = record

    seen: set[tuple[str, str]] = set()
    for record in definition_rows:
        if not isinstance(record, dict):
            raise ValueError("capsule definition record is malformed")
        path = _safe_path(record.get("source_path"))
        symbol = record.get("symbol")
        key = path, symbol
        if not isinstance(symbol, str) or not symbol or key in seen:
            raise ValueError("duplicate or malformed definition record")
        seen.add(key)
        source_record = sources.get(path)
        if source_record is None:
            raise ValueError(f"definition source record is missing: {path}:{symbol}")
        if record.get("source_module_sha256") != source_record["sha256"]:
            raise ValueError(f"definition source hash differs from its source record: {symbol}")
        start, end = record.get("source_start_byte"), record.get("source_end_byte")
        size, digest = record.get("bytes"), record.get("sha256")
        if (any(not isinstance(n, int) or isinstance(n, bool) for n in (start, end, size))
                or start < 0 or end <= start or size != end - start
                or not isinstance(digest, str) or _SHA256.fullmatch(digest) is None):
            raise ValueError(f"definition extent is malformed: {symbol}")
        stored_as = record.get("stored_as")
        if not isinstance(stored_as, str):
            raise ValueError(f"definition bytes are not stored: {symbol}")
        stored_as = _safe_path(stored_as)
        content = capsule.get(stored_as)
        if not isinstance(content, bytes):
            raise ValueError(f"definition bytes are missing: {stored_as}")
        expected_files.add(stored_as)
        if source_record.get("stored_as") is not None:
            original = capsule[source_record["stored_as"]]
            excerpt = original[start:end]
            lines = original.splitlines(keepends=True)
            start_line, end_line = record.get("source_start_line"), record.get("source_end_line")
            if (not isinstance(start_line, int) or not isinstance(end_line, int)
                    or start_line <= 0 or end_line < start_line
                    or sum(map(len, lines[:start_line - 1])) != start
                    or sum(map(len, lines[:end_line])) != end
                    or content != original):
                raise ValueError(f"definition extent differs from source: {symbol}")
        else:
            excerpt = content
        if len(excerpt) != size or _digest(excerpt) != digest:
            raise ValueError(f"definition bytes differ from their record: {symbol}")

    if set(capsule) != expected_files:
        raise ValueError("capsule files do not match the source and definition records")
    return sources


def _definition_bytes(
    capsule: Mapping[str, bytes],
    sources: dict[str, dict[str, Any]],
    record: dict[str, Any],
) -> bytes:
    source = sources[record["source_path"]]
    if source.get("stored_as") is not None:
        return capsule[source["stored_as"]][
            record["source_start_byte"]:record["source_end_byte"]
        ]
    return capsule[record["stored_as"]]


def _one_class(tree: ast.Module, name: str, path: str) -> ast.ClassDef:
    found = [item for item in tree.body if isinstance(item, ast.ClassDef) and item.name == name]
    if len(found) != 1:
        raise ValueError(f"expected exactly one {name} class in {path}")
    return found[0]


def _extent(source: bytes, node: ast.AST) -> dict[str, Any]:
    start_line, end_line = node.lineno, node.end_lineno
    decorators = getattr(node, "decorator_list", ())
    if decorators:
        start_line = min(start_line, *(item.lineno for item in decorators))
    lines = source.splitlines(keepends=True)
    start = sum(map(len, lines[:start_line - 1]))
    end = sum(map(len, lines[:end_line]))
    raw = source[start:end]
    return {
        "start_line": start_line, "end_line": end_line,
        "start_byte": start, "end_byte": end,
        "bytes": len(raw), "sha256": _digest(raw),
    }


def _method_node(raw: bytes, name: str) -> ast.FunctionDef:
    try:
        tree = ast.parse(b"class _CapsuleMethod:\n" + raw, filename=name)
    except SyntaxError as error:
        raise ValueError(f"invalid original method syntax: {name}") from error
    methods = (tree.body[0].body if len(tree.body) == 1
               and isinstance(tree.body[0], ast.ClassDef) else [])
    if len(methods) != 1 or not isinstance(methods[0], ast.FunctionDef):
        raise ValueError(f"expected one synchronous method: {name}")
    if methods[0].name != name:
        raise ValueError(f"method symbol does not match source: {name}")
    return methods[0]


def _record(metadata: dict[str, Any], path: str, symbol: str) -> dict[str, Any]:
    matches = [item for item in metadata["definitions"]
               if item.get("source_path") == path and item.get("symbol") == symbol]
    if len(matches) != 1:
        raise ValueError(f"expected one definition record for {symbol}")
    return matches[0]


def _validate_methods_and_decision(
    capsule: Mapping[str, bytes],
    metadata: dict[str, Any],
    sources: dict[str, dict[str, Any]],
) -> tuple[list[tuple[str, bytes, dict[str, Any]]], bytes, dict[str, Any]]:
    if metadata.get("selected_methods") != list(_METHODS):
        raise ValueError("selected method order differs from the original inventory")
    methods_in_metadata = [item.get("symbol") for item in metadata["definitions"]
                           if item.get("source_path") == _COMPOSITION_SOURCE]
    if methods_in_metadata != list(_METHODS):
        raise ValueError("method definition records are missing, extra, or reordered")
    for path in (_COMPOSITION_SOURCE, _PROFILE_PORT_SOURCE, *_REQUIRED_STORED_SOURCES):
        if path not in sources:
            raise ValueError(f"required source record is missing: {path}")
    for path in _REQUIRED_STORED_SOURCES:
        if sources[path].get("stored_as") is None:
            raise ValueError(f"required verbatim source is missing: {path}")
    for item in metadata.get("native_imports", []):
        path = item.get("source_module_path") if isinstance(item, dict) else None
        if path is not None and path not in sources:
            raise ValueError(f"native import source record is missing: {path}")

    selected: list[tuple[str, bytes, dict[str, Any]]] = []
    for name in _METHODS:
        record = _record(metadata, _COMPOSITION_SOURCE, name)
        raw = _definition_bytes(capsule, sources, record)
        _method_node(raw, name)
        selected.append((name, raw, record))
    self_refs = {
        node.attr for _name, raw, _record_item in selected
        for node in ast.walk(_method_node(raw, _name))
        if isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name) and node.value.id == "self"
    }
    if self_refs - set(_METHODS):
        raise ValueError("selected methods contain an unprovided self reference")

    decision_record = _record(metadata, _PROFILE_PORT_SOURCE, _DECISION_SYMBOL)
    decision_bytes = _definition_bytes(capsule, sources, decision_record)
    try:
        decision_tree = ast.parse(decision_bytes, filename=_DECISION_SYMBOL)
    except SyntaxError as error:
        raise ValueError("SetupActivationDecision source syntax is invalid") from error
    decision = _one_class(decision_tree, _DECISION_SYMBOL, _DECISION_SYMBOL)
    decorator, = decision.decorator_list
    if (not isinstance(decorator, ast.Call)
            or not isinstance(decorator.func, ast.Name)
            or decorator.func.id != "dataclass"
            or len(decorator.keywords) != 1
            or decorator.keywords[0].arg != "frozen"
            or not isinstance(decorator.keywords[0].value, ast.Constant)
            or decorator.keywords[0].value.value is not True
            or not any(isinstance(item, ast.FunctionDef) and item.name == "__post_init__"
                       for item in decision.body)):
        raise ValueError("SetupActivationDecision dataclass invariant is missing")
    return selected, decision_bytes, decision_record


def _generated_modules(
    methods: list[tuple[str, bytes, dict[str, Any]]], decision_bytes: bytes,
) -> tuple[bytes, bytes]:
    preamble = (
        b"from __future__ import annotations\n\n"
        b"from pathlib import Path\n"
        b"from typing import TYPE_CHECKING, Any, Mapping\n\n"
        b"from core_runtime.profile_runtime_port import SetupActivationDecision\n"
        b"from tobkiri_protocol.canonical import canonical_digest\n\n"
        b"if TYPE_CHECKING:\n"
        b"    from core_runtime.bundled_artifact_retention_v4 import BundledArtifactOrigin\n"
        b"    from ecosystem.defaultspack.domain.runtime_v4.service import (\n"
        b"        ActivationStore,\n"
        b"        ActiveDefaultProfile,\n"
        b"        BundledCatalog,\n"
        b"    )\n\n\n"
    )
    composition = (
        preamble + b"class DefaultspackProfileRuntime:\n"
        + b"\n".join(raw for _name, raw, _record_item in methods)
    )
    port = (
        b"from __future__ import annotations\n\n"
        b"from dataclasses import dataclass\n"
        b"from pathlib import Path\n"
        b"from typing import Any, Mapping\n\n"
        + decision_bytes
    )
    try:
        composition_tree = ast.parse(composition, filename=_COMPOSITION_PATH)
        port_tree = ast.parse(port, filename=_PROFILE_PORT_PATH)
    except SyntaxError as error:
        raise ValueError("assembled source module does not parse") from error

    runtime = _one_class(composition_tree, _RUNTIME_SYMBOL, _COMPOSITION_PATH)
    generated_methods = [item for item in runtime.body
                         if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))]
    if [item.name for item in generated_methods] != list(_METHODS):
        raise ValueError("assembled method order or inventory changed")
    for (name, raw, _record_item), generated in zip(methods, generated_methods, strict=True):
        original = _method_node(raw, name)
        if ast.dump(original, include_attributes=False) != ast.dump(
            generated, include_attributes=False,
        ) or _extent(composition, generated)["sha256"] != _digest(raw):
            raise ValueError(f"assembled method differs from its source: {name}")

    decision = _one_class(port_tree, _DECISION_SYMBOL, _PROFILE_PORT_PATH)
    original_tree = ast.parse(decision_bytes, filename=_DECISION_SYMBOL)
    original_decision = _one_class(original_tree, _DECISION_SYMBOL, _DECISION_SYMBOL)
    if (ast.dump(decision, include_attributes=False)
            != ast.dump(original_decision, include_attributes=False)):
        raise ValueError("assembled SetupActivationDecision differs from its source")
    decision_imports = [item for item in composition_tree.body
                        if isinstance(item, ast.ImportFrom)
                        and item.module == "core_runtime.profile_runtime_port"
                        and any(alias.name == _DECISION_SYMBOL for alias in item.names)]
    if len(decision_imports) != 1 or any(
        isinstance(item, ast.ClassDef) and item.name == _DECISION_SYMBOL
        for item in composition_tree.body
    ):
        raise ValueError("composition must import one shared SetupActivationDecision class")
    return composition, port


def _module_name(path: str) -> str:
    relative = path.removeprefix("tobkiri_runtime/")
    if relative.endswith("/__init__.py"):
        return relative[:-len("/__init__.py")].replace("/", ".")
    return relative[:-3].replace("/", ".") if relative.endswith(".py") else ""


def _dependency_scan(files: dict[str, bytes], external: list[str]) -> dict[str, Any]:
    modules: dict[str, tuple[str, bytes]] = {}
    prefix = "source-package/tobkiri_runtime/"
    for path, content in files.items():
        if path.startswith(prefix) and path.endswith(".py"):
            module = _module_name(path[len("source-package/"):])
            if module:
                modules[module] = (path, content)
    available = {".".join(module.split(".")[:i])
                 for module in modules for i in range(1, len(module.split(".")) + 1)}

    missing = set()
    for module, (path, content) in modules.items():
        tree = ast.parse(content, filename=path)
        package = module if path.endswith("/__init__.py") else module.rpartition(".")[0]
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                targets = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                targets = [resolve_name("." * node.level + (node.module or ""), package)
                           if node.level else node.module or ""]
            else:
                continue
            missing.update(target for target in targets
                           if target not in available
                           and target.split(".", 1)[0] not in sys.stdlib_module_names
                           and target.split(".", 1)[0] not in external)
    return {
        "host_python_implementation": platform.python_implementation(),
        "host_python_version": platform.python_version(),
        "classification_basis": "AST imports, captured source paths, declared externals, and host sys.stdlib_module_names",
        "source_paths_are_not_importability_proof": True,
        "stdlib_classification_limit": "host Python names do not prove the pinned WASI producer stdlib closure",
        "missing_modules": sorted(missing),
    }


def _definition_extents(
    metadata: dict[str, Any], sources: dict[str, dict[str, Any]],
    package_files: dict[str, bytes], composition: bytes, port: bytes,
) -> list[dict[str, Any]]:
    composition_tree = ast.parse(composition, filename=_COMPOSITION_PATH)
    runtime = _one_class(composition_tree, _RUNTIME_SYMBOL, _COMPOSITION_PATH)
    method_nodes = {item.name: item for item in runtime.body if isinstance(item, ast.FunctionDef)}
    port_tree = ast.parse(port, filename=_PROFILE_PORT_PATH)
    decision_node = _one_class(port_tree, _DECISION_SYMBOL, _PROFILE_PORT_PATH)
    result = []
    for original in metadata["definitions"]:
        item = dict(original)
        path, symbol = item["source_path"], item["symbol"]
        if path == _COMPOSITION_SOURCE and symbol in method_nodes:
            item["generated_path"] = _COMPOSITION_PATH
            item["generated_extent"] = _extent(composition, method_nodes[symbol])
        elif path == _PROFILE_PORT_SOURCE and symbol == _DECISION_SYMBOL:
            item["generated_path"] = _PROFILE_PORT_PATH
            item["generated_extent"] = _extent(port, decision_node)
        elif sources[path].get("stored_as") is not None:
            target = f"source-package/{path}"
            source = package_files[target]
            item["generated_path"] = target
            item["generated_extent"] = {
                "start_line": item["source_start_line"],
                "end_line": item["source_end_line"],
                "start_byte": item["source_start_byte"],
                "end_byte": item["source_end_byte"],
                "bytes": item["bytes"],
                "sha256": _digest(source[item["source_start_byte"]:item["source_end_byte"]]),
            }
        else:
            raise ValueError(f"definition has no generated source location: {path}:{symbol}")
        result.append(item)
    return result


def _manifest(
    capsule_metadata: dict[str, Any], metadata_checksum: str,
    sources: dict[str, dict[str, Any]], definitions: list[dict[str, Any]],
    composition: bytes, port: bytes, files: dict[str, bytes],
) -> bytes:
    external = capsule_metadata.get("external_runtime_dependencies", [])
    if not isinstance(external, list) or any(not isinstance(item, str) for item in external):
        raise ValueError("external runtime dependency inventory is malformed")
    native_paths = {
        item.get("source_module_path")
        for item in capsule_metadata.get("native_imports", [])
        if isinstance(item, dict) and isinstance(item.get("source_module_path"), str)
    }
    native_dependencies = []
    for path in sorted(native_paths):
        source = sources.get(path)
        if source is None:
            raise ValueError(f"native import source record is missing: {path}")
        stored_as = source.get("stored_as")
        native_dependencies.append({
            "module": _module_name(path), "source_path": path,
            "status": ("source_present_but_native_io_or_admission_unconnected"
                       if stored_as else "native_source_deferred"),
            "package_path": f"source-package/{path}" if stored_as else None,
        })

    source_inventory = [
        {"path": path, "role": item["role"], "sha256": item["sha256"],
         "bytes": item["bytes"], "capsule_stored_as": item.get("stored_as"),
         "package_path": f"source-package/{path}" if item.get("stored_as") else None}
        for path, item in sorted(sources.items())
    ]
    missing_initializers = [path for path in _MISSING_PACKAGE_INITIALIZERS if path not in sources]
    runtime_class = _one_class(ast.parse(composition), _RUNTIME_SYMBOL, _COMPOSITION_PATH)
    decision_class = _one_class(ast.parse(port), _DECISION_SYMBOL, _PROFILE_PORT_PATH)
    return _json_bytes({
        "format": _PACKAGE_FORMAT,
        "input_metadata_sha256": metadata_checksum,
        "source_package_assembled": True,
        "build_performed": False,
        "wasm_execution_performed": False,
        "native_inspection_ports_connected": False,
        "whole_setup_compatible": False,
        "production_enabled": False,
        "generated_modules": [
            {"path": _COMPOSITION_PATH, "sha256": _digest(composition),
             "bytes": len(composition), "class": _RUNTIME_SYMBOL,
             "class_extent": _extent(composition, runtime_class)},
            {"path": _PROFILE_PORT_PATH, "sha256": _digest(port),
             "bytes": len(port), "class": _DECISION_SYMBOL,
             "class_extent": _extent(port, decision_class)},
        ],
        "sources": source_inventory,
        "definitions": definitions,
        "native_imports": capsule_metadata.get("native_imports", []),
        "unconnected_native_dependencies": native_dependencies,
        "external_runtime_dependencies": external,
        "dependency_scan": _dependency_scan(files, external),
        "uncaptured_package_initializers": missing_initializers,
        "dependency_closure_complete": False,
    })


def assemble(capsule: Mapping[str, bytes]) -> dict[str, bytes]:
    """Return generated modules and copied sources under ``source-package/``."""
    if not isinstance(capsule, Mapping) or any(
        not isinstance(path, str) or not isinstance(content, bytes)
        for path, content in capsule.items()
    ):
        raise TypeError("capsule must map relative paths to bytes")
    metadata, checksum = _metadata(capsule)
    sources = _validate_capsule(capsule, metadata)
    methods, decision_bytes, _decision_record = _validate_methods_and_decision(
        capsule, metadata, sources,
    )
    composition, port = _generated_modules(methods, decision_bytes)

    files: dict[str, bytes] = {}
    for path, record in sources.items():
        if record.get("stored_as") is not None:
            target = f"source-package/{path}"
            if target in files:
                raise ValueError(f"duplicate package source path: {target}")
            files[target] = capsule[record["stored_as"]]
    for target, content in ((_COMPOSITION_PATH, composition), (_PROFILE_PORT_PATH, port)):
        if target in files:
            raise ValueError(f"generated module collides with captured source: {target}")
        files[target] = content
    definitions = _definition_extents(metadata, sources, files, composition, port)
    files["source-package/provenance.json"] = _manifest(
        metadata, checksum, sources, definitions, composition, port, files,
    )
    return files


