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

from .data_facade import DataFacade, assemble as assemble_data_facade


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
_SCHEMA_DIRECTORY = "tobkiri_runtime/tobkiri_protocol/schemas"
_NATIVE_IMPORT_SOURCE_ROLE = "native_import_original_source"
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
    "tobkiri_runtime/core_runtime/__init__.py",
    "tobkiri_runtime/core_runtime/resolved_profile_scope.py",
    "tobkiri_runtime/tobkiri_host/__init__.py",
    "tobkiri_runtime/tobkiri_host/artifact_compiler.py",
    "tobkiri_runtime/tobkiri_host/contracts.py",
    "tobkiri_runtime/tobkiri_host/errors.py",
    "tobkiri_runtime/tobkiri_host/models.py",
    "tobkiri_runtime/ecosystem/defaultspack/domain/runtime_v4/_record_validation_cache.py",
    "tobkiri_runtime/tobkiri_protocol/secure_persistence.py",
    "tobkiri_runtime/tobkiri_protocol/durability.py",
    "tobkiri_runtime/tobkiri_protocol/platform_paths.py",
    "tobkiri_runtime/tobkiri_protocol/windows_private_file.py",
)
_IMPLICIT_NAMESPACE_PACKAGES = (
    "ecosystem",
    "ecosystem.defaultspack",
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
                    or sum(map(len, lines[:end_line])) != end):
                raise ValueError(f"definition extent differs from source: {symbol}")
            if stored_as == source_record["stored_as"]:
                if content != original:
                    raise ValueError(f"whole-module definition source changed: {symbol}")
            elif content != excerpt:
                raise ValueError(f"definition bytes differ from source extent: {symbol}")
        else:
            excerpt = content
        if len(excerpt) != size or _digest(excerpt) != digest:
            raise ValueError(f"definition bytes differ from their record: {symbol}")

    if set(capsule) != expected_files:
        raise ValueError("capsule files do not match the source and definition records")
    return sources


def _validate_schema_resources(
    capsule: Mapping[str, bytes], sources: dict[str, dict[str, Any]],
) -> dict[str, str]:
    """Bind every packaged protocol schema to the original hash inventory."""
    record = sources.get(_SCHEMA_HASHES_SOURCE)
    if record is None or record.get("stored_as") is None:
        raise ValueError("protocol schema hash source is missing")
    try:
        metadata = json.loads(capsule[record["stored_as"]])
        pins = metadata["files"]
    except (KeyError, TypeError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("protocol schema hash source is malformed") from error
    if not isinstance(pins, dict) or any(
        not isinstance(name, str) or not isinstance(digest, str)
        for name, digest in pins.items()
    ):
        raise ValueError("protocol schema hash inventory is malformed")
    prefix = _SCHEMA_DIRECTORY + "/"
    schemas = {
        path.rsplit("/", 1)[-1]: row
        for path, row in sources.items()
        if path.startswith(prefix) and path.endswith(".schema.json")
    }
    if set(schemas) != set(pins):
        raise ValueError("protocol schema source inventory differs from its hash index")
    for name, row in schemas.items():
        stored_as = row.get("stored_as")
        content = capsule.get(stored_as) if isinstance(stored_as, str) else None
        if not isinstance(content, bytes):
            raise ValueError(f"protocol schema source is missing: {name}")
        actual = "sha256:" + _digest(content)
        if pins[name] != actual:
            raise ValueError(f"protocol schema source differs from its hash pin: {name}")
    return {name: str(pins[name]) for name in sorted(pins)}


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
    for path in (_COMPOSITION_SOURCE, _PROFILE_PORT_SOURCE,
                 "tobkiri_runtime/ecosystem/defaultspack/domain/runtime_v4/service.py",
                 "tobkiri_runtime/ecosystem/defaultspack/domain/runtime_v4/__init__.py"):
        if sources[path].get("stored_as") is None:
            raise ValueError(f"complete original module source is missing: {path}")
    for item in metadata.get("native_imports", []):
        path = item.get("source_module_path") if isinstance(item, dict) else None
        if path is not None and path not in sources:
            raise ValueError(f"native import source record is missing: {path}")
        if path is not None and sources[path].get("stored_as") is None:
            raise ValueError(f"native import original source is missing: {path}")

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


def _original_modules(
    methods: list[tuple[str, bytes, dict[str, Any]]], decision_bytes: bytes,
    composition: bytes, port: bytes,
) -> tuple[bytes, bytes]:
    """Validate and retain complete original modules, including their imports."""
    try:
        composition_tree = ast.parse(composition, filename=_COMPOSITION_PATH)
        port_tree = ast.parse(port, filename=_PROFILE_PORT_PATH)
    except SyntaxError as error:
        raise ValueError("assembled source module does not parse") from error

    runtime = _one_class(composition_tree, _RUNTIME_SYMBOL, _COMPOSITION_PATH)
    original_methods = [item for item in runtime.body
                        if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))]
    selected_methods = [item for item in original_methods if item.name in _METHODS]
    if [item.name for item in selected_methods] != list(_METHODS):
        raise ValueError("original selected method order or inventory changed")
    for (name, raw, _record_item), original_node in zip(
        methods, selected_methods, strict=True,
    ):
        original = _method_node(raw, name)
        if (ast.dump(original, include_attributes=False) != ast.dump(
                original_node, include_attributes=False,
        ) or _extent(composition, original_node)["sha256"] != _digest(raw)):
            raise ValueError(f"original selected method differs from its source: {name}")

    decision = _one_class(port_tree, _DECISION_SYMBOL, _PROFILE_PORT_PATH)
    if _extent(port, decision)["sha256"] != _digest(decision_bytes):
        raise ValueError("original SetupActivationDecision differs from its source record")
    decision_imports = [item for item in composition_tree.body
                        if isinstance(item, ast.ImportFrom)
                        and item.module == "core_runtime.profile_runtime_port"
                        and any(alias.name == _DECISION_SYMBOL for alias in item.names)]
    if len(decision_imports) != 1:
        raise ValueError("original composition must import one shared SetupActivationDecision class")
    return composition, port


def _module_name(path: str) -> str:
    relative = path.removeprefix("tobkiri_runtime/")
    if relative.endswith("/__init__.py"):
        return relative[:-len("/__init__.py")].replace("/", ".")
    return relative[:-3].replace("/", ".") if relative.endswith(".py") else ""


def _module_initialization_imports(
    tree: ast.Module, package: str,
) -> list[tuple[str, int]]:
    """Return imports executed at module initialization, excluding type-only code."""
    return [
        (target, line)
        for target, line, phase in _import_occurrences(tree, package)
        if phase == "module_initialization"
    ]


def _import_occurrences(
    tree: ast.Module, package: str,
) -> list[tuple[str, int, str]]:
    """Find direct, lazy, conditional, and type-only import occurrences."""
    result: list[tuple[str, int, str]] = []

    def visit(statements: list[ast.stmt], phase: str) -> None:
        for node in statements:
            if isinstance(node, ast.Import):
                result.extend((alias.name, node.lineno, phase) for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                target = (resolve_name("." * node.level + (node.module or ""), package)
                          if node.level else node.module or "")
                if target:
                    result.append((target, node.lineno, phase))
                if node.module is None and target:
                    result.extend((f"{target}.{alias.name}", node.lineno, phase)
                                  for alias in node.names)
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                visit(node.body, "runtime_lazy")
            elif isinstance(node, ast.If):
                if (isinstance(node.test, ast.Name)
                        and node.test.id == "TYPE_CHECKING"):
                    visit(node.body, "type_checking")
                    visit(node.orelse, phase)
                else:
                    visit(node.body, "conditional_initialization"
                          if phase == "module_initialization" else phase)
                    visit(node.orelse, "conditional_initialization"
                          if phase == "module_initialization" else phase)
            elif isinstance(node, (ast.Try, ast.TryStar)):
                visit(node.body, phase)
                for handler in node.handlers:
                    visit(handler.body, phase)
                visit(node.orelse, phase)
                visit(node.finalbody, phase)
            elif isinstance(node, (ast.With, ast.AsyncWith, ast.For, ast.AsyncFor,
                                   ast.While, ast.ClassDef)):
                visit(node.body, phase)
                if isinstance(node, (ast.For, ast.AsyncFor, ast.While)):
                    visit(node.orelse, phase)
    visit(tree.body, "module_initialization")
    return result


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
    initialization_missing = set()
    initialization_imports = []
    unresolved_imports = []
    dynamic_import_sites = []
    stdlib_imports = set()
    for module, (path, content) in modules.items():
        tree = ast.parse(content, filename=path)
        package = module if path.endswith("/__init__.py") else module.rpartition(".")[0]
        for target, line, phase in _import_occurrences(tree, package):
            if target.split(".", 1)[0] in sys.stdlib_module_names:
                stdlib_imports.add(target.split(".", 1)[0])
            if (target not in available
                    and target.split(".", 1)[0] not in sys.stdlib_module_names
                    and target.split(".", 1)[0] not in external):
                missing.add(target)
                unresolved_imports.append({
                    "source_path": path,
                    "module": module,
                    "target": target,
                    "line": line,
                    "phase": phase,
                })
        for target, line in _module_initialization_imports(tree, package):
            row = {"source_path": path, "module": module, "target": target, "line": line}
            initialization_imports.append(row)
            if (target not in available
                    and target.split(".", 1)[0] not in sys.stdlib_module_names
                    and target.split(".", 1)[0] not in external):
                initialization_missing.add(target)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            function = node.func
            is_dynamic_import = (
                isinstance(function, ast.Name)
                and function.id in {"__import__", "import_module"}
            ) or (
                isinstance(function, ast.Attribute)
                and function.attr == "import_module"
            )
            if not is_dynamic_import:
                continue
            target = None
            if node.args and isinstance(node.args[0], ast.Constant) \
                    and isinstance(node.args[0].value, str):
                target = node.args[0].value
                if target.startswith("."):
                    target = resolve_name(target, package)
                if target.split(".", 1)[0] in sys.stdlib_module_names:
                    stdlib_imports.add(target.split(".", 1)[0])
            dynamic_import_sites.append({
                "source_path": path,
                "module": module,
                "target": target,
                "line": node.lineno,
                "binding": "literal" if target is not None else "runtime_value",
            })
    return {
        "host_python_implementation": platform.python_implementation(),
        "host_python_version": platform.python_version(),
        "classification_basis": "AST imports, captured source paths, declared externals, and host sys.stdlib_module_names",
        "source_paths_are_not_importability_proof": True,
        "stdlib_classification_limit": "host Python names do not prove the pinned WASI producer stdlib closure",
        "missing_modules": sorted(missing),
        "unresolved_imports": sorted(
            unresolved_imports,
            key=lambda item: (item["source_path"], item["line"], item["target"]),
        ),
        "dynamic_import_sites": sorted(
            dynamic_import_sites,
            key=lambda item: (item["source_path"], item["line"]),
        ),
        "host_stdlib_modules_seen": sorted(stdlib_imports),
        "target_stdlib_importability_proven": False,
        "module_initialization_imports": initialization_imports,
        "module_initialization_missing_modules": sorted(initialization_missing),
        "module_initialization_complete": not initialization_missing,
    }


def _source_index(files: Mapping[str, bytes]) -> tuple[bytes, str, int, int]:
    """Build a data-only index for staged original runtime modules."""
    prefix = "source-package/tobkiri_runtime/"
    modules: list[tuple[str, str, str]] = []
    path_modules = []
    for package_path, content in files.items():
        if not package_path.startswith(prefix) or not package_path.endswith(".py"):
            continue
        runtime_path = package_path.removeprefix(prefix)
        module = _module_name("tobkiri_runtime/" + runtime_path)
        if not module:
            continue
        tree = ast.parse(content, filename=package_path)
        modules.append((module, runtime_path, _digest(content)))
        if any(
            (isinstance(node, ast.Import)
             and any(alias.name == "pathlib" or alias.name.startswith("pathlib.")
                     for alias in node.names))
            or (isinstance(node, ast.ImportFrom)
                and (node.module == "pathlib" or (node.module or "").startswith("pathlib.")))
            for node in ast.walk(tree)
        ):
            path_modules.append(module)
    modules.sort()
    path_modules = sorted(set(path_modules))
    canonical = _canonical_json_bytes({
        "MODULES": modules,
        "PATH_MODULES": path_modules,
    })
    checksum = _digest(canonical)
    lines = ["MODULES = ("]
    lines.extend(f"    {item!r}," for item in modules)
    lines.extend([
        ")",
        "PATH_MODULES = (",
    ])
    lines.extend(f"    {module!r}," for module in path_modules)
    lines.extend([
        ")",
        f"INDEX_SHA256 = {checksum!r}",
        "",
    ])
    return "\n".join(lines).encode("utf-8"), checksum, len(modules), len(path_modules)


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
    composition: bytes, port: bytes, files: dict[str, bytes], facade: DataFacade,
    source_index_sha256: str, source_index_module_count: int,
    source_index_path_module_count: int,
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
        target = f"source-package/{path}"
        native_dependencies.append({
            "module": _module_name(path), "source_path": path,
            "status": ("source_present_but_native_io_or_admission_unconnected"
                       if stored_as else "original_data_facade_native_exports_unconnected"
                       if target in facade.files else "native_source_deferred"),
            "package_path": target if stored_as or target in facade.files else None,
        })

    source_inventory = [
        {"path": path, "role": item["role"], "sha256": item["sha256"],
         "bytes": item["bytes"], "capsule_stored_as": item.get("stored_as"),
         "package_path": f"source-package/{path}" if item.get("stored_as") else None,
         "native_import_source": item.get("role") == _NATIVE_IMPORT_SOURCE_ROLE}
        for path, item in sorted(sources.items())
    ]
    namespace_packages = [name for name in _IMPLICIT_NAMESPACE_PACKAGES
                          if f"tobkiri_runtime/{name.replace('.', '/')}/__init__.py"
                          not in sources]
    runtime_class = _one_class(ast.parse(composition), _RUNTIME_SYMBOL, _COMPOSITION_PATH)
    decision_class = _one_class(ast.parse(port), _DECISION_SYMBOL, _PROFILE_PORT_PATH)
    module_extents: dict[str, dict[str, Any]] = {}
    for definition in definitions:
        generated_path = definition["generated_path"]
        module_extents.setdefault(generated_path, {})[definition["symbol"]] = (
            definition["generated_extent"]
        )
    packaged_modules = []
    prefix = "source-package/tobkiri_runtime/"
    for path, content in sorted(files.items()):
        if not path.startswith(prefix) or not path.endswith(".py"):
            continue
        source_path = path.removeprefix("source-package/")
        source_record = sources.get(source_path)
        row = {
            "path": path,
            "module": _module_name(source_path),
            "sha256": _digest(content),
            "bytes": len(content),
            "source_kind": "verbatim_original" if source_record else "generated",
        }
        if source_record:
            row["source_path"] = source_path
            row["source_sha256"] = source_record["sha256"]
        if path in module_extents:
            row["definition_extents"] = module_extents[path]
        if path == _COMPOSITION_PATH:
            row["class"] = _RUNTIME_SYMBOL
            row["class_extent"] = _extent(composition, runtime_class)
        elif path == _PROFILE_PORT_PATH:
            row["class"] = _DECISION_SYMBOL
            row["class_extent"] = _extent(port, decision_class)
        packaged_modules.append(row)
    service_custody = dict(facade.custody)
    service_custody.update({
        "original_initializer_preserved_as": (
            "source-package/tobkiri_runtime/ecosystem/defaultspack/"
            "domain/runtime_v4/__init__.py"
        ),
        "original_service_package_path": (
            "source-package/tobkiri_runtime/ecosystem/defaultspack/"
            "domain/runtime_v4/service.py"
        ),
        "original_initializer_package_path": (
            "source-package/tobkiri_runtime/ecosystem/defaultspack/"
            "domain/runtime_v4/__init__.py"
        ),
        "complete_original_modules_staged": True,
        "facade_modules_used_for_execution": False,
    })
    schema_hashes_path = f"source-package/{_SCHEMA_HASHES_SOURCE}"
    try:
        schema_pins = json.loads(files[schema_hashes_path])["files"]
    except (KeyError, TypeError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("staged protocol schema hash inventory is malformed") from error
    schema_resources = []
    for name, digest in sorted(schema_pins.items()):
        path = f"source-package/{_SCHEMA_DIRECTORY}/{name}"
        content = files.get(path)
        if not isinstance(content, bytes) or "sha256:" + _digest(content) != digest:
            raise ValueError(f"staged protocol schema does not match its hash pin: {name}")
        schema_resources.append({"path": path, "sha256": digest, "bytes": len(content)})
    dependency_scan = _dependency_scan(files, external)
    if not dependency_scan["module_initialization_complete"]:
        missing = ", ".join(dependency_scan["module_initialization_missing_modules"])
        raise ValueError(f"captured module-initialization import closure is incomplete: {missing}")
    return _json_bytes({
        "format": _PACKAGE_FORMAT,
        "input_metadata_sha256": metadata_checksum,
        "source_package_assembled": True,
        "build_performed": False,
        "wasm_execution_performed": False,
        "native_inspection_ports_connected": False,
        "whole_setup_compatible": False,
        "production_enabled": False,
        "generated_modules": packaged_modules,
        "sources": source_inventory,
        "definitions": definitions,
        "native_imports": capsule_metadata.get("native_imports", []),
        "original_service_package": service_custody,
        "unconnected_native_dependencies": native_dependencies,
        "external_runtime_dependencies": external,
        "schema_resources": schema_resources,
        "dependency_scan": dependency_scan,
        "source_index": {
            "path": "source-package/harness_native_source_index.py",
            "sha256": _digest(files["source-package/harness_native_source_index.py"]),
            "modules": source_index_module_count,
            "path_modules": source_index_path_module_count,
            "index_sha256": source_index_sha256,
        },
        "uncaptured_package_initializers": [],
        "implicit_namespace_packages": namespace_packages,
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
    _validate_schema_resources(capsule, sources)
    methods, decision_bytes, _decision_record = _validate_methods_and_decision(
        capsule, metadata, sources,
    )
    composition_source = capsule[sources[_COMPOSITION_SOURCE]["stored_as"]]
    port_source = capsule[sources[_PROFILE_PORT_SOURCE]["stored_as"]]
    composition, port = _original_modules(
        methods, decision_bytes, composition_source, port_source,
    )
    facade = assemble_data_facade(capsule, metadata, _extent)

    files: dict[str, bytes] = {}
    for path, record in sources.items():
        if record.get("stored_as") is not None:
            target = f"source-package/{path}"
            if target in files:
                raise ValueError(f"duplicate package source path: {target}")
            files[target] = capsule[record["stored_as"]]
    for target, expected in ((_COMPOSITION_PATH, composition), (_PROFILE_PORT_PATH, port)):
        if files.get(target) != expected:
            raise ValueError(f"complete original module was not staged verbatim: {target}")
    service_path = (
        "source-package/tobkiri_runtime/ecosystem/defaultspack/"
        "domain/runtime_v4/service.py"
    )
    initializer_path = (
        "source-package/tobkiri_runtime/ecosystem/defaultspack/"
        "domain/runtime_v4/__init__.py"
    )
    if files.get(service_path) != capsule[sources[
            "tobkiri_runtime/ecosystem/defaultspack/domain/runtime_v4/service.py"
    ]["stored_as"]]:
        raise ValueError("complete original service module was not staged verbatim")
    if files.get(initializer_path) != capsule[sources[
            "tobkiri_runtime/ecosystem/defaultspack/domain/runtime_v4/__init__.py"
    ]["stored_as"]]:
        raise ValueError("complete original service initializer was not staged verbatim")
    source_index, index_sha256, index_module_count, path_module_count = _source_index(files)
    files["source-package/harness_native_source_index.py"] = source_index
    definitions = _definition_extents(metadata, sources, files, composition, port)
    files["source-package/provenance.json"] = _manifest(
        metadata, checksum, sources, definitions, composition, port, files, facade,
        index_sha256, index_module_count, path_module_count,
    )
    return files
