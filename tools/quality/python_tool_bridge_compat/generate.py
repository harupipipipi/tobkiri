"""Capture bounded observations from the unchanged Tool Bridge v2 sources.

This is a source oracle only. It does not import app_v2/WIT bindings or run
Rust/Wasm. The archived v1 oracle contributes frozen inputs, never expectations.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys
import types
import unicodedata
from typing import Any


ROOT = Path(__file__).resolve().parents[3]
PACK = "packs/local.ai-tool-bridge-ordered-python"
CANONICAL = "tobkiri_runtime/ecosystem/rumi_ai_tool_bridge_pack/runtime/bridge.py"
DISPATCH = "tobkiri_runtime/core_runtime/global_contract_dispatch.py"
FUNCTION = f"{PACK}/python/original_bridge.py"
V1_ADAPTER = f"{PACK}/python/caller_adapter.py"
V2_ADAPTER = f"{PACK}/python/caller_adapter_v2.py"
APP_V2 = f"{PACK}/python/app_v2.py"
PROVENANCE = f"{PACK}/python/provenance.json"
INVENTORY = f"{PACK}/python/inventory.json"
CODEC_V1 = f"{PACK}/ordered_json.py"
CODEC_V2 = f"{PACK}/ordered_json_v2.py"
PACK_V2_WIT = f"{PACK}/wit/pack-v2.wit"
CORE_WIT = "crates/tobkiri-wit/wit/core/core.wit"
ORACLE = f"{PACK}/oracle.json"
ORACLE_FREEZE = f"{PACK}/oracle.freeze.json"
FREEZE_SCRIPT = "scripts/wasm/freeze_ordered_tool_bridge.py"
EXTRACTION_SCRIPT = "scripts/wasm/prepare_ordered_tool_bridge.py"
CASE_MODULE = "tools/quality/python_tool_bridge_compat/cases.py"
PROVIDER = "tool-bridge"
PREFIX = "rumi_ai_tool_bridge_pack.ai-tool-intent-normalize"

MAX_SOURCE_BYTES = 8 * 1024 * 1024
MAX_TOTAL_SOURCE_BYTES = 32 * 1024 * 1024
MAX_CASES = 128
MAX_OPERATION_BYTES = 1024
MAX_INPUT_BYTES_PER_CASE = 4 * 1024 * 1024 + 128
MAX_TOTAL_INPUT_BYTES = 16 * 1024 * 1024
MAX_OUTPUT_BYTES_PER_CASE = 1024 * 1024
MAX_TOTAL_OUTPUT_BYTES = 8 * 1024 * 1024
MAX_RECEIPT_BYTES = 1024 * 1024
MAX_EXECUTABLE_BYTES = 512 * 1024 * 1024
MAX_UNICODE_MODULE_BYTES = 64 * 1024 * 1024
CASE_ID = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
ALLOWED_ERROR_CODES = {"invalid", "internal", "limit-exceeded"}

SOURCE_PATHS = (
    CANONICAL,
    DISPATCH,
    FUNCTION,
    V1_ADAPTER,
    V2_ADAPTER,
    APP_V2,
    PROVENANCE,
    INVENTORY,
    CODEC_V1,
    CODEC_V2,
    PACK_V2_WIT,
    CORE_WIT,
    ORACLE,
    ORACLE_FREEZE,
    FREEZE_SCRIPT,
    EXTRACTION_SCRIPT,
)


def _sha256(data: bytes) -> str:
    """Return the lowercase SHA-256 digest for exact bytes."""
    return hashlib.sha256(data).hexdigest()


def _open_directory(path: Path) -> int:
    """Open an absolute directory without following symlink components."""
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
    """Capture one repository-relative source file under the source byte cap."""
    relative = Path(path)
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError("source path must remain within the repository")
    parent_fd = _open_directory(ROOT / relative.parent)
    try:
        return _read_regular_at(parent_fd, relative.name, MAX_SOURCE_BYTES)
    finally:
        os.close(parent_fd)


def _source_paths() -> list[str]:
    """Return the complete sorted source closure captured before execution."""
    own = Path(__file__).resolve().relative_to(ROOT).as_posix()
    return sorted({own, CASE_MODULE, *SOURCE_PATHS})


def _sources_before_import() -> tuple[dict[str, bytes], list[dict[str, Any]]]:
    """Capture every behavior/input source before compiling any of them."""
    contents = {path: _capture(path) for path in _source_paths()}
    total = sum(len(data) for data in contents.values())
    if total > MAX_TOTAL_SOURCE_BYTES:
        raise ValueError("captured source closure exceeds its aggregate byte limit")
    records = [
        {"path": path, "bytes": len(data), "sha256": _sha256(data)}
        for path, data in sorted(contents.items())
    ]
    return contents, records


def _verify_extraction(contents: dict[str, bytes]) -> dict[str, Any]:
    """Verify original_bridge.py against the captured canonical byte slices."""
    provenance = json.loads(contents[PROVENANCE].decode("utf-8"))
    if not isinstance(provenance, dict):
        raise ValueError("Tool Bridge extraction provenance must be an object")
    if (
        provenance.get("schema") != "io.tobkiri.python-source-extraction.v1"
        or provenance.get("extraction") != "verbatim-complete-source-lines"
        or provenance.get("line_numbers") != "one-based-inclusive"
        or provenance.get("byte_offsets") != "zero-based-half-open"
    ):
        raise ValueError("Tool Bridge extraction provenance format changed")
    if provenance.get("build_performed") is not False:
        raise ValueError("Tool Bridge extraction provenance claims a build")
    if provenance.get("production_enabled") is not False:
        raise ValueError("Tool Bridge extraction provenance claims production use")
    if provenance.get("generator") != EXTRACTION_SCRIPT:
        raise ValueError("Tool Bridge provenance names an unexpected extractor")

    source_records = provenance.get("sources")
    if (
        not isinstance(source_records, list)
        or [record.get("path") for record in source_records
            if isinstance(record, dict)] != [CANONICAL, DISPATCH]
        or len(source_records) != 2
    ):
        raise ValueError("Tool Bridge provenance has no source inventory")
    for record in source_records:
        if not isinstance(record, dict):
            raise ValueError("Tool Bridge provenance source record is malformed")
        path = record.get("path")
        if type(path) is not str or path not in contents:
            raise ValueError("Tool Bridge provenance source was not captured")
        data = contents[path]
        if record.get("bytes") != len(data) or record.get("sha256") != _sha256(data):
            raise ValueError(f"Tool Bridge source disagrees with provenance: {path}")

    inventory_record = provenance.get("inventory")
    if not isinstance(inventory_record, dict):
        raise ValueError("Tool Bridge provenance has no inventory binding")
    inventory_bytes = contents[INVENTORY]
    if (
        inventory_record.get("path") != "inventory.json"
        or inventory_record.get("bytes") != len(inventory_bytes)
        or inventory_record.get("sha256") != _sha256(inventory_bytes)
    ):
        raise ValueError("Tool Bridge inventory disagrees with provenance")

    output_record = provenance.get("output")
    output_bytes = contents[FUNCTION]
    if not isinstance(output_record, dict) or (
        output_record.get("path") != Path(FUNCTION).name
        or output_record.get("bytes") != len(output_bytes)
        or output_record.get("sha256") != _sha256(output_bytes)
    ):
        raise ValueError("original_bridge.py disagrees with extraction provenance")

    slices = provenance.get("slices")
    if not isinstance(slices, list) or not slices:
        raise ValueError("Tool Bridge provenance has no extracted source slices")
    expected_slice_sources = (
        CANONICAL, CANONICAL, CANONICAL, CANONICAL, DISPATCH,
        CANONICAL, CANONICAL, CANONICAL, CANONICAL,
    )
    expected_slice_symbols = (
        "import:__future__", "import:json", "import:re", "import:typing",
        "GlobalContractInvocationError", "_TOOL_NAME",
        "create_tool_intent_operation", "_normalize", "_invalid",
    )
    if len(slices) != len(expected_slice_symbols):
        raise ValueError("Tool Bridge extracted source slice count changed")
    header = (
        b"# Generated by scripts/wasm/prepare_ordered_tool_bridge.py.\n"
        b"# Source slices below are verbatim; see provenance.json.\n\n"
    )
    output_cursor = len(header)
    if not contents[FUNCTION].startswith(header):
        raise ValueError("original_bridge.py extraction header changed")
    for index, item in enumerate(slices):
        if not isinstance(item, dict):
            raise ValueError("Tool Bridge source slice record is malformed")
        source_path = item.get("source_path")
        if (
            type(source_path) is not str
            or source_path not in contents
            or source_path != expected_slice_sources[index]
            or item.get("symbol") != expected_slice_symbols[index]
        ):
            raise ValueError("Tool Bridge source slice refers to uncaptured bytes")
        source_start = item.get("source_start_byte")
        source_end = item.get("source_end_byte")
        output_start = item.get("output_start_byte")
        output_end = item.get("output_end_byte")
        if not all(type(value) is int for value in (
            source_start, source_end, output_start, output_end,
        )):
            raise ValueError("Tool Bridge source slice offsets are malformed")
        if (
            source_start < 0
            or source_end <= source_start
            or source_end > len(contents[source_path])
            or output_start != output_cursor
            or output_end <= output_start
            or output_end > len(output_bytes)
        ):
            raise ValueError("Tool Bridge source slice offsets are out of range")
        source_piece = contents[source_path][source_start:source_end]
        output_piece = output_bytes[output_start:output_end]
        expected_digest = item.get("sha256")
        if (
            not source_piece
            or source_piece != output_piece
            or expected_digest != _sha256(source_piece)
        ):
            raise ValueError("original_bridge.py source slice changed")
        output_cursor = output_end
        if index + 1 < len(slices):
            if output_bytes[output_cursor:output_cursor + 2] != b"\n\n":
                raise ValueError("original_bridge.py contains unbound extraction bytes")
            output_cursor += 2
    if output_cursor != len(output_bytes):
        raise ValueError("original_bridge.py contains unbound extraction bytes")
    return provenance


def _load_source_module(
    name: str, path: str, contents: dict[str, bytes],
) -> types.ModuleType:
    """Execute exactly one captured source file as a top-level module."""
    module = types.ModuleType(name)
    module.__file__ = str(ROOT / path)
    module.__package__ = ""
    sys.modules[name] = module
    code = compile(contents[path], path, "exec", dont_inherit=True)
    exec(code, module.__dict__)
    return module


def _load_adapters(contents: dict[str, bytes]) -> types.ModuleType:
    """Load only captured codecs, original function and v2 transport adapter."""
    for name in (
        "ordered_json", "ordered_json_v2", "original_bridge",
        "caller_adapter", "caller_adapter_v2",
    ):
        sys.modules.pop(name, None)
    _load_source_module("ordered_json", CODEC_V1, contents)
    _load_source_module("ordered_json_v2", CODEC_V2, contents)
    _load_source_module("original_bridge", FUNCTION, contents)
    _load_source_module("caller_adapter", V1_ADAPTER, contents)
    return _load_source_module("caller_adapter_v2", V2_ADAPTER, contents)


def _name(node: ast.AST | None) -> str:
    if isinstance(node, ast.Name):
        return node.id
    raise ValueError("app_v2 error handler has an unsupported exception name")


def _handler_names(node: ast.expr | None) -> tuple[str, ...]:
    if isinstance(node, ast.Name):
        return (node.id,)
    if isinstance(node, ast.Tuple):
        return tuple(_name(item) for item in node.elts)
    raise ValueError("app_v2 error handler is missing an explicit exception type")


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
            raise ValueError("app_v2 typed error must contain code and message")
        code_node, message_node = error_call.args
        if (
            not isinstance(code_node, ast.Attribute)
            or not isinstance(code_node.value, ast.Name)
            or code_node.value.id != "ErrorCode"
            or not isinstance(message_node, ast.Constant)
            or type(message_node.value) is not str
        ):
            raise ValueError("app_v2 typed error has a nonliteral mapping")
        code = code_node.attr.lower().replace("_", "-")
        return code, message_node.value
    raise ValueError("app_v2 handler has no fixed Err(Error(...)) mapping")


def _app_error_map(
    app_bytes: bytes, wit_bytes: bytes, core_wit_bytes: bytes,
) -> dict[str, dict[str, str]]:
    """Extract fixed v2 error mappings without importing the WIT app module."""
    tree = ast.parse(app_bytes, filename=APP_V2)
    methods = [
        node for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == "normalize"
    ]
    if len(methods) != 1:
        raise ValueError("expected exactly one app_v2.normalize method")
    tries = [node for node in ast.walk(methods[0]) if isinstance(node, ast.Try)]
    if len(tries) != 1:
        raise ValueError("expected one closed error boundary in app_v2.normalize")
    pack_wit = wit_bytes.decode("utf-8")
    if not re.search(r"normalize\s*:\s*func\s*\(operation:\s*string", pack_wit):
        raise ValueError("v2 Pack WIT does not declare the expected normalize call")
    if not re.search(r"->\s*result\s*<\s*string\s*,\s*error\s*>", pack_wit):
        raise ValueError("v2 Pack WIT does not return the Core error type")
    core_wit = core_wit_bytes.decode("utf-8")
    match = re.search(r"enum error-code\s*\{([^}]*)\}", core_wit)
    if match is None:
        raise ValueError("Core WIT error-code enum could not be read")
    wit_codes = {part.strip() for part in match.group(1).split(",")}
    mappings: dict[str, dict[str, str]] = {}
    for handler in tries[0].handlers:
        code, message = _error_from_handler(handler)
        if code not in wit_codes or code not in ALLOWED_ERROR_CODES:
            raise ValueError(f"app_v2 maps to a non-closed error code: {code}")
        for exception_name in _handler_names(handler.type):
            if exception_name in mappings:
                raise ValueError(f"duplicate app_v2 handler for {exception_name}")
            mappings[exception_name] = {"code": code, "message": message}
    return mappings


def _map_exception(
    error: Exception,
    error_map: dict[str, dict[str, str]],
    case: Any,
) -> dict[str, str]:
    """Map a source exception only through the closed app_v2 handler table."""
    for cls in type(error).__mro__:
        mapping = error_map.get(cls.__name__)
        if mapping is not None:
            return mapping
    raise ValueError(
        "unmapped source exception in "
        f"{case.provider}/{case.case_id}: {type(error).__name__}"
    ) from None


def _validate_provenance_metadata(
    provenance: dict[str, Any], contents: dict[str, bytes],
) -> None:
    """Require the archived oracle/freeze inputs to name their exact sources."""
    oracle = json.loads(contents[ORACLE].decode("utf-8"))
    binding = json.loads(contents[ORACLE_FREEZE].decode("utf-8"))
    if not isinstance(oracle, dict) or not isinstance(binding, dict):
        raise ValueError("frozen Tool Bridge metadata must be objects")
    oracle_digest = _sha256(contents[ORACLE])
    provenance_digest = _sha256(contents[PROVENANCE])
    freeze_script_digest = _sha256(contents[FREEZE_SCRIPT])
    if oracle.get("source_provenance_sha256") != provenance_digest:
        raise ValueError("frozen v1 oracle provenance binding changed")
    if oracle.get("freeze_script_sha256") != freeze_script_digest:
        raise ValueError("frozen v1 oracle freeze-script binding changed")
    if binding.get("oracle_sha256") != oracle_digest:
        raise ValueError("frozen v1 binding does not match oracle bytes")
    if binding.get("source_provenance_sha256") != provenance_digest:
        raise ValueError("frozen v1 binding does not match source provenance")
    if binding.get("freeze_script_sha256") != freeze_script_digest:
        raise ValueError("frozen v1 binding does not match freeze script")
    if provenance.get("output", {}).get("path") != "original_bridge.py":
        raise ValueError("Tool Bridge provenance does not bind original_bridge.py")


def _runtime_identity() -> dict[str, Any]:
    """Report the real interpreter settings used to produce observations."""
    if sys.implementation.name != "cpython":
        raise ValueError("the Tool Bridge source oracle requires CPython")
    version = sys.version_info
    get_digits = getattr(sys, "get_int_max_str_digits", None)
    if not callable(get_digits):
        raise ValueError("the v2 adapter requires CPython's integer digit-limit API")
    int_limit = get_digits()
    if version[:2] != (3, 13) or unicodedata.unidata_version != "15.1.0":
        raise ValueError("the Tool Bridge source oracle requires CPython 3.13 / Unicode 15.1")
    if int_limit != 4300:
        raise ValueError("the Tool Bridge source oracle requires the unchanged 4300-digit limit")
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


def _hash_actual_file(path: str, limit: int) -> dict[str, Any]:
    """Hash an interpreter file without recording its local path."""
    candidate = Path(path).resolve(strict=True)
    parent_fd = _open_directory(candidate.parent)
    try:
        data = _read_regular_at(parent_fd, candidate.name, limit)
    finally:
        os.close(parent_fd)
    return {"bytes": len(data), "sha256": _sha256(data)}


def _runtime_provenance() -> dict[str, Any]:
    """Bind the reported runtime identity to its executable and Unicode module."""
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
    return {**identity, "runtime_identity_sha256": _sha256(canonical)}


def _case_records(
    static_cases: tuple[Any, ...],
    adapter: types.ModuleType,
    error_map: dict[str, dict[str, str]],
    source_manifest_sha256: str,
) -> tuple[list[dict[str, Any]], dict[str, bytes], int, int]:
    """Run each bounded input through the captured real source adapter."""
    if not static_cases or len(static_cases) > MAX_CASES:
        raise ValueError("static Tool Bridge case count is empty or too large")
    operations = tuple(getattr(adapter, "OPERATIONS", ()))
    expected_operations = (PREFIX, PREFIX + ".generate", PREFIX + ".stream")
    if operations != expected_operations:
        raise ValueError("captured v2 adapter has unexpected operation aliases")

    seen: set[str] = set()
    records: list[dict[str, Any]] = []
    sidecars: dict[str, bytes] = {}
    total_input = 0
    total_output = 0
    for case in static_cases:
        if (
            case.provider != PROVIDER
            or not isinstance(case.case_id, str)
            or CASE_ID.fullmatch(case.case_id) is None
            or case.case_id in seen
        ):
            raise ValueError("static Tool Bridge case has an invalid or duplicate id")
        if type(case.operation) is not str:
            raise ValueError(f"Tool Bridge operation is not text: {case.case_id}")
        try:
            operation_bytes = case.operation.encode("utf-8")
        except UnicodeEncodeError:
            raise ValueError(
                f"Tool Bridge operation is not valid UTF-8: {case.case_id}"
            ) from None
        if len(operation_bytes) > MAX_OPERATION_BYTES:
            raise ValueError(
                f"Tool Bridge operation exceeds its byte limit: {case.case_id}"
            )
        if type(case.payload) is not str:
            raise ValueError(f"Tool Bridge input is not text: {case.case_id}")
        seen.add(case.case_id)

        input_bytes = case.payload.encode("utf-8")
        if len(input_bytes) > MAX_INPUT_BYTES_PER_CASE:
            raise ValueError(
                f"Tool Bridge input exceeds its case limit: {case.case_id}"
            )
        total_input += len(input_bytes)
        if total_input > MAX_TOTAL_INPUT_BYTES:
            raise ValueError(
                "Tool Bridge corpus input bytes exceed the total limit"
            )

        try:
            raw_result = adapter.invoke(case.operation, case.payload)
        except Exception as error:
            mapping = _map_exception(error, error_map, case)
            expected: dict[str, Any] = {
                "kind": "error",
                "code": mapping["code"],
                "message": mapping["message"],
            }
        else:
            if type(raw_result) is not str:
                raise ValueError(
                    f"captured adapter returned non-text: {case.case_id}"
                )
            try:
                result_bytes = raw_result.encode("utf-8")
            except UnicodeEncodeError:
                raise ValueError(
                    f"captured adapter returned non-WIT UTF-8 text: {case.case_id}"
                ) from None
            if len(result_bytes) > MAX_OUTPUT_BYTES_PER_CASE:
                raise ValueError(f"Tool Bridge output exceeds 1 MiB: {case.case_id}")
            total_output += len(result_bytes)
            if total_output > MAX_TOTAL_OUTPUT_BYTES:
                raise ValueError(
                    "Tool Bridge corpus output bytes exceed the total limit"
                )
            output_path = f"cases/{PROVIDER}/{case.case_id}.output"
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

        input_path = f"cases/{PROVIDER}/{case.case_id}.input"
        sidecars[input_path] = input_bytes
        records.append({
            "id": case.case_id,
            "provider": PROVIDER,
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
        })
    return records, sidecars, total_input, total_output


def _json_bytes(value: Any) -> bytes:
    """Serialize an ASCII-only receipt with deterministic object ordering."""
    encoded = json.dumps(value, ensure_ascii=True, sort_keys=True, indent=2) + "\n"
    return encoded.encode("ascii")


def _open_new_output(path_text: str) -> tuple[int, int, str]:
    """Create a new private output directory without following parent links."""
    path = Path(path_text)
    if not path.is_absolute() or ".." in path.parts or path.name in ("", ".", ".."):
        raise ValueError("--output-dir must be absolute without parent traversal")
    parent_fd = _open_directory(path.parent)
    try:
        os.mkdir(path.name, 0o700, dir_fd=parent_fd)
        output_fd = os.open(
            path.name,
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
            dir_fd=parent_fd,
        )
        return parent_fd, output_fd, path.name
    except BaseException:
        os.close(parent_fd)
        raise


def _mkdir_at(parent_fd: int, name: str) -> int:
    """Create and open one new private child directory."""
    os.mkdir(name, 0o700, dir_fd=parent_fd)
    return os.open(
        name,
        os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
        dir_fd=parent_fd,
    )


def _write_at(directory_fd: int, name: str, content: bytes) -> None:
    """Write one new fsynced private regular file without following links."""
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
            written = os.write(descriptor, view[offset:])
            if written <= 0:
                raise OSError("short write while creating oracle artifact")
            offset += written
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _write_artifacts(
    output_dir: str,
    sidecars: dict[str, bytes],
    receipt_bytes: bytes,
) -> None:
    """Persist only into a new 0700 directory, with 0600 files."""
    parent_fd, output_fd, _leaf = _open_new_output(output_dir)
    try:
        cases_fd = _mkdir_at(output_fd, "cases")
        try:
            provider_fd = _mkdir_at(cases_fd, PROVIDER)
            try:
                for relative, data in sorted(sidecars.items()):
                    parts = relative.split("/")
                    if (
                        len(parts) != 3
                        or parts[0] != "cases"
                        or parts[1] != PROVIDER
                    ):
                        raise ValueError("invalid Tool Bridge sidecar path")
                    _write_at(provider_fd, parts[2], data)
            finally:
                os.close(provider_fd)
        finally:
            os.close(cases_fd)
        _write_at(output_fd, "receipt.json", receipt_bytes)
    finally:
        os.close(output_fd)
        os.close(parent_fd)


def main(argv: list[str] | None = None) -> int:
    """Generate fresh bounded observations from the captured original source."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output-dir", required=True,
        help="new absolute private directory; existing paths are never overwritten",
    )
    args = parser.parse_args(argv)
    sys.dont_write_bytecode = True
    output = Path(args.output_dir)
    if not output.is_absolute() or ".." in output.parts:
        raise ValueError("--output-dir must be absolute without parent traversal")

    contents, sources = _sources_before_import()
    provenance = _verify_extraction(contents)
    _validate_provenance_metadata(provenance, contents)

    own_path = Path(__file__).resolve().relative_to(ROOT).as_posix()
    case_module = _load_source_module(
        "python_tool_bridge_compat_cases", CASE_MODULE, contents,
    )
    static_cases = tuple(
        case_module.frozen_input_cases(
            contents[ORACLE],
            contents[ORACLE_FREEZE],
            contents[PROVENANCE],
            contents[FREEZE_SCRIPT],
        )
    ) + tuple(case_module.authored_cases())
    if len(static_cases) > MAX_CASES:
        raise ValueError("Tool Bridge case count exceeds the configured limit")

    adapter = _load_adapters(contents)
    error_map = _app_error_map(
        contents[APP_V2], contents[PACK_V2_WIT], contents[CORE_WIT],
    )
    runtime = _runtime_identity()
    runtime_provenance = _runtime_provenance()
    source_manifest_bytes = json.dumps(
        sources, ensure_ascii=True, sort_keys=True, separators=(",", ":"),
    ).encode("ascii")
    source_manifest_sha256 = _sha256(source_manifest_bytes)
    if own_path not in {item["path"] for item in sources}:
        raise ValueError("generator source was not included in the source manifest")

    cases, sidecars, input_bytes, output_bytes = _case_records(
        static_cases, adapter, error_map, source_manifest_sha256,
    )
    receipt: dict[str, Any] = {
        "schema": "io.tobkiri.python-provider-compat.v1",
        "provider": PROVIDER,
        "protocol": "ordered-finite-json-v2",
        "scope": (
            "independent original-source CPython v2 observations; the frozen v1 "
            "oracle supplies inputs only; no Rust/Wasm execution or parity claim"
        ),
        "runtime": runtime,
        "runtime_provenance": runtime_provenance,
        "source_manifest_sha256": source_manifest_sha256,
        "sources": sources,
        "limits": {
            "static_case_count": len(cases),
            "static_case_limit": MAX_CASES,
            "operation_bytes_limit_per_case": MAX_OPERATION_BYTES,
            "input_bytes_total": input_bytes,
            "input_bytes_limit_total": MAX_TOTAL_INPUT_BYTES,
            "input_bytes_limit_per_case": MAX_INPUT_BYTES_PER_CASE,
            "output_bytes_total": output_bytes,
            "output_bytes_limit_total": MAX_TOTAL_OUTPUT_BYTES,
            "output_bytes_limit_per_case": MAX_OUTPUT_BYTES_PER_CASE,
            "source_bytes_total": sum(item["bytes"] for item in sources),
            "source_bytes_limit_total": MAX_TOTAL_SOURCE_BYTES,
            "source_bytes_limit_per_file": MAX_SOURCE_BYTES,
            "receipt_bytes_limit": MAX_RECEIPT_BYTES,
        },
        "cases": cases,
    }
    receipt_bytes = _json_bytes(receipt)
    if len(receipt_bytes) > MAX_RECEIPT_BYTES:
        raise ValueError("Tool Bridge source oracle receipt exceeds its byte cap")
    _write_artifacts(args.output_dir, sidecars, receipt_bytes)
    print(
        "Tool Bridge v2 source receipt written: "
        f"cases={len(cases)} input-bytes={input_bytes} output-bytes={output_bytes}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
