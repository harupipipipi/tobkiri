"""Capture bounded observations from the unchanged original Python providers.

This is a source oracle only. It does not load WIT bindings or run Rust/Wasm.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat
import sys
import types
import unicodedata
from typing import Any


ROOT = Path(__file__).resolve().parents[3]
CODEC_PACK = "packs/local.ai-tool-bridge-ordered-python"
CODEC_V1 = f"{CODEC_PACK}/ordered_json.py"
CODEC_V2 = f"{CODEC_PACK}/ordered_json_v2.py"
CORE_WIT = "crates/tobkiri-wit/wit/core/core.wit"
MAX_SOURCE_BYTES = 8 * 1024 * 1024
MAX_CASES = 128
MAX_INPUT_BYTES_PER_CASE = 4 * 1024 * 1024 + 128
MAX_TOTAL_INPUT_BYTES = 16 * 1024 * 1024
MAX_OUTPUT_BYTES_PER_CASE = 1024 * 1024
MAX_TOTAL_OUTPUT_BYTES = 8 * 1024 * 1024
MAX_RECEIPT_BYTES = 1024 * 1024
MAX_UNICODE_TABLE_BYTES = 1024 * 1024
MAX_SCALAR_VECTOR_COUNT = 128
MAX_SCALAR_INPUT_BYTES = 1024 * 1024
MAX_SCALAR_ARTIFACT_BYTES = 1024 * 1024
MAX_EXECUTABLE_BYTES = 512 * 1024 * 1024
MAX_UNICODE_MODULE_BYTES = 64 * 1024 * 1024
CASE_ID = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")

PROVIDERS: dict[str, dict[str, str]] = {
    "usage-cost": {
        "canonical": (
            "tobkiri_runtime/ecosystem/rumi_ai_usage_pack/runtime/usage.py"
        ),
        "function": "packs/local.ai-usage-cost-python/python/"
        "original_usage_cost.py",
        "adapter": "packs/local.ai-usage-cost-python/python/"
        "usage_cost_adapter.py",
        "app": "packs/local.ai-usage-cost-python/python/app.py",
        "provenance": "packs/local.ai-usage-cost-python/python/provenance.json",
        "function_module": "original_usage_cost",
        "adapter_module": "usage_cost_adapter",
        "function_symbol": "create_cost_operation",
        "app_method": "calculate",
    },
    "stream-normalizer": {
        "canonical": (
            "tobkiri_runtime/ecosystem/rumi_ai_stream_pack/runtime/normalizer.py"
        ),
        "dispatch": "tobkiri_runtime/core_runtime/global_contract_dispatch.py",
        "function": "packs/local.ai-stream-normalizer-python/python/"
        "original_stream_normalizer.py",
        "adapter": "packs/local.ai-stream-normalizer-python/python/"
        "stream_normalizer_adapter.py",
        "app": "packs/local.ai-stream-normalizer-python/python/app.py",
        "provenance": "packs/local.ai-stream-normalizer-python/python/"
        "provenance.json",
        "function_module": "original_stream_normalizer",
        "adapter_module": "stream_normalizer_adapter",
        "function_symbol": "create_stream_normalize_operation",
        "app_method": "normalize",
    },
}


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _open_directory(path: Path) -> int:
    """Open an absolute directory without following any symlink component."""
    if not path.is_absolute() or ".." in path.parts:
        raise ValueError("directory path must be absolute without parent traversal")
    descriptor = os.open(path.anchor, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in path.parts[1:]:
            if part in ("", "."):
                continue
            child = os.open(
                part,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                dir_fd=descriptor,
            )
            os.close(descriptor)
            descriptor = child
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _read_regular_at(directory_fd: int, name: str, limit: int) -> bytes:
    """Read one stable bounded regular file without following a symlink."""
    descriptor = os.open(
        name,
        os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
        dir_fd=directory_fd,
    )
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_size > limit:
            raise ValueError("source is not a bounded regular file")
        chunks: list[bytes] = []
        remaining = limit + 1
        while remaining:
            chunk = os.read(descriptor, min(remaining, 64 * 1024))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        content = b"".join(chunks)
        after = os.fstat(descriptor)
        before_id = (
            before.st_dev, before.st_ino, before.st_size,
            before.st_mtime_ns, before.st_ctime_ns,
        )
        after_id = (
            after.st_dev, after.st_ino, after.st_size,
            after.st_mtime_ns, after.st_ctime_ns,
        )
        if len(content) > limit or len(content) != before.st_size:
            raise ValueError("source exceeded its read bound")
        if before_id != after_id:
            raise ValueError("source changed while it was read")
        return content
    finally:
        os.close(descriptor)


def _capture(path: str) -> bytes:
    """Capture one repository-relative source file safely and with a cap."""
    relative = Path(path)
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError("source path must remain within the repository")
    parent_fd = _open_directory(ROOT / relative.parent)
    try:
        return _read_regular_at(parent_fd, relative.name, MAX_SOURCE_BYTES)
    finally:
        os.close(parent_fd)


def _source_paths() -> list[str]:
    paths = [
        Path(__file__).resolve().relative_to(ROOT).as_posix(),
        "tools/quality/python_provider_compat/cases.py",
        CODEC_V1,
        CODEC_V2,
        CORE_WIT,
    ]
    for spec in PROVIDERS.values():
        paths.extend(
            spec[key]
            for key in ("canonical", "function", "adapter", "app", "provenance")
        )
        if "dispatch" in spec:
            paths.append(spec["dispatch"])
    return sorted(set(paths))


def _sources_before_import() -> tuple[dict[str, bytes], list[dict[str, Any]]]:
    contents = {path: _capture(path) for path in _source_paths()}
    records = [
        {"path": path, "bytes": len(data), "sha256": _sha256(data)}
        for path, data in sorted(contents.items())
    ]
    return contents, records


def _canonical_source_bytes(
    provenance: dict[str, Any], canonical_path: str, captured: dict[str, bytes],
) -> bytes:
    source_records = [
        item for item in provenance.get("sources", [])
        if item.get("path") == canonical_path
    ]
    if len(source_records) != 1:
        raise ValueError(f"provenance does not bind exactly one {canonical_path}")
    record = source_records[0]
    data = captured[canonical_path]
    if record.get("bytes") != len(data) or record.get("sha256") != _sha256(data):
        raise ValueError(f"canonical source disagrees with provenance: {canonical_path}")
    return data


def _verify_extraction(
    provider: str, spec: dict[str, str], captured: dict[str, bytes],
) -> None:
    """Require the staged function and codec to match their source receipt."""
    provenance = json.loads(captured[spec["provenance"]].decode("utf-8"))
    _canonical_source_bytes(provenance, spec["canonical"], captured)
    output = provenance.get("output", {})
    function_source = captured[spec["function"]]
    if (
        output.get("path") != Path(spec["function"]).name
        or output.get("bytes") != len(function_source)
        or output.get("sha256") != _sha256(function_source)
    ):
        raise ValueError(f"extracted function source disagrees with provenance: {provider}")
    for reference in provenance.get("staged_references", []):
        if reference.get("path") in (CODEC_V1, CODEC_V2):
            source = captured[reference["path"]]
            if (
                reference.get("bytes") != len(source)
                or reference.get("sha256") != _sha256(source)
            ):
                raise ValueError(f"codec source disagrees with provenance: {provider}")
    if "dispatch" in spec:
        _canonical_source_bytes(
            provenance, spec["dispatch"], captured,
        )


def _load_source_module(
    name: str, path: str, contents: dict[str, bytes],
) -> types.ModuleType:
    """Execute exactly the captured source bytes as one top-level module."""
    module = types.ModuleType(name)
    module.__file__ = str(ROOT / path)
    module.__package__ = ""
    sys.modules[name] = module
    code = compile(contents[path], path, "exec", dont_inherit=True)
    exec(code, module.__dict__)
    return module


def _load_adapter(
    provider: str, spec: dict[str, str], contents: dict[str, bytes],
) -> types.ModuleType:
    """Load the real adapter and captured source/codec modules, without WIT."""
    names = (
        "ordered_json", "ordered_json_v2", spec["function_module"],
        spec["adapter_module"],
    )
    for name in names:
        sys.modules.pop(name, None)
    _load_source_module("ordered_json", CODEC_V1, contents)
    _load_source_module("ordered_json_v2", CODEC_V2, contents)
    _load_source_module(spec["function_module"], spec["function"], contents)
    return _load_source_module(spec["adapter_module"], spec["adapter"], contents)


def _name(node: ast.AST | None) -> str:
    if isinstance(node, ast.Name):
        return node.id
    raise ValueError("app.py error handler has an unsupported exception name")


def _handler_names(node: ast.expr | None) -> tuple[str, ...]:
    if isinstance(node, ast.Name):
        return (node.id,)
    if isinstance(node, ast.Tuple):
        return tuple(_name(item) for item in node.elts)
    raise ValueError("app.py error handler is missing an explicit exception type")


def _error_from_handler(handler: ast.ExceptHandler) -> tuple[str, str]:
    for node in ast.walk(ast.Module(body=handler.body, type_ignores=[])):
        if not isinstance(node, ast.Raise) or not isinstance(node.exc, ast.Call):
            continue
        if not isinstance(node.exc.func, ast.Name) or node.exc.func.id != "Err":
            continue
        if not node.exc.args or not isinstance(node.exc.args[0], ast.Call):
            continue
        error_call = node.exc.args[0]
        if not isinstance(error_call.func, ast.Name) or error_call.func.id != "Error":
            continue
        if len(error_call.args) != 2:
            raise ValueError("app.py typed error must contain code and message")
        code_node, message_node = error_call.args
        if (
            not isinstance(code_node, ast.Attribute)
            or not isinstance(code_node.value, ast.Name)
            or code_node.value.id != "ErrorCode"
            or not isinstance(message_node, ast.Constant)
            or type(message_node.value) is not str
        ):
            raise ValueError("app.py typed error has a nonliteral mapping")
        code = code_node.attr.lower().replace("_", "-")
        return code, message_node.value
    raise ValueError("app.py error handler has no fixed Err(Error(...)) mapping")


def _app_error_map(
    app_bytes: bytes, method_name: str, wit_bytes: bytes,
) -> dict[str, dict[str, str]]:
    """Read closed error mappings from app.py without importing WIT bindings."""
    tree = ast.parse(app_bytes, filename="app.py")
    methods = [
        node for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == method_name
    ]
    if len(methods) != 1:
        raise ValueError(f"expected exactly one app method {method_name}")
    tries = [node for node in ast.walk(methods[0]) if isinstance(node, ast.Try)]
    if len(tries) != 1:
        raise ValueError(f"expected one closed error boundary in app.{method_name}")
    wit_text = wit_bytes.decode("utf-8")
    match = re.search(r"enum error-code\s*\{([^}]*)\}", wit_text)
    if match is None:
        raise ValueError("Core WIT error-code enum could not be read")
    wit_codes = {part.strip() for part in match.group(1).split(",")}
    mappings: dict[str, dict[str, str]] = {}
    for handler in tries[0].handlers:
        code, message = _error_from_handler(handler)
        if code not in wit_codes:
            raise ValueError(f"app.py maps to an unknown Core WIT code: {code}")
        for name in _handler_names(handler.type):
            if name in mappings:
                raise ValueError(f"duplicate app.py error handler for {name}")
            mappings[name] = {"code": code, "message": message}
    return mappings


def _map_exception(
    error: Exception, app_map: dict[str, dict[str, str]], case: Any,
) -> dict[str, str]:
    for cls in type(error).__mro__:
        mapping = app_map.get(cls.__name__)
        if mapping is not None:
            return mapping
    raise ValueError(
        f"unmapped source exception in {case.provider}/{case.case_id}: "
        f"{type(error).__name__}"
    ) from None


def _runtime_identity() -> dict[str, Any]:
    if sys.implementation.name != "cpython":
        raise ValueError("the source oracle requires the actual CPython runtime")
    version = sys.version_info
    int_limit = getattr(sys, "get_int_max_str_digits", lambda: None)()
    return {
        "implementation": sys.implementation.name,
        "python": {
            "version": f"{version.major}.{version.minor}.{version.micro}",
            "major": version.major,
            "minor": version.minor,
            "micro": version.micro,
        },
        "unicode_version": unicodedata.unidata_version,
        "int_max_str_digits": int_limit,
        "recursion_limit": sys.getrecursionlimit(),
    }


def _case_records(
    static_cases: tuple[Any, ...],
    contents: dict[str, bytes],
    source_manifest_sha256: str,
) -> tuple[list[dict[str, Any]], dict[str, bytes], int, int]:
    if len(static_cases) > MAX_CASES:
        raise ValueError("static case count exceeds the configured limit")
    seen: set[tuple[str, str]] = set()
    records: list[dict[str, Any]] = []
    sidecars: dict[str, bytes] = {}
    total_input = 0
    total_output = 0
    adapter_cache: dict[str, types.ModuleType] = {}
    error_maps: dict[str, dict[str, dict[str, str]]] = {}

    for case in static_cases:
        if case.provider not in PROVIDERS or not CASE_ID.fullmatch(case.case_id):
            raise ValueError("static case has an invalid provider or case ID")
        identity = (case.provider, case.case_id)
        if identity in seen:
            raise ValueError(f"duplicate static case ID: {case.provider}/{case.case_id}")
        seen.add(identity)
        input_bytes = case.payload.encode("utf-8")
        if len(input_bytes) > MAX_INPUT_BYTES_PER_CASE:
            raise ValueError(f"static input is too large: {case.case_id}")
        total_input += len(input_bytes)
        if total_input > MAX_TOTAL_INPUT_BYTES:
            raise ValueError("static corpus input bytes exceed the configured limit")

        spec = PROVIDERS[case.provider]
        if case.provider not in adapter_cache:
            _verify_extraction(case.provider, spec, contents)
            adapter_cache[case.provider] = _load_adapter(
                case.provider, spec, contents,
            )
            error_maps[case.provider] = _app_error_map(
                contents[spec["app"]],
                spec["app_method"],
                contents[CORE_WIT],
            )
        adapter = adapter_cache[case.provider]
        try:
            raw_result = adapter.invoke(case.operation, case.payload)
        except Exception as error:
            mapped = _map_exception(error, error_maps[case.provider], case)
            expected: dict[str, Any] = {
                "kind": "error",
                "code": mapped["code"],
                "message": mapped["message"],
            }
        else:
            if type(raw_result) is not str:
                raise ValueError(
                    f"adapter returned a non-string result: {case.provider}/{case.case_id}"
                )
            result_bytes = raw_result.encode("utf-8")
            if len(result_bytes) > MAX_OUTPUT_BYTES_PER_CASE:
                raise ValueError(
                    f"source output exceeds 1 MiB: {case.provider}/{case.case_id}"
                )
            total_output += len(result_bytes)
            if total_output > MAX_TOTAL_OUTPUT_BYTES:
                raise ValueError("static corpus output bytes exceed the configured limit")
            output_path = f"cases/{case.provider}/{case.case_id}.output"
            sidecars[output_path] = result_bytes
            expected = {
                "kind": "result",
                "output": {
                    "path": output_path,
                    "encoding": "utf-8",
                    "bytes": len(result_bytes),
                    "sha256": _sha256(result_bytes),
                },
            }

        input_path = f"cases/{case.provider}/{case.case_id}.input"
        sidecars[input_path] = input_bytes
        operation_bytes = case.operation.encode("utf-8")
        records.append(
            {
                "id": case.case_id,
                "provider": case.provider,
                "operation": case.operation,
                "operation_utf8": {
                    "bytes": len(operation_bytes),
                    "sha256": _sha256(operation_bytes),
                },
                "source_manifest_sha256": source_manifest_sha256,
                "input": {
                    "path": input_path,
                    "encoding": "utf-8",
                    "bytes": len(input_bytes),
                    "sha256": _sha256(input_bytes),
                },
                "expected": expected,
            }
        )

    return records, sidecars, total_input, total_output


def _hash_actual_file(path: str, limit: int) -> dict[str, Any]:
    """Hash one executable/module file without recording its private path."""
    candidate = Path(path).resolve(strict=True)
    parent_fd = _open_directory(candidate.parent)
    try:
        data = _read_regular_at(parent_fd, candidate.name, limit)
    finally:
        os.close(parent_fd)
    return {"bytes": len(data), "sha256": _sha256(data)}


def _runtime_provenance() -> dict[str, Any]:
    runtime = _runtime_identity()
    executable = _hash_actual_file(sys.executable, MAX_EXECUTABLE_BYTES)
    module_hash: dict[str, Any] | None = None
    module_path = getattr(unicodedata, "__file__", None)
    if isinstance(module_path, str):
        try:
            module_hash = _hash_actual_file(module_path, MAX_UNICODE_MODULE_BYTES)
        except (OSError, ValueError):
            module_hash = None
    identity = {
        "runtime": runtime,
        "python_executable": executable,
        "unicodedata_module": module_hash,
    }
    canonical = json.dumps(
        identity, ensure_ascii=True, sort_keys=True, separators=(",", ":"),
    ).encode("ascii")
    return {
        **identity,
        "runtime_identity_sha256": _sha256(canonical),
    }


def _unicode_table(
    source_manifest_sha256: str,
    generator_source_sha256: str,
) -> bytes:
    if sys.implementation.name != "cpython":
        raise ValueError("Unicode table requires the actual CPython runtime")
    if unicodedata.unidata_version != "15.1.0":
        raise ValueError("Unicode table generation requires Unicode 15.1.0")

    nonprintable: list[list[int]] = []
    whitespace: list[int] = []
    decimal_zeroes: list[int] = []
    range_start: int | None = None
    range_end: int | None = None

    def close_range() -> None:
        nonlocal range_start, range_end
        if range_start is not None and range_end is not None:
            nonprintable.append([range_start, range_end])
        range_start = None
        range_end = None

    for codepoint in range(0x110000):
        if 0xD800 <= codepoint <= 0xDFFF:
            close_range()
            continue
        character = chr(codepoint)
        if not character.isprintable():
            if range_start is None:
                range_start = codepoint
            range_end = codepoint
        else:
            close_range()
        if character.isspace():
            whitespace.append(codepoint)
        try:
            decimal = unicodedata.decimal(character)
        except ValueError:
            pass
        else:
            if decimal == 0:
                decimal_zeroes.append(codepoint)
    close_range()

    runtime_provenance = _runtime_provenance()
    script_path = Path(__file__).resolve().relative_to(ROOT).as_posix()
    table = {
        "schema": "io.tobkiri.python-unicode-compat.v1",
        "scope": "source-bound CPython Unicode scalar tables; no Rust/Wasm execution",
        "runtime": runtime_provenance["runtime"],
        "unicode_version": unicodedata.unidata_version,
        "scalar_range_inclusive": [0, 0x10FFFF],
        "surrogates_included": False,
        "python_executable": runtime_provenance["python_executable"],
        "unicodedata_module": runtime_provenance["unicodedata_module"],
        "runtime_identity_sha256": runtime_provenance["runtime_identity_sha256"],
        "generator_source": {
            "path": script_path,
            "sha256": generator_source_sha256,
        },
        "source_manifest_sha256": source_manifest_sha256,
        "isprintable_false_ranges_inclusive": nonprintable,
        "isspace_codepoints": whitespace,
        "decimal_zero_codepoints": decimal_zeroes,
    }
    data = (json.dumps(table, ensure_ascii=True, sort_keys=True, indent=2) + "\n").encode(
        "ascii"
    )
    if len(data) > MAX_UNICODE_TABLE_BYTES:
        raise ValueError("Unicode table exceeds its output cap")
    return data


def _builtin_observation(operation: str, value: Any) -> dict[str, Any]:
    """Record one exact builtin result or its native exception class/message."""
    try:
        if operation == "float":
            result = float(value)
            observed: dict[str, Any] = {
                "kind": "result",
                "type": "float",
                "hex": result.hex(),
                "repr": repr(result),
                "str": str(result),
                "is_finite": math.isfinite(result),
            }
        elif operation == "int":
            result = int(value)
            observed = {
                "kind": "result",
                "type": "int",
                "decimal": str(result),
                "repr": repr(result),
            }
        elif operation == "str":
            result = str(value)
            observed = {"kind": "result", "type": "str", "value": result}
        elif operation == "repr":
            result = repr(value)
            observed = {"kind": "result", "type": "str", "value": result}
        elif operation == "truthy":
            result = bool(value)
            observed = {"kind": "result", "type": "bool", "value": result}
        elif operation == "strip":
            result = str(value).strip()
            observed = {"kind": "result", "type": "str", "value": result}
        else:
            raise ValueError("unknown scalar builtin operation")
    except Exception as error:
        return {
            "kind": "error",
            "exception_class": type(error).__name__,
            "message": str(error),
        }
    return observed


def _scalar_vectors(
    static_vectors: tuple[Any, ...],
    contents: dict[str, bytes],
    source_manifest_sha256: str,
) -> bytes:
    """Capture finite original Python builtins on values decoded by source v2."""
    if not static_vectors or len(static_vectors) > MAX_SCALAR_VECTOR_COUNT:
        raise ValueError("the scalar vector set is empty or too large")
    sys.modules.pop("ordered_json", None)
    sys.modules.pop("ordered_json_v2", None)
    _load_source_module("ordered_json", CODEC_V1, contents)
    codec = _load_source_module("ordered_json_v2", CODEC_V2, contents)
    operations = ("float", "int", "str", "repr", "truthy", "strip")
    seen: set[str] = set()
    vectors: list[dict[str, Any]] = []
    total_input = 0
    for vector in static_vectors:
        vector_id = vector.vector_id
        if not CASE_ID.fullmatch(vector_id) or vector_id in seen:
            raise ValueError("scalar vector IDs must be unique lowercase slugs")
        seen.add(vector_id)
        input_bytes = vector.input_json.encode("utf-8")
        total_input += len(input_bytes)
        if total_input > MAX_SCALAR_INPUT_BYTES:
            raise ValueError("scalar vector inputs exceed the configured limit")
        value = codec.loads(vector.input_json)
        vectors.append(
            {
                "id": vector_id,
                "input_json": vector.input_json,
                "input_utf8": {
                    "bytes": len(input_bytes),
                    "sha256": _sha256(input_bytes),
                },
                "decoded_type": type(value).__name__,
                "observations": {
                    operation: _builtin_observation(operation, value)
                    for operation in operations
                },
            }
        )
    runtime_provenance = _runtime_provenance()
    artifact = {
        "schema": "io.tobkiri.python-scalar-compat.v1",
        "scope": (
            "original CPython builtins applied to values decoded by the captured "
            "ordered_json_v2 source; nonfinite builtin results are outside "
            "DATA-codec parity scope"
        ),
        "runtime": runtime_provenance["runtime"],
        "python_executable": runtime_provenance["python_executable"],
        "unicodedata_module": runtime_provenance["unicodedata_module"],
        "runtime_identity_sha256": runtime_provenance["runtime_identity_sha256"],
        "source_manifest_sha256": source_manifest_sha256,
        "value_codec": {
            "path": CODEC_V2,
            "sha256": _sha256(contents[CODEC_V2]),
        },
        "limits": {
            "vector_count": len(vectors),
            "vector_count_limit": MAX_SCALAR_VECTOR_COUNT,
            "input_bytes_total": total_input,
            "input_bytes_limit_total": MAX_SCALAR_INPUT_BYTES,
            "artifact_bytes_limit": MAX_SCALAR_ARTIFACT_BYTES,
        },
        "operations": {
            "float": "float(value), with hex/repr/str/is_finite",
            "int": "int(value), with decimal/repr",
            "str": "str(value)",
            "repr": "repr(value)",
            "truthy": "bool(value)",
            "strip": "str(value).strip()",
        },
        "vectors": vectors,
    }
    data = _json_bytes(artifact)
    if len(data) > MAX_SCALAR_ARTIFACT_BYTES:
        raise ValueError("scalar compatibility artifact exceeds its output cap")
    return data


def _json_bytes(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=True, sort_keys=True, indent=2) + "\n").encode(
        "ascii"
    )


def _open_new_output(path_text: str) -> tuple[int, int, str]:
    path = Path(path_text)
    if not path.is_absolute() or ".." in path.parts:
        raise ValueError("--output-dir must be absolute without parent traversal")
    if path.name in ("", ".", ".."):
        raise ValueError("--output-dir must name a new directory")
    parent_fd = _open_directory(path.parent)
    try:
        os.mkdir(path.name, 0o700, dir_fd=parent_fd)
    except BaseException:
        os.close(parent_fd)
        raise
    output_fd = os.open(
        path.name,
        os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
        dir_fd=parent_fd,
    )
    return parent_fd, output_fd, path.name


def _mkdir_at(parent_fd: int, name: str) -> int:
    os.mkdir(name, 0o700, dir_fd=parent_fd)
    return os.open(
        name,
        os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
        dir_fd=parent_fd,
    )


def _write_at(directory_fd: int, name: str, content: bytes) -> None:
    descriptor = os.open(
        name,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
        0o600,
        dir_fd=directory_fd,
    )
    try:
        view = memoryview(content)
        offset = 0
        while offset < len(view):
            offset += os.write(descriptor, view[offset:])
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _write_artifacts(
    output_dir: str,
    sidecars: dict[str, bytes],
    receipt: bytes,
    unicode_table: bytes | None,
    scalar_vectors: bytes | None,
) -> None:
    parent_fd, output_fd, _leaf = _open_new_output(output_dir)
    try:
        cases_fd = _mkdir_at(output_fd, "cases")
        try:
            provider_fds: dict[str, int] = {}
            try:
                for relative, data in sorted(sidecars.items()):
                    parts = relative.split("/")
                    if len(parts) != 3 or parts[0] != "cases":
                        raise ValueError("invalid sidecar path")
                    provider = parts[1]
                    if provider not in provider_fds:
                        provider_fds[provider] = _mkdir_at(cases_fd, provider)
                    _write_at(provider_fds[provider], parts[2], data)
            finally:
                for descriptor in provider_fds.values():
                    os.close(descriptor)
        finally:
            os.close(cases_fd)
        _write_at(output_fd, "receipt.json", receipt)
        if unicode_table is not None:
            _write_at(output_fd, "unicode-table.json", unicode_table)
        if scalar_vectors is not None:
            _write_at(output_fd, "scalar-vectors.json", scalar_vectors)
    finally:
        os.close(output_fd)
        os.close(parent_fd)


def _validate_cases(static_cases: tuple[Any, ...]) -> None:
    if not static_cases or len(static_cases) > MAX_CASES:
        raise ValueError("the static corpus is empty or too large")
    expected_providers = set(PROVIDERS)
    actual_providers = {case.provider for case in static_cases}
    if actual_providers != expected_providers:
        raise ValueError("the static corpus must cover both original providers")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output-dir", required=True,
        help="new absolute private directory; existing paths are never overwritten",
    )
    parser.add_argument(
        "--unicode-tables", action="store_true",
        help="also capture bounded CPython Unicode 15.1 compatibility tables",
    )
    parser.add_argument(
        "--scalar-vectors", action="store_true",
        help="also capture finite original Python builtin observations",
    )
    args = parser.parse_args(argv)

    sys.dont_write_bytecode = True
    output = Path(args.output_dir)
    if not output.is_absolute() or ".." in output.parts:
        raise ValueError("--output-dir must be absolute without parent traversal")

    source_contents, sources = _sources_before_import()
    case_module = _load_source_module(
        "python_provider_compat_cases",
        "tools/quality/python_provider_compat/cases.py",
        source_contents,
    )
    static_cases = tuple(case_module.CASES)
    _validate_cases(static_cases)
    for provider, spec in PROVIDERS.items():
        _verify_extraction(provider, spec, source_contents)
    runtime = _runtime_identity()
    source_manifest_bytes = json.dumps(
        sources, ensure_ascii=True, sort_keys=True, separators=(",", ":"),
    ).encode("ascii")
    source_manifest_sha256 = _sha256(source_manifest_bytes)
    generator_path = Path(__file__).resolve().relative_to(ROOT).as_posix()
    generator_source_sha256 = next(
        item["sha256"] for item in sources if item["path"] == generator_path
    )

    cases, sidecars, input_bytes, output_bytes = _case_records(
        static_cases, source_contents, source_manifest_sha256,
    )
    unicode_bytes = (
        _unicode_table(source_manifest_sha256, generator_source_sha256)
        if args.unicode_tables else None
    )
    scalar_bytes = (
        _scalar_vectors(
            tuple(case_module.SCALAR_VECTORS),
            source_contents,
            source_manifest_sha256,
        )
        if args.scalar_vectors else None
    )
    receipt: dict[str, Any] = {
        "schema": "io.tobkiri.python-provider-compat.v1",
        "scope": (
            "original-source CPython observations only; no Rust/Wasm execution "
            "or parity claim"
        ),
        "runtime": runtime,
        "source_manifest_sha256": source_manifest_sha256,
        "sources": sources,
        "limits": {
            "static_case_count": len(cases),
            "static_case_limit": MAX_CASES,
            "input_bytes_total": input_bytes,
            "input_bytes_limit_total": MAX_TOTAL_INPUT_BYTES,
            "input_bytes_limit_per_case": MAX_INPUT_BYTES_PER_CASE,
            "output_bytes_total": output_bytes,
            "output_bytes_limit_total": MAX_TOTAL_OUTPUT_BYTES,
            "output_bytes_limit_per_case": MAX_OUTPUT_BYTES_PER_CASE,
            "receipt_bytes_limit": MAX_RECEIPT_BYTES,
        },
        "cases": cases,
    }
    if unicode_bytes is not None:
        receipt["unicode_table"] = {
            "path": "unicode-table.json",
            "encoding": "ascii-json",
            "bytes": len(unicode_bytes),
            "sha256": _sha256(unicode_bytes),
        }
    if scalar_bytes is not None:
        receipt["scalar_vectors"] = {
            "path": "scalar-vectors.json",
            "encoding": "ascii-json",
            "bytes": len(scalar_bytes),
            "sha256": _sha256(scalar_bytes),
        }
    receipt_bytes = _json_bytes(receipt)
    if len(receipt_bytes) > MAX_RECEIPT_BYTES:
        raise ValueError("provider oracle receipt exceeds its output cap")

    _write_artifacts(
        args.output_dir, sidecars, receipt_bytes, unicode_bytes, scalar_bytes,
    )
    print(
        "Python provider source receipt written: "
        f"cases={len(cases)} input-bytes={input_bytes} output-bytes={output_bytes} "
        f"unicode-table={'yes' if unicode_bytes is not None else 'no'} "
        f"scalar-vectors={'yes' if scalar_bytes is not None else 'no'}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
