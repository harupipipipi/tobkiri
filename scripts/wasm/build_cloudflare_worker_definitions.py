#!/usr/bin/env python3
"""Build an import-free Cloudflare Workers Python definitions component.

The component is a conformance artifact.  It is deliberately not registered in
the Pack catalog: the active Worker runtime remains the Pack VM until a later
change updates the Pack's executable selection and macOS resource controls.
"""

from __future__ import annotations

import argparse
from pathlib import Path
import sys


RUNTIME_ROOT = Path(__file__).resolve().parents[2] / "tobkiri_runtime"
if str(RUNTIME_ROOT) not in sys.path:
    sys.path.insert(0, str(RUNTIME_ROOT))

from ecosystem.rumi_cloudflare_worker_python_pack.runtime.worker import (  # noqa: E402
    TOOL_ALIASES,
    _definitions,
)
from tobkiri_protocol.canonical import canonical_json  # noqa: E402


VALID_OPERATIONS = ("list", "catalog")
ERROR_MESSAGE = "unknown Workers Python catalog operation"


def _wat_bytes(value: bytes) -> str:
    """Encode bytes for a WAT data segment."""

    return "".join(f"\\{byte:02x}" for byte in value)


def _operation_checks(operation: str) -> str:
    """Return WAT that compares the input operation with one fixed string."""

    return "\n".join(
        f"""local.get $matches
          local.get $operation_pointer
          i32.load8_u offset={index}
          i32.const {byte}
          i32.eq
          i32.and
          local.set $matches"""
        for index, byte in enumerate(operation.encode("utf-8"))
    )


def _operation_branch(operation: str, fallback: str) -> str:
    """Return a WAT branch for one recognized catalog operation."""

    return f"""local.get $operation_length
          i32.const {len(operation.encode("utf-8"))}
          i32.eq
          local.set $matches
          {_operation_checks(operation)}
          local.get $matches
          (if (result i32)
            (then i32.const 0)
            (else {fallback}))"""


def build_component() -> bytes:
    """Build deterministic component bytes from the current Python catalog."""

    from wasmtime import wat2wasm

    content = canonical_json(
        {"aliases": dict(TOOL_ALIASES), "definitions": _definitions()}
    )
    error = ERROR_MESSAGE.encode("utf-8")
    error_pointer = 32 + len(content)
    descriptors = (
        bytes(4)
        + (32).to_bytes(4, "little")
        + len(content).to_bytes(4, "little")
        + (1).to_bytes(4, "little")
        + error_pointer.to_bytes(4, "little")
        + len(error).to_bytes(4, "little")
    )
    allocation_pointer = (error_pointer + len(error) + 31) // 16 * 16
    pages = max(1, (allocation_pointer + 65_535) // 65_536)
    invalid_operation = "i32.const 12"
    catalog_branch = _operation_branch("catalog", invalid_operation)
    list_branch = _operation_branch("list", catalog_branch)
    wat = f"""
    (component
      (core module $guest
        (memory (export "memory") {pages})
        (global $heap (mut i32) (i32.const {allocation_pointer}))
        (data (i32.const 0) "{_wat_bytes(descriptors)}")
        (data (i32.const 32) "{_wat_bytes(content)}")
        (data (i32.const {error_pointer}) "{_wat_bytes(error)}")
        (func (export "realloc")
          (param i32 i32 i32) (param $new_size i32) (result i32)
          (local $pointer i32)
          global.get $heap
          local.tee $pointer
          local.get $new_size
          i32.add
          global.set $heap
          local.get $pointer)
        (func (export "invoke")
          (param $operation_pointer i32) (param $operation_length i32)
          (param i32 i32) (result i32)
          (local $matches i32)
          {list_branch}))
      (core instance $guest (instantiate $guest))
      (func (export "invoke")
        (param "operation-id" string) (param "payload-json" string)
        (result (result string (error string)))
        (canon lift (core func $guest "invoke")
          (memory $guest "memory") (realloc (func $guest "realloc")))))
    """
    return bytes(wat2wasm(wat))


def write_component(output: Path) -> int:
    """Write one checked component to a caller-selected regular WASM file."""

    if output.suffix != ".wasm" or output.is_symlink():
        raise ValueError("output must be a regular .wasm artifact")
    output.parent.mkdir(parents=True, exist_ok=True)
    component = build_component()
    output.write_bytes(component)
    print(f"wrote {output} ({len(component)} bytes)")
    return 0


def main() -> int:
    """Build a conformance component without changing Pack selection."""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    return write_component(args.output)


if __name__ == "__main__":
    raise SystemExit(main())
