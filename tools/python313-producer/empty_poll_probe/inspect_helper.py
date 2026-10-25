"""Inspect real compiled C/SDK core modules on CI, without instantiating them."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sys
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from wasmtime import Engine

sys.path.insert(0, "/validation-deps")
sys.path.insert(0, "/support")

from rpds_probe.driver import custom_sections

REQUIRED = {"poll", "__errno_location"}


def module_inventory(engine: Engine, path: Path) -> dict:
    """Bind every actual shared import/export to this bounded core-module hash."""
    from wasmtime import FuncType, MemoryType, Module, TableType

    if path.is_symlink() or not path.is_file():
        raise ValueError("physical regular shared module required")
    with path.open("rb") as stream:
        raw = stream.read(32 * 1024 * 1024 + 1)
    if not raw or len(raw) > 32 * 1024 * 1024:
        raise ValueError("shared module exceeds original Pack bound")
    with Module(engine, raw) as module:
        imports = [
            {"module": item.module, "name": item.name, "kind": type(item.type).__name__}
            for item in module.imports
        ]
        exports = [
            {"name": item.name, "kind": type(item.type).__name__}
            for item in module.exports
        ]
        memories = [
            item.type for item in module.imports if isinstance(item.type, MemoryType)
        ]
        if any(
            memory.is_shared is not False or memory.is_64 is not False
            for memory in memories
        ):
            raise ValueError("oracle requires actual non-shared wasm32 memory")
        return {
            "identity": {"bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()},
            "custom_sections": custom_sections(raw),
            "imports": imports,
            "exports": exports,
            "function_imports": [
                i.name for i in module.imports if isinstance(i.type, FuncType)
            ],
            "function_exports": [
                i.name for i in module.exports if isinstance(i.type, FuncType)
            ],
            "imported_memories": len(memories),
            "imported_tables": sum(
                isinstance(i.type, TableType) for i in module.imports
            ),
            "non_shared_wasm32_imported_memory": bool(memories),
        }


def main() -> None:
    """Reject a helper that embeds poll/errno instead of dynamically importing them."""
    from wasmtime import Config, Engine

    config = Config()
    config.parallel_compilation = False
    with Engine(config) as engine:
        helper = module_inventory(engine, Path("/work/_tobkiri_poll_oracle.abi3.so"))
        original = module_inventory(
            engine, Path("/sdk/share/wasi-sysroot/lib/wasm32-wasip2/libc.so")
        )
    if (
        not REQUIRED <= set(helper["function_imports"])
        or REQUIRED & set(helper["function_exports"])
        or "PyInit__tobkiri_poll_oracle" not in helper["function_exports"]
        or "dylink.0" not in helper["custom_sections"]
        or not helper["imported_memories"]
        or not helper["imported_tables"]
        or not REQUIRED <= set(original["function_exports"])
    ):
        raise ValueError("actual C helper lacks the required shared-module ABI")
    report = {
        "format": "tobkiri.ui-empty-poll-c-helper-inspection/1",
        "helper": helper["identity"],
        "helper_module": helper,
        "sdk_libc_module": original,
        "shared_imports_verified": True,
        "all_link_providers_verified": False,
        "component_link_observed": False,
        "guest_execution": False,
        "c_abi_verified": False,
        "production_enabled": False,
    }
    Path("/work/helper-inspection.json").write_text(json.dumps(report, indent=2) + "\n")


if __name__ == "__main__":
    main()
