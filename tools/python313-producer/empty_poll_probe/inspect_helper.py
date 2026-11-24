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

REQUIRED = {"poll"}


def module_inventory(engine: Engine, path: Path) -> dict:
    """Bind every actual shared import/export to this bounded core-module hash."""
    from wasmtime import FuncType, GlobalType, MemoryType, Module, TableType

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
        signatures = {
            side: {
                name: [
                    {
                        "params": [str(p) for p in item.type.params],
                        "results": [str(r) for r in item.type.results],
                    }
                    for item in entries
                    if item.name == name and isinstance(item.type, FuncType)
                ]
                for name in sorted(REQUIRED)
            }
            for side, entries in (
                ("imports", module.imports),
                ("exports", module.exports),
            )
        }
        memories = [
            item.type for item in module.imports if isinstance(item.type, MemoryType)
        ]
        memory_types = [
            {"is_shared": memory.is_shared, "is_64": memory.is_64}
            for memory in memories
        ]
        return {
            "identity": {"bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()},
            "custom_sections": custom_sections(raw),
            "imports": imports,
            "exports": exports,
            "required_function_signatures": signatures,
            "errno_address_imports": [
                {
                    "module": i.module,
                    "name": i.name,
                    "content": str(i.type.content),
                    "mutable": i.type.mutable,
                }
                for i in module.imports
                if i.name == "errno" and isinstance(i.type, GlobalType)
            ],
            "errno_data_exports": [
                {
                    "name": i.name,
                    "content": str(i.type.content),
                    "mutable": i.type.mutable,
                }
                for i in module.exports
                if i.name == "errno" and isinstance(i.type, GlobalType)
            ],
            "function_imports": [
                i.name for i in module.imports if isinstance(i.type, FuncType)
            ],
            "function_exports": [
                i.name for i in module.exports if isinstance(i.type, FuncType)
            ],
            "imported_memories": len(memories),
            "imported_memory_types": memory_types,
            "imported_tables": sum(
                isinstance(i.type, TableType) for i in module.imports
            ),
            "non_shared_wasm32_imported_memory": bool(memories)
            and all(
                entry["is_shared"] is False and entry["is_64"] is False
                for entry in memory_types
            ),
        }


def checks(helper: dict, original: dict) -> dict[str, bool]:
    """Name each independent guard, including actual imported/exported C signatures."""
    return {
        "required_function_imports": REQUIRED <= set(helper["function_imports"]),
        "no_embedded_poll_or_errno_exports": not bool(
            {"poll", "__errno_location"} & set(helper["function_exports"])
        ),
        "python_initializer": "PyInit__tobkiri_poll_oracle"
        in helper["function_exports"],
        "dynamic_link_metadata": "dylink.0" in helper["custom_sections"],
        "imported_memory": bool(helper["imported_memories"]),
        "non_shared_wasm32_memory": all(
            entry["is_shared"] is False and entry["is_64"] is False
            for entry in helper["imported_memory_types"]
        ),
        "imported_table": bool(helper["imported_tables"]),
        "sdk_libc_function_exports": REQUIRED <= set(original["function_exports"]),
        "errno_got_address_import": len(helper["errno_address_imports"]) == 1
        and helper["errno_address_imports"][0]["module"] == "GOT.mem"
        and helper["errno_address_imports"][0]["content"] == "i32"
        and type(helper["errno_address_imports"][0]["mutable"]) is bool,
        "sdk_errno_data_export": len(original["errno_data_exports"]) == 1
        and original["errno_data_exports"][0]["content"] == "i32"
        and type(original["errno_data_exports"][0]["mutable"]) is bool,
        "no_helper_errno_export": not any(
            e["name"] == "errno" for e in helper["exports"]
        ),
        "function_signatures_match": all(
            len(helper["required_function_signatures"]["imports"][name]) == 1
            and helper["required_function_signatures"]["imports"][name]
            == original["required_function_signatures"]["exports"][name]
            for name in REQUIRED
        ),
    }


def object_symbols(raw: bytes) -> dict:
    """Retain actual LLVM symbols; require one external reference per C symbol."""
    if len(raw) > 4 * 1024 * 1024:
        raise ValueError("object symbol transcript exceeds bound")
    rows = {name: [] for name in ("poll", "errno")}
    for line in raw.decode("utf-8").splitlines():
        fields = line.split()
        if fields and fields[0] in rows:
            rows[fields[0]].append(fields)
    return {
        "symbols": rows,
        "undefined": {
            name: len(items) == 1 and len(items[0]) >= 2 and items[0][1] == "U"
            for name, items in rows.items()
        },
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
        diagnostic = module_inventory(engine, Path("/work/compiler-default.so"))
    selected_checks = checks(helper, original)
    with Path("/work/object-symbols.txt").open("rb") as stream:
        symbols = object_symbols(stream.read(4 * 1024 * 1024 + 1))
    for name, external in symbols["undefined"].items():
        selected_checks[name + "_object_symbol_undefined"] = external
    report = {
        "format": "tobkiri.ui-empty-poll-c-helper-inspection/1",
        "helper": helper["identity"],
        "helper_module": helper,
        "sdk_libc_module": original,
        "shared_imports_verified": all(selected_checks.values()),
        "checks": selected_checks,
        "object_symbol_proof": symbols,
        "errno_address_types_do_not_prove_storage_identity": True,
        "tls_storage_provider_verified": False,
        "compiler_default_diagnostic": {
            "module": diagnostic,
            "checks": checks(diagnostic, original),
            "selected_as_helper": False,
        },
        "all_link_providers_verified": False,
        "component_link_observed": False,
        "guest_execution": False,
        "c_abi_verified": False,
        "production_enabled": False,
    }
    Path("/work/helper-inspection.json").write_text(json.dumps(report, indent=2) + "\n")
    if not all(selected_checks.values()):
        raise ValueError(
            "actual shared-helper guards failed: "
            + ", ".join(name for name, passed in selected_checks.items() if not passed)
        )


if __name__ == "__main__":
    main()
