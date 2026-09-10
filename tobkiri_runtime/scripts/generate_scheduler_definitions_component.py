#!/usr/bin/env python3
"""Generate the import-free Scheduler tool-definition component."""

from __future__ import annotations

import argparse
from pathlib import Path

from ecosystem.rumi_scheduler_tool_adapter_pack.runtime.adapter import _definitions
from tobkiri_protocol.canonical import canonical_json


ROOT = Path(__file__).resolve().parents[1]
OUTPUT = (
    ROOT
    / "ecosystem"
    / "rumi_scheduler_tool_adapter_pack"
    / "runtime"
    / "definitions_component.wasm"
)
OPERATION = "rumi_scheduler_tool_adapter_pack.scheduler-tool-definitions"


def _wat_bytes(value: bytes) -> str:
    return "".join(f"\\{byte:02x}" for byte in value)


def build_component() -> bytes:
    """Build the deterministic component from the canonical Python catalog."""

    from wasmtime import wat2wasm

    content = canonical_json({"aliases": {}, "definitions": _definitions()})
    error = b"unknown scheduler tool catalog operation"
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
    operation_checks = "\n".join(
        f"""local.get $matches
          local.get $operation_pointer
          i32.load8_u offset={index}
          i32.const {byte}
          i32.eq
          i32.and
          local.set $matches"""
        for index, byte in enumerate(OPERATION.encode("utf-8"))
    )
    wat = f"""
    (component
      (core module $guest
        (memory (export "memory") {pages})
        (data (i32.const 0) "{_wat_bytes(descriptors)}")
        (data (i32.const 32) "{_wat_bytes(content)}")
        (data (i32.const {error_pointer}) "{_wat_bytes(error)}")
        (func (export "realloc") (param i32 i32 i32 i32) (result i32)
          i32.const {allocation_pointer})
        (func (export "invoke")
          (param $operation_pointer i32) (param $operation_length i32)
          (param i32 i32) (result i32)
          (local $matches i32)
          local.get $operation_length
          i32.const {len(OPERATION.encode("utf-8"))}
          i32.eq
          local.set $matches
          {operation_checks}
          local.get $matches
          (if (result i32)
            (then i32.const 0)
            (else i32.const 12))))
      (core instance $guest (instantiate $guest))
      (func (export "invoke")
        (param "operation-id" string) (param "payload-json" string)
        (result (result string (error string)))
        (canon lift (core func $guest "invoke")
          (memory $guest "memory") (realloc (func $guest "realloc")))))
    """
    return bytes(wat2wasm(wat))


def main() -> int:
    """Write the component or verify that the checked-in bytes are current."""

    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    generated = build_component()
    if args.check:
        if not OUTPUT.is_file() or OUTPUT.read_bytes() != generated:
            raise SystemExit("Scheduler definitions component is stale")
        print("Scheduler definitions component is current")
        return 0
    OUTPUT.write_bytes(generated)
    print(f"wrote {OUTPUT} ({len(generated)} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
