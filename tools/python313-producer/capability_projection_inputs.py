"""Capture the real App projection and locked dependencies as bounded Pack DATA.

This source tool imports no original runtime/wheel/extension code. Both worlds
receive identical original source bytes; only their typed export wrappers differ.
"""
from __future__ import annotations

import ast
import hashlib
import json
from pathlib import Path
import sys

from build_native import HERE
from fetch_sdk import inventory
from harness_native.source_package import _import_occurrences
from harness_native_dependencies import MAX_FILE, MAX_TOTAL, _read, compress_sources
from schema_probe.captured_loader import Finder

PACK = "packs/local.harness-capability-projection-python"
ROOTS = (
    "ecosystem.defaultspack.defaultspack.http_dynamic_targets",
    "core_runtime.global_contracts.http_contract_dispatch",
)
CORE_IMPORTS = [f"tobkiri:core/{name}@1.0.0" for name in
                ("types", "flow", "clock", "random", "audit")]
NATIVE_EXPORT = "tobkiri-ui:capability-projection/projection@1.0.0"
PACK_EXPORT = "local:harness-capability-projection-python/projection@1.0.0"


def runtime_sources(repo: Path) -> tuple[dict[str, bytes], dict, tuple[str, ...]]:
    """Capture complete genuine eager modules, including real package bodies."""
    root = repo / "tobkiri_runtime"
    pending, modules, files, namespaces = list(ROOTS), {}, {}, set()

    def source(name: str) -> Path | None:
        p = root.joinpath(*name.split("."))
        for candidate in (p.with_suffix(".py"), p / "__init__.py"):
            if candidate.is_file():
                if candidate.resolve() != candidate:
                    raise ValueError("original projection source is not physical")
                return candidate
        return None

    while pending:
        name = pending.pop()
        if name in modules:
            continue
        path = source(name)
        if path is None:
            if (root / name.split(".")[0]).is_dir():
                namespaces.add(name)
            elif name.split(".")[0] not in sys.stdlib_module_names | {"jsonschema", "__future__"}:
                raise ValueError("unprovided projection dependency: " + name)
            continue
        raw = _read(path)
        relative = path.relative_to(root).as_posix()
        modules[name] = {"path": relative, "bytes": len(raw),
                         "sha256": hashlib.sha256(raw).hexdigest()}
        files[relative] = raw
        parts = name.split(".")
        for i in range(1, len(parts)):
            parent = ".".join(parts[:i])
            if source(parent) is not None:
                pending.append(parent)
            else:
                namespaces.add(parent)
        package = name if path.name == "__init__.py" else name.rpartition(".")[0]
        pending.extend(target for target, _line, phase in _import_occurrences(
            ast.parse(raw), package,
        ) if phase in {"module_initialization", "conditional_initialization"})
    return files, modules, tuple(sorted(namespaces - modules.keys()))


def stage(repo: Path, destination: Path, verified: dict, schema_inputs: Path,
          stdlib: Path, rpds: Path, *, native: bool) -> dict:
    """Stage a new closed plan without changing any historical/native plan."""
    if repo.resolve() != repo or destination.exists() or type(native) is not bool:
        raise ValueError("new physical source stage and explicit world required")
    files, modules, namespaces = runtime_sources(repo)
    original = dict(files)
    distributions = {}

    def add(name: str, raw: bytes) -> None:
        if (not name or name.startswith("/") or "\\" in name or ":" in name
                or "\0" in name or any(p in ("", ".", "..") for p in name.split("/"))
                or name in files or len(raw) > MAX_FILE):
            raise ValueError("projection source collision or file bound")
        files[name] = raw

    for pin in verified["inputs"]:
        native_wheel = pin["role"] == "native-oracle"
        root = schema_inputs / "packages" / (
            "rpds-py-native" if native_wheel else pin["distribution"]
        )
        for name, entry in pin["files"].items():
            if native_wheel and (not name.startswith("rpds/") or name.endswith(".so")):
                continue
            add(name, _read(root / name, entry))
            if not native_wheel and name.endswith(".dist-info/METADATA"):
                if pin["distribution"] in distributions:
                    raise ValueError("duplicate projection distribution")
                distributions[pin["distribution"]] = name.split("/")[0]
    if len(distributions) != 5 or "rpds/__init__.py" not in files:
        raise ValueError("complete locked schema closure required")
    for name, entry in verified["stdlib"]["files"].items():
        add(name, _read(stdlib / "source" / name, entry))
    add("projection_adapter.py", _read(repo / PACK / "python/projection_adapter.py"))
    packed = compress_sources(files)
    captured = Finder(packed, distributions, "/captured-capability-projection",
                      namespaces, compressed=True).tree
    for name, raw in files.items():
        if captured.files[name] != raw:
            raise ValueError("projection capture differs from original bytes")
    data = ("FILES = " + repr(packed) + "\nDISTRIBUTIONS = " + repr(distributions)
            + "\nNAMESPACES = " + repr(namespaces) + "\n").encode()
    add("projection_data.py", data)
    add("projection_loader.py", _read(HERE / "schema_probe/captured_loader.py"))
    add("rpds/rpds.abi3.so", _read(rpds / "work/stage/rpds.abi3.so", verified["extension"]))
    add("projection_export.py", _read(
        HERE / "capability_projection/native.py" if native else repo / PACK / "python/app.py"
    ))
    add("app.py", _read(HERE / "capability_projection/entry.py"))
    source_receipt = {
        "format": "tobkiri.capability-projection-source/1",
        "original_modules": modules,
        "schema_dependencies": verified,
        "adapter_sha256": hashlib.sha256(files["projection_adapter.py"]).hexdigest(),
        "source_storage": "same original bytes, per-file zlib/identity, no expansion cache",
        "captured_tree_accounted_bytes": captured.accounted_bytes,
        "import_change": "pure public-input helper no longer imports UI schema reader",
        "ui_schema_validation_retained": True,
        "native_comparison_only": native,
        "authority_or_product_parity_claimed": False,
        "production_enabled": False,
    }
    add("projection-source.json", (json.dumps(source_receipt, sort_keys=True, indent=2) + "\n").encode())
    # Retain complete source members as well as the bounded immutable capture.
    # The SDK audits every source and the source/restore overlap remains funded.
    code = dict(files)
    files = {"python/" + name: raw for name, raw in code.items()}
    files["wit/pack.wit"] = _read(
        HERE / "capability_projection/native.wit" if native else repo / PACK / "wit/pack.wit"
    )
    if not native:
        files["wit/deps/tobkiri-core/core.wit"] = _read(repo / "crates/tobkiri-wit/wit/core/core.wit")
    if sum(map(len, files.values())) > MAX_TOTAL:
        raise ValueError("projection source stage exceeds original 32 MiB bound")
    plan = {
        "format": "tobkiri.python-build/3", "entrypoint": "app", "wit": "wit/pack.wit",
        "world": "capability-projection" if native else "pack", "stub_wasi": True,
        "allowed_imports": [] if native else CORE_IMPORTS,
        "outer_exports": [NATIVE_EXPORT] if native else ["tobkiri:core/lifecycle@1.0.0", PACK_EXPORT],
        "sources": [{"source": name, "destination": name.removeprefix("python/"),
                     "sha256": hashlib.sha256(raw).hexdigest()} for name, raw in sorted(files.items())],
    }
    destination.mkdir(mode=0o700)
    for name, raw in files.items():
        p = destination / name
        p.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with p.open("xb") as stream:
            stream.write(raw)
    plan_raw = (json.dumps(plan, sort_keys=True, indent=2) + "\n").encode()
    (destination / "plan.json").write_bytes(plan_raw)
    provenance = {"format": "tobkiri.capability-projection-stage/1",
                  "original_files": {name: hashlib.sha256(raw).hexdigest() for name, raw in original.items()},
                  "source_receipt": source_receipt, "production_enabled": False}
    (destination / "provenance.json").write_text(json.dumps(provenance, sort_keys=True, indent=2) + "\n")
    return {"plan_sha256": hashlib.sha256(plan_raw).hexdigest(), "source_files": plan["sources"],
            "files": inventory(destination), "capability_projection": source_receipt}


def verify_component(proof: dict, source: dict, *, native: bool) -> None:
    """Require the exact new closed world and every actual source manifest row."""
    expected = [{**item, "bytes": source["files"][item["source"]]["bytes"]}
                for item in source["source_files"]]
    exports = [NATIVE_EXPORT] if native else ["tobkiri:core/lifecycle@1.0.0", PACK_EXPORT]
    imports = [] if native else CORE_IMPORTS
    if (proof.get("source_manifest") != expected
            or proof.get("build_plan_sha256") != source["plan_sha256"]
            or proof.get("world") != ("capability-projection" if native else "pack")
            or proof.get("imports") != sorted(imports)
            or proof.get("exports") != sorted(exports)
            or proof.get("inner", {}).get("imports") != sorted(imports)
            or proof.get("inner", {}).get("exports") != sorted([*exports, "exports"])):
        raise ValueError("actual capability component source or typed boundary differs")
