"""Observe original Gateway _normalize_result usage coercion under CPython."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys
import types
from typing import Any


ROOT = Path(__file__).resolve().parents[3]
PACK = "packs/local.ai-gateway-finalizer-python"
PYTHON_DIR = f"{PACK}/python"
SOURCE_CASES = f"{PACK}/source-cases.json"
FINALIZER_SOURCE = f"{PYTHON_DIR}/original_gateway_finalizer.py"
CASE_MODULE = "tools/quality/python_finalizer_normalize_compat/cases.py"
GENERATOR_MODULE = "tools/quality/python_finalizer_normalize_compat/generate.py"
README = "tools/quality/python_finalizer_normalize_compat/README.md"

MAX_SOURCE_BYTES = 16 * 1024 * 1024
MAX_TOTAL_SOURCE_BYTES = 32 * 1024 * 1024
MAX_CASES = 32
MAX_INPUT_BYTES_PER_CASE = 256 * 1024
MAX_TOTAL_INPUT_BYTES = 2 * 1024 * 1024
MAX_OUTPUT_BYTES_PER_CASE = 512 * 1024
MAX_TOTAL_OUTPUT_BYTES = 4 * 1024 * 1024
MAX_RECEIPT_BYTES = 1024 * 1024
CASE_ID = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
SOURCE_HELPER_PATHS = (
    "scripts/wasm/observe_gateway_finalizer.py",
    "scripts/wasm/prepare_gateway_finalizer.py",
    "scripts/wasm/stage_gateway_finalizer.py",
    "scripts/wasm/prepare_gateway_finalization_providers.py",
    "scripts/wasm/stage_gateway_finalization_providers.py",
    "scripts/wasm/prepare_gateway_tool_caller.py",
    "scripts/wasm/stage_gateway_tool_caller.py",
    "scripts/wasm/prepare_ordered_tool_bridge.py",
    "scripts/wasm/stage_ordered_tool_bridge.py",
    "scripts/wasm/stage_ordered_tool_bridge_v2.py",
)

WASM_TOOLS = ROOT / "scripts/wasm"
sys.path.insert(0, str(WASM_TOOLS))
import observe_gateway_finalizer as source_observer  # noqa: E402
import prepare_gateway_finalizer as source_preparation  # noqa: E402


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=True, sort_keys=True, separators=(",", ":"),
        allow_nan=False,
    ).encode("ascii")


def _json_bytes(value: Any) -> bytes:
    return (json.dumps(
        value, ensure_ascii=True, sort_keys=True, indent=2, allow_nan=False,
    ) + "\n").encode("ascii")


def _open_directory(path: Path) -> int:
    """Open an absolute directory by components without following links."""
    if not path.is_absolute() or ".." in path.parts:
        raise ValueError("directory path must be absolute without parent traversal")
    descriptor = os.open(path.anchor, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in path.parts[1:]:
            if part in ("", "."):
                continue
            child = os.open(
                part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                dir_fd=descriptor,
            )
            os.close(descriptor)
            descriptor = child
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _read_regular_at(directory_fd: int, name: str, limit: int) -> bytes:
    """Read one bounded stable regular file without following a symlink."""
    descriptor = os.open(
        name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory_fd,
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


def _capture(relative: str) -> bytes:
    """Capture one repository source through no-follow directory descriptors."""
    path = Path(relative)
    if path.is_absolute() or ".." in path.parts or not path.parts:
        raise ValueError("source path must remain within the repository")
    parent_fd = _open_directory(ROOT / path.parent)
    try:
        return _read_regular_at(parent_fd, path.name, MAX_SOURCE_BYTES)
    finally:
        os.close(parent_fd)


def _strict_json(content: bytes) -> Any:
    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in items:
            if key in result:
                raise ValueError("duplicate source-case JSON key")
            result[key] = value
        return result

    def reject_constant(value: str) -> None:
        raise ValueError(f"non-finite source-case constant: {value}")

    return json.loads(
        content.decode("utf-8"), object_pairs_hook=pairs,
        parse_constant=reject_constant,
    )


def _runtime() -> dict[str, Any]:
    observed = source_observer.runtime()
    version = sys.version_info
    if (
        observed.get("implementation") != "CPython"
        or (version.major, version.minor) != (3, 13)
        or observed.get("unicode") != "15.1.0"
        or observed.get("int_max_str_digits") != 4300
    ):
        raise ValueError(
            "source observation requires CPython 3.13.x, Unicode 15.1.0, "
            "and int_max_str_digits=4300"
        )
    return {
        **observed,
        "version_info": {
            "major": version.major, "minor": version.minor,
            "micro": version.micro, "releaselevel": version.releaselevel,
            "serial": version.serial,
        },
        "recursion_limit": sys.getrecursionlimit(),
    }


def _source_records(
    own_sources: dict[str, bytes], provenance: dict[str, Any],
) -> list[dict[str, Any]]:
    """Bind the fresh source inputs, loader, and verified extraction evidence."""
    records: dict[str, dict[str, Any]] = {}

    def add(path: str, content: bytes, expected: dict[str, Any] | None = None) -> None:
        record = {"path": path, "bytes": len(content), "sha256": _sha256(content)}
        if expected is not None and record != expected:
            raise ValueError(f"verified source changed during capture: {path}")
        previous = records.get(path)
        if previous is not None and previous != record:
            raise ValueError(f"source has conflicting bindings: {path}")
        records[path] = record

    for field in ("sources", "transport_dependencies"):
        entries = provenance.get(field)
        if type(entries) is not list:
            raise ValueError("verified extraction provenance omits source records")
        for item in entries:
            if (
                type(item) is not dict
                or type(item.get("path")) is not str
                or type(item.get("bytes")) is not int
                or type(item.get("sha256")) is not str
            ):
                raise ValueError("verified extraction source record is malformed")
            path = item["path"]
            add(path, _capture(path), item)

    inventory_path = f"{PYTHON_DIR}/inventory.json"
    inventory_bytes = _capture(inventory_path)
    inventory_binding = provenance.get("inventory")
    if (
        type(inventory_binding) is not dict
        or inventory_binding.get("path") != "inventory.json"
        or inventory_binding.get("bytes") != len(inventory_bytes)
        or inventory_binding.get("sha256") != _sha256(inventory_bytes)
    ):
        raise ValueError("verified extraction inventory changed")
    inventory = _strict_json(inventory_bytes)
    inventory_sources = inventory.get("sources") if type(inventory) is dict else None
    if type(inventory_sources) is not list:
        raise ValueError("verified extraction inventory omits source records")
    for item in inventory_sources:
        if (
            type(item) is not dict
            or type(item.get("path")) is not str
            or type(item.get("bytes")) is not int
            or type(item.get("sha256")) is not str
        ):
            raise ValueError("verified extraction inventory record is malformed")
        path = item["path"]
        add(path, _capture(path), item)

    paths = set(source_observer.SOURCE_MODULES.values())
    paths.update(SOURCE_HELPER_PATHS)
    paths.update({
        SOURCE_CASES,
        inventory_path,
        f"{PYTHON_DIR}/provenance.json",
        CASE_MODULE,
        GENERATOR_MODULE,
        README,
    })
    for path in sorted(paths):
        add(path, own_sources[path] if path in own_sources else _capture(path))
    required = {
        SOURCE_CASES,
        FINALIZER_SOURCE,
        inventory_path,
        f"{PYTHON_DIR}/provenance.json",
        "packs/local.ai-tool-bridge-ordered-python/ordered_json_v2.py",
        CASE_MODULE,
        GENERATOR_MODULE,
        README,
    }
    required.update(SOURCE_HELPER_PATHS)
    if not required <= records.keys():
        raise ValueError("source manifest omits finalizer observation inputs")
    result = [records[path] for path in sorted(records)]
    if len(result) > 128 or sum(item["bytes"] for item in result) > MAX_TOTAL_SOURCE_BYTES:
        raise ValueError("finalizer source manifest exceeds its byte limit")
    if any(item["bytes"] > MAX_SOURCE_BYTES for item in result):
        raise ValueError("a captured finalizer source exceeds its byte limit")
    return result


def _load_case_module(content: bytes) -> types.ModuleType:
    module = types.ModuleType("_python_finalizer_normalize_cases")
    module.__file__ = str(ROOT / CASE_MODULE)
    module.__package__ = ""
    previous = sys.modules.get(module.__name__)
    sys.modules[module.__name__] = module
    try:
        exec(compile(content, CASE_MODULE, "exec", dont_inherit=True), module.__dict__)
    except BaseException:
        if previous is None:
            sys.modules.pop(module.__name__, None)
        else:
            sys.modules[module.__name__] = previous
        raise
    if previous is None:
        sys.modules.pop(module.__name__, None)
    else:
        sys.modules[module.__name__] = previous
    return module


def _first_generate_fixture(catalog_bytes: bytes) -> dict[str, Any]:
    catalog = _strict_json(catalog_bytes)
    if (
        type(catalog) is not dict
        or catalog.get("schema") != "io.tobkiri.gateway-finalizer-source-cases.v1"
        or type(catalog.get("cases")) is not list
        or len(catalog["cases"]) != 10
    ):
        raise ValueError("unexpected reviewed finalizer source-case catalog")
    first = catalog["cases"][0]
    if (
        type(first) is not dict
        or first.get("id") != "generate-tools-usage"
        or first.get("operation") != "generate"
        or type(first.get("payload")) is not dict
    ):
        raise ValueError("the first generate source fixture changed")
    payload = first["payload"]
    if (
        type(payload.get("value")) is not dict
        or type(payload.get("candidate")) is not dict
        or type(payload.get("request_id")) is not str
    ):
        raise ValueError("the first generate fixture lacks normalizer inputs")
    return payload


def _tag(value: Any) -> Any:
    """Encode exact Python value types and ordered mapping entries as JSON data."""
    kind = type(value)
    if value is None:
        return {"type": "none"}
    if kind is bool:
        return {"type": "bool", "value": value}
    if kind is int:
        return {"type": "int", "decimal": str(value)}
    if kind is float:
        return {"type": "float", "repr": repr(value), "hex": value.hex()}
    if kind is str:
        return {"type": "str", "value": value}
    if kind is list:
        return {"type": "list", "items": [_tag(item) for item in value]}
    if kind is tuple:
        return {"type": "tuple", "items": [_tag(item) for item in value]}
    if kind is dict:
        return {
            "type": "dict",
            "items": [[_tag(key), _tag(item)] for key, item in value.items()],
        }
    raise TypeError(f"unsupported original result type: {kind.__name__}")


def _case_payload(base: dict[str, Any], case: Any) -> bytes:
    payload = dict(base)
    value = dict(base["value"])
    if case.usage_present:
        value["usage"] = case.usage
    else:
        value.pop("usage", None)
    payload["value"] = value
    return json.dumps(
        payload, ensure_ascii=True, separators=(",", ":"), allow_nan=False,
    ).encode("ascii")


def _descriptor(path: str, content: bytes) -> dict[str, Any]:
    return {
        "path": path, "encoding": "utf-8", "bytes": len(content),
        "sha256": _sha256(content),
    }


def _observe_cases(
    base_payload: dict[str, Any], case_specs: tuple[Any, ...],
    modules: dict[str, types.ModuleType], source_manifest_sha256: str,
) -> tuple[list[dict[str, Any]], dict[str, bytes], int, int]:
    if not case_specs or len(case_specs) > MAX_CASES:
        raise ValueError("usage probe case count is empty or too large")
    codec = modules["ordered_json_v2"]
    finalizer = modules["finalizer"]
    seen: set[str] = set()
    records: list[dict[str, Any]] = []
    sidecars: dict[str, bytes] = {}
    input_total = 0
    output_total = 0

    for case in case_specs:
        case_id = case.case_id
        if (
            type(case_id) is not str or CASE_ID.fullmatch(case_id) is None
            or case_id in seen or type(case.usage_present) is not bool
        ):
            raise ValueError("usage probe case id is invalid or duplicated")
        seen.add(case_id)
        input_bytes = _case_payload(base_payload, case)
        if len(input_bytes) > MAX_INPUT_BYTES_PER_CASE:
            raise ValueError(f"usage probe input is too large: {case_id}")
        input_total += len(input_bytes)
        if input_total > MAX_TOTAL_INPUT_BYTES:
            raise ValueError("usage probe inputs exceed the total byte limit")
        input_path = f"cases/{case_id}.input.json"
        sidecars[input_path] = input_bytes

        decoded = codec.loads(input_bytes.decode("utf-8"))
        candidate_data = decoded["candidate"]
        candidate = finalizer.Candidate(**{
            **candidate_data,
            "capabilities": frozenset(candidate_data["capabilities"]),
            "modalities": frozenset(candidate_data["modalities"]),
        })
        try:
            result = finalizer._normalize_result(
                decoded["value"], decoded["request_id"], candidate,
            )
        except Exception as error:
            normalizer_kind = "exception"
            tagged_result = {
                "kind": "exception", "exception_type": type(error).__name__,
                "message": str(error),
            }
            ordered_result: dict[str, Any] = {"kind": "not-run"}
        else:
            normalizer_kind = "result"
            tagged_result = {"kind": "result", "value": _tag(result)}
            try:
                encoded = codec.dumps(result)
            except Exception as error:
                error_bytes = _json_bytes({
                    "kind": "error", "exception_type": type(error).__name__,
                    "message": str(error),
                })
                if len(error_bytes) > MAX_OUTPUT_BYTES_PER_CASE:
                    raise ValueError(
                        f"ordered-json-v2 error observation is too large: {case_id}"
                    )
                output_total += len(error_bytes)
                if output_total > MAX_TOTAL_OUTPUT_BYTES:
                    raise ValueError(
                        "finalizer observations exceed the total output byte limit"
                    )
                error_path = f"cases/{case_id}.ordered-json-v2-error.json"
                sidecars[error_path] = error_bytes
                ordered_result = {
                    "kind": "error", "output": _descriptor(error_path, error_bytes),
                }
            else:
                if type(encoded) is not str:
                    raise ValueError("ordered-json-v2 returned non-text output")
                encoded_bytes = encoded.encode("utf-8")
                if len(encoded_bytes) > MAX_OUTPUT_BYTES_PER_CASE:
                    raise ValueError(f"ordered-json-v2 output is too large: {case_id}")
                output_total += len(encoded_bytes)
                if output_total > MAX_TOTAL_OUTPUT_BYTES:
                    raise ValueError("ordered-json-v2 outputs exceed the total byte limit")
                encoded_path = f"cases/{case_id}.ordered-json-v2.json"
                sidecars[encoded_path] = encoded_bytes
                ordered_result = {
                    "kind": "result", "output": _descriptor(encoded_path, encoded_bytes),
                }

        tagged_bytes = _json_bytes(tagged_result)
        if len(tagged_bytes) > MAX_OUTPUT_BYTES_PER_CASE:
            raise ValueError(f"tagged Python result is too large: {case_id}")
        output_total += len(tagged_bytes)
        if output_total > MAX_TOTAL_OUTPUT_BYTES:
            raise ValueError("finalizer observations exceed the total output byte limit")
        output_path = f"cases/{case_id}.python-value-tree.json"
        sidecars[output_path] = tagged_bytes
        records.append({
            "id": case_id,
            "source_manifest_sha256": source_manifest_sha256,
            "input": _descriptor(input_path, input_bytes),
            "normalizer": {
                "kind": normalizer_kind,
                "output": _descriptor(output_path, tagged_bytes),
            },
            "ordered_json_v2": ordered_result,
        })
    return records, sidecars, input_total, output_total


def _create_output(output_text: str, sidecars: dict[str, bytes], receipt: bytes) -> None:
    path = Path(output_text)
    if not path.is_absolute() or ".." in path.parts or path.name in ("", ".", ".."):
        raise ValueError("--output-dir must be absolute without parent traversal")
    parent_fd = _open_directory(path.parent)
    try:
        os.mkdir(path.name, 0o700, dir_fd=parent_fd)
        output_fd = os.open(
            path.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
            dir_fd=parent_fd,
        )
    finally:
        os.close(parent_fd)
    try:
        os.mkdir("cases", 0o700, dir_fd=output_fd)
        cases_fd = os.open(
            "cases", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
            dir_fd=output_fd,
        )
        try:
            for relative, content in sorted(sidecars.items()):
                if not relative.startswith("cases/") or "/" in relative[6:]:
                    raise ValueError("invalid usage oracle sidecar path")
                descriptor = os.open(
                    relative[6:],
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                    0o600, dir_fd=cases_fd,
                )
                try:
                    remaining = memoryview(content)
                    while remaining:
                        written = os.write(descriptor, remaining)
                        if written <= 0:
                            raise OSError("short write creating usage oracle sidecar")
                        remaining = remaining[written:]
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
        finally:
            os.close(cases_fd)

        descriptor = os.open(
            "receipt.json",
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600, dir_fd=output_fd,
        )
        try:
            remaining = memoryview(receipt)
            while remaining:
                written = os.write(descriptor, remaining)
                if written <= 0:
                    raise OSError("short write creating usage oracle receipt")
                remaining = remaining[written:]
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    finally:
        os.close(output_fd)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output-dir", required=True,
        help="new absolute private output directory; existing paths are rejected",
    )
    args = parser.parse_args(argv)

    runtime = _runtime()
    own_paths = (GENERATOR_MODULE, CASE_MODULE, README)
    own_sources = {path: _capture(path) for path in own_paths}
    provenance = source_preparation.verify(ROOT / PYTHON_DIR)
    extracted = _capture(FINALIZER_SOURCE)
    if provenance.get("output", {}).get("sha256") != _sha256(extracted):
        raise ValueError("verified finalizer extraction hash changed")
    sources = _source_records(own_sources, provenance)
    source_manifest_sha256 = _sha256(_canonical_bytes(sources))
    catalog_bytes = _capture(SOURCE_CASES)
    catalog_record = next(item for item in sources if item["path"] == SOURCE_CASES)
    if (
        catalog_record["bytes"] != len(catalog_bytes)
        or catalog_record["sha256"] != _sha256(catalog_bytes)
    ):
        raise ValueError("source-case fixture changed during source capture")
    case_module = _load_case_module(own_sources[CASE_MODULE])
    base_payload = _first_generate_fixture(catalog_bytes)
    case_specs = tuple(case_module.usage_cases())

    with source_observer.source_modules() as modules:
        cases, sidecars, input_total, output_total = _observe_cases(
            base_payload, case_specs, modules, source_manifest_sha256,
        )

    after_own = {path: _capture(path) for path in own_paths}
    if after_own != own_sources or _source_records(after_own, provenance) != sources:
        raise ValueError("a local source changed while recording observations")
    after_extraction = source_preparation.verify(ROOT / PYTHON_DIR)
    if after_extraction != provenance:
        raise ValueError("verified finalizer extraction changed during observation")

    receipt: dict[str, Any] = {
        "schema": "io.tobkiri.python-finalizer-normalize-compat.v1",
        "provider": "gateway-finalizer-normalize",
        "protocol": "ordered-finite-json-v2",
        "scope": (
            "direct calls to unchanged original_gateway_finalizer._normalize_result; "
            "Candidate comes from the first generate source fixture and only value.usage "
            "is varied; no dependency, Host, Core, or Wasm call is made"
        ),
        "oracle_provenance": {
            "kind": "fresh-independent-source-observation",
            "historical_frozen_expected_outputs_reused": False,
            "historical_observer_expected_runtime_used": False,
        },
        "runtime": runtime,
        "source_manifest_sha256": source_manifest_sha256,
        "sources": sources,
        "prepared_extraction": {
            "verifier": "scripts/wasm/prepare_gateway_finalizer.py::verify",
            "provenance_sha256": _sha256(_canonical_bytes(provenance)),
            "provenance": provenance,
        },
        "fixture": {
            "source_path": SOURCE_CASES,
            "source_sha256": _sha256(catalog_bytes),
            "case_id": "generate-tools-usage",
            "operation": "generate",
        },
        "limits": {
            "case_count": len(cases),
            "case_count_limit": MAX_CASES,
            "input_bytes_total": input_total,
            "input_bytes_limit_total": MAX_TOTAL_INPUT_BYTES,
            "input_bytes_limit_per_case": MAX_INPUT_BYTES_PER_CASE,
            "output_bytes_total": output_total,
            "output_bytes_limit_total": MAX_TOTAL_OUTPUT_BYTES,
            "output_bytes_limit_per_case": MAX_OUTPUT_BYTES_PER_CASE,
            "source_bytes_total": sum(item["bytes"] for item in sources),
            "source_bytes_limit_total": MAX_TOTAL_SOURCE_BYTES,
            "source_bytes_limit_per_file": MAX_SOURCE_BYTES,
            "receipt_bytes_limit": MAX_RECEIPT_BYTES,
        },
        "execution_scope": {
            "dependency_calls_made": False,
            "core_parity_claimed": False,
            "wasm_parity_claimed": False,
            "host_behavior_claimed": False,
        },
        "cases": cases,
    }
    receipt_bytes = _json_bytes(receipt)
    if len(receipt_bytes) > MAX_RECEIPT_BYTES:
        raise ValueError("finalizer normalize receipt exceeds its byte limit")
    _create_output(args.output_dir, sidecars, receipt_bytes)
    print(
        "Finalizer normalize source receipt written: "
        f"cases={len(cases)} input-bytes={input_total} output-bytes={output_total}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
