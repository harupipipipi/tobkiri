#!/usr/bin/env python3
"""Observe the canonical provider compiler functions without copying them."""

from __future__ import annotations

import argparse
import copy
import decimal
import hashlib
import importlib
import inspect
import json
import math
import os
from pathlib import Path
import platform
import sys
import unicodedata
from types import ModuleType
from typing import Any, Callable


SCHEMA = "tobkiri.issue1520.provider-model-access-observations.v1"
SPECIAL_FLOAT_KEY = "$python_float"
OPERATIONS: dict[str, Callable[..., Any]]
MODULES: dict[str, ModuleType]


def _sha256(path: Path) -> str | None:
    try:
        if not path.is_file():
            return None
        digest = hashlib.sha256()
        with path.open("rb") as source:
            for block in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(block)
        return "sha256:" + digest.hexdigest()
    except OSError:
        return None


def _path_record(path_value: str | None) -> dict[str, Any]:
    if not path_value or path_value.startswith("<"):
        return {"path": path_value, "sha256": None, "bytes": None}
    path = Path(path_value).resolve()
    try:
        length = path.stat().st_size if path.is_file() else None
    except OSError:
        length = None
    return {"path": str(path), "sha256": _sha256(path), "bytes": length}


def _decode_special_values(value: Any, *, allow_nonfinite: bool) -> Any:
    if isinstance(value, list):
        return [
            _decode_special_values(item, allow_nonfinite=allow_nonfinite)
            for item in value
        ]
    if isinstance(value, dict):
        if set(value) == {SPECIAL_FLOAT_KEY}:
            spelling = value[SPECIAL_FLOAT_KEY]
            if spelling != "nan" or not allow_nonfinite:
                raise ValueError("unsupported special value in input cases")
            return float("nan")
        return {
            key: _decode_special_values(item, allow_nonfinite=allow_nonfinite)
            for key, item in value.items()
        }
    return value


def _json_projection(value: Any) -> Any:
    """Keep JSON-native values and visibly tag Python-only nonfinite floats."""
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if math.isnan(value):
            return {SPECIAL_FLOAT_KEY: "nan"}
        if math.isinf(value):
            return {SPECIAL_FLOAT_KEY: "infinity" if value > 0 else "-infinity"}
        return value
    if isinstance(value, list):
        return [_json_projection(item) for item in value]
    if isinstance(value, dict):
        if any(not isinstance(key, str) for key in value):
            raise TypeError("oracle values must use string mapping keys")
        return {key: _json_projection(item) for key, item in value.items()}
    raise TypeError(f"oracle value is not JSON-compatible: {type(value).__name__}")


def _type_tree(value: Any) -> dict[str, Any]:
    if value is None:
        return {"type": "NoneType"}
    if isinstance(value, bool):
        return {"type": "bool"}
    if isinstance(value, int):
        return {"type": "int"}
    if isinstance(value, float):
        return {
            "type": "float",
            "repr": repr(value),
            "hex": value.hex(),
            "finite": math.isfinite(value),
        }
    if isinstance(value, str):
        return {"type": "str"}
    if isinstance(value, list):
        return {"type": "list", "items": [_type_tree(item) for item in value]}
    if isinstance(value, dict):
        return {
            "type": "dict",
            "entries": [
                {"key": key, "value": _type_tree(item)}
                for key, item in value.items()
            ],
        }
    raise TypeError(f"oracle value has unsupported type: {type(value).__name__}")


def _key_order_tree(value: Any) -> Any:
    if isinstance(value, dict):
        if any(not isinstance(key, str) for key in value):
            raise TypeError("oracle mappings must use string keys")
        return {
            "keys": list(value),
            "values": {key: _key_order_tree(item) for key, item in value.items()},
        }
    if isinstance(value, list):
        return [_key_order_tree(item) for item in value]
    return None


def _same_structure(left: Any, right: Any) -> bool:
    """Compare values, scalar types, float signs, and dict insertion order."""
    if type(left) is not type(right):
        return False
    if isinstance(left, float):
        if math.isnan(left):
            return math.isnan(right)
        return left.hex() == right.hex()
    if isinstance(left, dict):
        return (
            list(left) == list(right)
            and all(_same_structure(left[key], right[key]) for key in left)
        )
    if isinstance(left, list):
        return len(left) == len(right) and all(
            _same_structure(a, b) for a, b in zip(left, right, strict=True)
        )
    return left == right


def _decimal_context() -> dict[str, Any]:
    context = decimal.getcontext()
    return {
        "precision": context.prec,
        "rounding": context.rounding,
        "emin": context.Emin,
        "emax": context.Emax,
        "capitals": context.capitals,
        "clamp": context.clamp,
        "flags": [
            {"signal": signal.__name__, "set": bool(enabled)}
            for signal, enabled in context.flags.items()
        ],
        "traps": [
            {"signal": signal.__name__, "enabled": bool(enabled)}
            for signal, enabled in context.traps.items()
        ],
    }


def _import_source(runtime_root: Path) -> tuple[dict[str, Callable[..., Any]], dict[str, ModuleType]]:
    required = (
        runtime_root / "tobkiri_protocol/provider_compiler/model_access.py",
        runtime_root / "tobkiri_protocol/provider_compiler/native_filters.py",
    )
    missing = [str(path) for path in required if not path.is_file()]
    if missing:
        raise FileNotFoundError("canonical source file(s) missing: " + ", ".join(missing))

    sys.path.insert(0, str(runtime_root))
    protocol_package = importlib.import_module("tobkiri_protocol")
    compiler_package = importlib.import_module("tobkiri_protocol.provider_compiler")
    model_access = importlib.import_module("tobkiri_protocol.provider_compiler.model_access")
    native_filters = importlib.import_module("tobkiri_protocol.provider_compiler.native_filters")

    modules = {
        "tobkiri_protocol": protocol_package,
        "tobkiri_protocol.provider_compiler": compiler_package,
        "tobkiri_protocol.provider_compiler.model_access": model_access,
        "tobkiri_protocol.provider_compiler.native_filters": native_filters,
    }
    operations = {
        "normalize_model_access": model_access.normalize_model_access,
        "effective_model_access": model_access.effective_model_access,
        "model_is_allowed": model_access.model_is_allowed,
        "compile_connection_parameters": model_access.compile_connection_parameters,
        "is_official_openrouter": native_filters.is_official_openrouter,
        "normalize_native_filters": native_filters.normalize_native_filters,
        "discovery_url": native_filters.discovery_url,
        "apply_native_routing": native_filters.apply_native_routing,
    }

    expected_files = {
        "normalize_model_access": required[0],
        "effective_model_access": required[0],
        "model_is_allowed": required[0],
        "compile_connection_parameters": required[0],
        "is_official_openrouter": required[1],
        "normalize_native_filters": required[1],
        "discovery_url": required[1],
        "apply_native_routing": required[1],
    }
    for name, function in operations.items():
        actual_file = Path(inspect.getsourcefile(function) or "").resolve()
        if actual_file != expected_files[name].resolve():
            raise RuntimeError(
                f"{name} did not resolve to canonical source: {actual_file}"
            )
    return operations, modules


def _source_closure(runtime_root: Path) -> list[dict[str, Any]]:
    found: dict[Path, dict[str, Any]] = {}
    root = runtime_root.resolve()
    for module in tuple(sys.modules.values()):
        path_value = getattr(module, "__file__", None)
        if not isinstance(path_value, str) or path_value.startswith("<"):
            continue
        path = Path(path_value).resolve()
        try:
            relative = path.relative_to(root).as_posix()
        except ValueError:
            continue
        if path.is_file():
            found[path] = {
                "path": relative,
                "sha256": _sha256(path),
                "bytes": path.stat().st_size,
            }
    return sorted(found.values(), key=lambda item: item["path"])


def _stdlib_provenance() -> dict[str, Any]:
    names = ("re", "typing", "decimal", "_decimal", "urllib", "urllib.parse")
    modules: list[dict[str, Any]] = []
    for name in names:
        module = importlib.import_module(name)
        record = _path_record(getattr(module, "__file__", None))
        record["module"] = name
        record["version"] = getattr(module, "__version__", None)
        modules.append(record)
    decimal_module = importlib.import_module("decimal")
    decimal_c = importlib.import_module("_decimal")
    return {
        "modules": modules,
        "decimal_version": getattr(decimal_module, "__version__", None),
        "libmpdec_version": getattr(decimal_module, "__libmpdec_version__", None),
        "_decimal_libmpdec_version": getattr(decimal_c, "__libmpdec_version__", None),
    }


def _function_provenance(
    operations: dict[str, Callable[..., Any]], runtime_root: Path,
) -> list[dict[str, Any]]:
    root = runtime_root.resolve()
    result = []
    for name, function in operations.items():
        source_file = Path(inspect.getsourcefile(function) or "").resolve()
        line_number = inspect.getsourcelines(function)[1]
        result.append({
            "operation": name,
            "module": function.__module__,
            "qualname": function.__qualname__,
            "source": source_file.relative_to(root).as_posix(),
            "first_line": line_number,
        })
    return result


def _observe_case(
    case: dict[str, Any], operations: dict[str, Callable[..., Any]], sequence: int,
) -> dict[str, Any]:
    operation = case["operation"]
    if operation not in operations:
        raise ValueError(f"unknown source function in cases: {operation}")
    comparison = case.get("comparison", "native-wasm-finite")
    if comparison not in {"native-wasm-finite", "source-only-nonfinite"}:
        raise ValueError(f"unknown comparison mode: {comparison}")
    arguments = _decode_special_values(
        case["arguments"], allow_nonfinite=comparison == "source-only-nonfinite",
    )
    if not isinstance(arguments, dict):
        raise ValueError("case arguments must be a JSON object")

    before = copy.deepcopy(arguments)
    before_view = _json_projection(before)
    before_types = _type_tree(before)
    before_order = _key_order_tree(before)
    context_before = _decimal_context()
    try:
        returned = operations[operation](**arguments)
    except Exception as error:  # Preserve the original class and message verbatim.
        outcome: dict[str, Any] = {
            "status": "source-failure",
            "exception_class": type(error).__name__,
            "message": str(error),
        }
        qualified_exception_class = (
            type(error).__module__ + "." + type(error).__qualname__
        )
        returned_view = None
        returned_python_type = None
        returned_types = None
        returned_order = None
        python_json_default = None
    else:
        returned_view = _json_projection(returned)
        returned_python_type = type(returned).__name__
        returned_types = _type_tree(returned)
        returned_order = _key_order_tree(returned)
        outcome = {"status": "ok", "value": returned_view}
        qualified_exception_class = None
        # This preserves Python's normal encoder behavior as supplementary evidence.
        python_json_default = json.dumps(returned, allow_nan=True)
    # This is the thin JSON string returned by the candidate WIT operation.
    # The companion trees below retain Python type, order, and exact float data.
    outcome_json = json.dumps(
        outcome, ensure_ascii=False, separators=(",", ":"), allow_nan=False,
    )

    after_view = _json_projection(arguments)
    after_types = _type_tree(arguments)
    after_order = _key_order_tree(arguments)
    context_after = _decimal_context()
    unchanged = _same_structure(before, arguments)

    return {
        "sequence": sequence,
        "id": case["id"],
        "operation": operation,
        "comparison": comparison,
        "arguments_before": before_view,
        "arguments_before_python_type_tree": before_types,
        "arguments_before_mapping_key_order": before_order,
        "outcome": outcome,
        "outcome_json": outcome_json,
        "outcome_exception_qualified_class": qualified_exception_class,
        "outcome_python_type": returned_python_type,
        "outcome_python_type_tree": returned_types,
        "outcome_mapping_key_order": returned_order,
        "outcome_python_json_default": python_json_default,
        "arguments_after": after_view,
        "arguments_after_python_type_tree": after_types,
        "arguments_after_mapping_key_order": after_order,
        "inputs_unchanged": unchanged,
        "decimal_context_before": context_before,
        "decimal_context_after": context_after,
    }


def _load_json(path: Path) -> Any:
    def reject_constant(value: str) -> None:
        raise ValueError(f"non-standard JSON numeric constant is forbidden: {value}")

    with path.open("r", encoding="utf-8") as source:
        return json.load(source, parse_constant=reject_constant)


def _runtime_identity() -> dict[str, Any]:
    executable = Path(sys.executable)
    resolved_executable = executable.resolve()
    return {
        "implementation": sys.implementation.name,
        "version": sys.version,
        "version_info": {
            "major": sys.version_info.major,
            "minor": sys.version_info.minor,
            "micro": sys.version_info.micro,
            "releaselevel": sys.version_info.releaselevel,
            "serial": sys.version_info.serial,
        },
        "cache_tag": sys.implementation.cache_tag,
        "hexversion": sys.hexversion,
        "executable": str(executable),
        "resolved_executable": str(resolved_executable),
        "executable_sha256": _sha256(resolved_executable),
        "platform": sys.platform,
        "os_name": os.name,
        "platform_details": platform.platform(),
        "python_compiler": platform.python_compiler(),
        "unicode_version": unicodedata.unidata_version,
    }


def _write_new_private_output(path: Path, content: str) -> None:
    """Create one new 0600 receipt under a non-symlink 0700 directory."""
    import stat

    if ".." in path.parts:
        raise ValueError("output path must not contain parent traversal")
    output_path = path if path.is_absolute() else Path.cwd() / path
    output_path = Path(os.path.abspath(output_path))
    parent = output_path.parent

    def reject_symlink_components(target: Path) -> None:
        current = Path(target.anchor)
        for part in target.parts[1:]:
            current = current / part
            try:
                mode = current.lstat().st_mode
            except FileNotFoundError:
                continue
            if stat.S_ISLNK(mode):
                raise ValueError(f"symlink path component is forbidden: {current}")

    reject_symlink_components(parent)
    missing: list[Path] = []
    cursor = parent
    while True:
        try:
            cursor.lstat()
            break
        except FileNotFoundError:
            missing.append(cursor)
            if cursor.parent == cursor:
                raise FileNotFoundError(f"no existing ancestor for output parent: {parent}")
            cursor = cursor.parent

    for directory in reversed(missing):
        os.mkdir(directory, 0o700)
        directory_mode = directory.lstat().st_mode
        if stat.S_ISLNK(directory_mode) or not stat.S_ISDIR(directory_mode):
            raise ValueError(f"output parent is not a real directory: {directory}")
        if stat.S_IMODE(directory_mode) != 0o700:
            raise PermissionError(f"new output parent is not mode 0700: {directory}")

    reject_symlink_components(parent)
    parent_mode = parent.lstat().st_mode
    if stat.S_ISLNK(parent_mode) or not stat.S_ISDIR(parent_mode):
        raise ValueError(f"output parent is not a real directory: {parent}")
    if stat.S_IMODE(parent_mode) != 0o700:
        raise PermissionError(f"output parent must have mode 0700: {parent}")

    try:
        output_path.lstat()
    except FileNotFoundError:
        pass
    else:
        raise FileExistsError(f"refusing to replace existing output: {output_path}")

    directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    directory_flags |= getattr(os, "O_NOFOLLOW", 0)
    parent_fd = os.open(parent, directory_flags)
    output_fd: int | None = None
    output_created = False
    output_identity: tuple[int, int] | None = None
    try:
        opened_parent = os.fstat(parent_fd)
        current_parent = parent.lstat()
        if (
            not stat.S_ISDIR(opened_parent.st_mode)
            or stat.S_ISLNK(current_parent.st_mode)
            or (opened_parent.st_dev, opened_parent.st_ino)
            != (current_parent.st_dev, current_parent.st_ino)
            or stat.S_IMODE(opened_parent.st_mode) != 0o700
        ):
            raise PermissionError("output parent changed or is not a private directory")

        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        output_fd = os.open(output_path.name, flags, 0o600, dir_fd=parent_fd)
        output_created = True
        created_stat = os.fstat(output_fd)
        output_identity = (created_stat.st_dev, created_stat.st_ino)
        os.fchmod(output_fd, 0o600)
        if stat.S_IMODE(os.fstat(output_fd).st_mode) != 0o600:
            raise PermissionError("new observation file is not mode 0600")
        with os.fdopen(output_fd, "w", encoding="utf-8", newline="\n") as output_file:
            output_fd = None
            output_file.write(content)
            output_file.flush()
            os.fsync(output_file.fileno())
    except BaseException:
        if output_fd is not None:
            os.close(output_fd)
        if output_created and output_identity is not None:
            try:
                current = os.stat(
                    output_path.name, dir_fd=parent_fd, follow_symlinks=False,
                )
            except FileNotFoundError:
                pass
            else:
                if (current.st_dev, current.st_ino) == output_identity:
                    os.unlink(output_path.name, dir_fd=parent_fd)
        raise
    finally:
        os.close(parent_fd)


def main() -> int:
    script_dir = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--runtime-root", required=True,
        help="canonical tobkiri_runtime source root containing tobkiri_protocol/",
    )
    parser.add_argument(
        "--cases", default=str(script_dir / "provider-model-access.cases.v1.json"),
        help="input-only case manifest (defaults to the adjacent fixture)",
    )
    parser.add_argument(
        "--output", default=str(script_dir / "provider-model-access.observations.v1.json"),
        help="observation receipt to write (defaults beside this generator)",
    )
    args = parser.parse_args()

    runtime_root = Path(args.runtime_root).resolve(strict=True)
    cases_path = Path(args.cases).resolve(strict=True)
    requested_output = Path(args.output)
    if ".." in requested_output.parts:
        raise ValueError("output path must not contain parent traversal")
    output_path = (
        requested_output if requested_output.is_absolute()
        else Path.cwd() / requested_output
    )
    manifest = _load_json(cases_path)
    if not isinstance(manifest, dict) or manifest.get("schema") != (
        "tobkiri.issue1520.provider-model-access-cases.v1"
    ):
        raise ValueError("unrecognized provider-model-access case manifest")
    cases = manifest.get("cases")
    if not isinstance(cases, list) or not cases:
        raise ValueError("case manifest must contain a nonempty cases array")
    ids = [case.get("id") for case in cases if isinstance(case, dict)]
    if len(ids) != len(cases) or len(set(ids)) != len(ids) or any(
        not isinstance(case.get("id"), str) or not case.get("id")
        for case in cases
    ):
        raise ValueError("case IDs must be present and unique")
    if any("expected" in case or "expected_result" in case for case in cases):
        raise ValueError("input case manifest must not contain expected results")

    operations, modules = _import_source(runtime_root)
    if manifest.get("source_functions") != list(operations):
        raise ValueError("case manifest function list does not match the direct source bindings")
    for module in modules.values():
        if Path(module.__file__).resolve().parent != runtime_root.resolve() and (
            runtime_root.resolve() not in Path(module.__file__).resolve().parents
        ):
            raise RuntimeError(f"module imported outside canonical runtime root: {module.__name__}")

    initial_decimal_context = _decimal_context()
    observations = [
        _observe_case(case, operations, sequence)
        for sequence, case in enumerate(cases)
    ]
    final_decimal_context = _decimal_context()
    source_closure = _source_closure(runtime_root)
    if not source_closure:
        raise RuntimeError("canonical Python source closure could not be recorded")

    provenance = _stdlib_provenance()
    output = {
        "schema": SCHEMA,
        "producer": "direct-canonical-python-source-call",
        "runtime_root": str(runtime_root),
        "cases": {
            "path": cases_path.name,
            "sha256": "sha256:" + hashlib.sha256(cases_path.read_bytes()).hexdigest(),
            "bytes": cases_path.stat().st_size,
        },
        "runtime": _runtime_identity(),
        "source_functions": _function_provenance(operations, runtime_root),
        "source_closure": source_closure,
        "stdlib_provenance": provenance,
        "decimal_context_before": initial_decimal_context,
        "decimal_context_after": final_decimal_context,
        "source_constants": {
            "model_access_version": modules[
                "tobkiri_protocol.provider_compiler.model_access"
            ].VERSION,
            "native_filter_capability_revision": modules[
                "tobkiri_protocol.provider_compiler.native_filters"
            ].CAPABILITY_REVISION,
        },
        "observation_count": len(observations),
        "observations": observations,
    }
    encoded = json.dumps(output, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    _write_new_private_output(output_path, encoded)
    print(json.dumps({
        "output": str(output_path),
        "sha256": "sha256:" + hashlib.sha256(encoded.encode("utf-8")).hexdigest(),
        "observation_count": len(observations),
        "runtime": output["runtime"],
    }, ensure_ascii=False, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
