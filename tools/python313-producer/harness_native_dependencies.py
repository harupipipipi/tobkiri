"""Stage genuine locked dependencies only for the actual native Harness plan.

All input verification is data-only. This tool never imports original runtime,
wheel or extension code, and never runs a guest or changes its budgets.
"""

from __future__ import annotations

import ast
import hashlib
import json
from pathlib import Path
import sys

from build_native import HERE, fingerprint
import build_schema
from fetch_sdk import inventory
import runtime_inputs

ENTRYPOINT = "harness_native_entry"
METADATA = "harness-native-dependencies.json"
MAX_FILE = 8 * 1024 * 1024
MAX_TOTAL = 32 * 1024 * 1024
EXPORTS = [
    "tobkiri-ui:harness-native/runtime@1.0.0",
    "tobkiri-ui:harness-native/transport@1.0.0",
]
IMPORTS = ["tobkiri-native:harness/bindings@1.0.0"]


def selected(
    harness_native: bool,
    schema_inputs: Path | None,
    stdlib: Path | None,
    rpds: Path | None,
) -> bool:
    """Keep optional dependencies out of every frozen or other candidate plan."""
    present = [path is not None for path in (schema_inputs, stdlib, rpds)]
    if any(present) and (not harness_native or not all(present)):
        raise ValueError(
            "schema-inputs/stdlib/rpds require --harness-native and all three"
        )
    return all(present)


def verify(
    schema_inputs: Path,
    producer: Path,
    rpds: Path,
    validation: Path,
    stdlib: Path,
) -> dict:
    """Reuse pinned wheel/archive and genuine positive rpds/producer verification."""
    predecessors = build_schema.verify_inputs(
        schema_inputs,
        producer,
        rpds,
        validation,
        stdlib,
    )
    return {
        "predecessors": predecessors,
        "inputs": runtime_inputs.report(schema_inputs / "schema-inputs.json")["inputs"],
        "stdlib": runtime_inputs.report(stdlib / "stdlib-inputs.json"),
        "extension": fingerprint(rpds / "work/stage/rpds.abi3.so"),
        "rpds_head": build_schema.RPDS_HEAD,
        "ui_clock_variant": False,
    }


def _read(path: Path, expected: dict | None = None) -> bytes:
    if path.is_symlink() or not path.is_file():
        raise ValueError("regular dependency source required")
    with path.open("rb") as stream:
        raw = stream.read(MAX_FILE + 1)
    if len(raw) > MAX_FILE or (
        expected is not None
        and (
            len(raw) != expected["bytes"]
            or hashlib.sha256(raw).hexdigest() != expected["sha256"]
        )
    ):
        raise ValueError("dependency source differs from its verified bytes")
    return raw


def _index(files: dict[str, bytes]) -> tuple[dict, tuple[str, ...]]:
    """Inspect the existing pure DATA index; execute no original module."""
    tree = ast.parse(files["harness_native_source_index.py"])
    if any(not isinstance(node, ast.Assign) for node in tree.body):
        raise ValueError("native source index is not pure DATA")
    values = {
        node.targets[0].id: ast.literal_eval(node.value)
        for node in tree.body
        if len(node.targets) == 1 and isinstance(node.targets[0], ast.Name)
    }
    if set(values) != {"MODULES", "PATH_MODULES", "INDEX_SHA256"}:
        raise ValueError("native source index fields differ")
    canonical = json.dumps(
        {name: values[name] for name in ("MODULES", "PATH_MODULES")},
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    if hashlib.sha256(canonical).hexdigest() != values["INDEX_SHA256"]:
        raise ValueError("native source index checksum differs")
    modules = {}
    for module, path, digest in values["MODULES"]:
        if (
            module in modules
            or path not in files
            or (hashlib.sha256(files[path]).hexdigest() != digest)
        ):
            raise ValueError("original native module differs from its source index")
        modules[module] = path
    namespaces = {"harness_native"}
    for module in modules:
        namespaces.update(
            ".".join(module.split(".")[:i])
            for i in range(1, len(module.split(".")))
            if ".".join(module.split(".")[:i]) not in modules
        )
    return modules, tuple(sorted(namespaces))


def _stdlib_preimports(files: dict[str, bytes], modules: dict) -> tuple[str, ...]:
    """Find only stdlib eager dependencies of the real post-binding entry."""
    from harness_native.source_package import _module_initialization_imports

    tree = ast.parse(files["harness_native/bootstrap.py"])
    (load,) = [
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "_load_bound_runtime"
    ]
    pending = [
        node.module
        for node in load.body
        if isinstance(node, ast.ImportFrom) and not node.level
    ]
    reached, stdlib = set(), set()
    while pending:
        module = pending.pop()
        if module in reached:
            continue
        reached.add(module)
        if module.split(".")[0] in sys.stdlib_module_names:
            if module != "__future__":
                stdlib.add(module)
            continue
        if module not in modules:
            continue
        path = modules[module]
        package = module if path.endswith("/__init__.py") else module.rpartition(".")[0]
        pending.extend(
            target
            for target, _line in _module_initialization_imports(
                ast.parse(files[path]),
                package,
            )
        )
        pending.extend(
            ".".join(module.split(".")[:i])
            for i in range(1, len(module.split(".")))
            if ".".join(module.split(".")[:i]) in modules
        )
    return tuple(sorted(stdlib))


def stage(
    destination: Path,
    original: dict,
    verified: dict,
    schema_inputs: Path,
    stdlib: Path,
    rpds: Path,
) -> dict:
    """Extend only a freshly staged native plan; preserve all original bytes."""
    if inventory(destination) != original["files"]:
        raise ValueError("original native source stage changed")
    raw_plan = _read(destination / "plan.json")
    if hashlib.sha256(raw_plan).hexdigest() != original["plan_sha256"]:
        raise ValueError("original native source plan changed")
    plan = json.loads(raw_plan)
    if (
        plan["world"] != "harness-native"
        or plan["entrypoint"] != "app"
        or plan["outer_exports"] != EXPORTS
        or plan["allowed_imports"] != IMPORTS
    ):
        raise ValueError("dependencies require the unchanged native Harness boundary")
    files = {
        item["destination"]: _read(
            destination / item["source"],
            {
                "bytes": original["files"][item["source"]]["bytes"],
                "sha256": item["sha256"],
            },
        )
        for item in plan["sources"]
    }
    modules, namespaces = _index(files)
    stdlib_preimports = _stdlib_preimports(files, modules)
    original_files = dict(files)
    additions, distributions = {}, {}

    def add(name: str, raw: bytes) -> None:
        # All names come from verified archive inventories or this fixed helper.
        if (
            not name
            or name.startswith("/")
            or "\\" in name
            or ":" in name
            or "\0" in name
            or any(p in ("", ".", "..") for p in name.split("/"))
            or name in files
            or name in additions
            or len(raw) > MAX_FILE
        ):
            raise ValueError("dependency source namespace collision or invalid path")
        additions[name] = raw

    for pin in verified["inputs"]:
        native = pin["role"] == "native-oracle"
        root = (
            schema_inputs
            / "packages"
            / ("rpds-py-native" if native else pin["distribution"])
        )
        for name, entry in pin["files"].items():
            if native and (not name.startswith("rpds/") or name.endswith(".so")):
                continue
            add(name, _read(root / name, entry))
            if not native and name.endswith(".dist-info/METADATA"):
                if pin["distribution"] in distributions:
                    raise ValueError("duplicate dependency distribution metadata")
                distributions[pin["distribution"]] = name.split("/")[0]
    if "rpds/__init__.py" not in additions or len(distributions) != 5:
        raise ValueError("complete locked wheels and original rpds wrapper required")
    for name, entry in verified["stdlib"]["files"].items():
        add(name, _read(stdlib / "source" / name, entry))
    add(
        "rpds/rpds.abi3.so",
        _read(
            rpds / "work/stage/rpds.abi3.so",
            verified["extension"],
        ),
    )
    add(ENTRYPOINT + ".py", _read(HERE / "harness_native/entry_wrapper.py"))
    add(
        "harness_native_captured_loader.py",
        _read(
            HERE / "schema_probe/captured_loader.py",
        ),
    )
    tree_files = {
        name: raw
        for name, raw in {**files, **additions}.items()
        if not name.startswith("wit/") and not name.endswith(".so")
    }
    from schema_probe.captured_loader import Finder

    tree_accounted_bytes = Finder(
        tree_files,
        distributions,
        "/captured-native-harness",
        namespaces,
    ).tree.accounted_bytes
    parts = ["# Captured immutable original source/resource DATA.\nFILES = {\n"]
    literal_bytes = len(parts[0])
    for name, raw in sorted(tree_files.items()):
        literal = repr(name) + ": " + repr(raw) + ",\n"
        literal_bytes += len(literal.encode())
        if literal_bytes > MAX_FILE:
            raise ValueError("native captured DATA literal exceeds original file bound")
        parts.append(literal)
    parts.extend(
        [
            "}\nDISTRIBUTIONS = " + repr(distributions),
            "\nNAMESPACES = " + repr(namespaces),
            "\nSTDLIB_PREIMPORTS = " + repr(stdlib_preimports) + "\n",
        ]
    )
    data = "".join(parts).encode()
    add("harness_native_dependency_data.py", data)
    schema_files = [
        name for name in original_files if name.startswith("tobkiri_protocol/schemas/")
    ]
    proof = {
        "format": "tobkiri.harness-native-dependencies/1",
        "original_plan_sha256": original["plan_sha256"],
        "entrypoint": ENTRYPOINT,
        "original_entrypoint": "app",
        "original_source_files_unchanged": True,
        "original_module_count": len(modules),
        "original_schema_count": len(schema_files),
        "captured_tree_files": len(tree_files),
        "captured_source_bytes": sum(map(len, tree_files.values())),
        "captured_tree_accounted_bytes": tree_accounted_bytes,
        "namespace_packages": list(namespaces),
        "stdlib_preimports": list(stdlib_preimports),
        "dependencies": verified,
        "schema_dependencies_staged": True,
        "provided_external_dependencies": ["jsonschema", "packaging"],
        "external_dependencies_staged": False,
        "unprovided_external_dependencies": ["cryptography"],
        "all_seventeen_methods_compatible": False,
        "dependency_closure_complete": False,
        "component_init_performed": False,
        "native_imports_connected": False,
        "production_enabled": False,
    }
    metadata = (json.dumps(proof, sort_keys=True, indent=2) + "\n").encode()
    add(METADATA, metadata)
    if (
        len(tree_files) > 4096
        or sum(map(len, files.values())) + sum(map(len, additions.values())) > MAX_TOTAL
    ):
        raise ValueError("native dependency source capture exceeds original SDK bounds")
    for name, raw in additions.items():
        source = "python/" + name
        target = destination / source
        target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with target.open("xb") as stream:
            stream.write(raw)
        plan["sources"].append(
            {
                "source": source,
                "destination": name,
                "sha256": hashlib.sha256(raw).hexdigest(),
            }
        )
    plan["entrypoint"] = ENTRYPOINT
    plan_bytes = (json.dumps(plan, sort_keys=True, indent=2) + "\n").encode()
    (destination / "plan.json").write_bytes(plan_bytes)
    return {
        **original,
        "plan_sha256": hashlib.sha256(plan_bytes).hexdigest(),
        "source_files": plan["sources"],
        "files": inventory(destination),
        "harness_native_dependencies": {
            "metadata_path": METADATA,
            "metadata_sha256": hashlib.sha256(metadata).hexdigest(),
            "original_plan_sha256": original["plan_sha256"],
            "entrypoint": ENTRYPOINT,
        },
    }


def verify_component_receipt(proof: dict, source: dict, inputs: Path) -> None:
    """Recheck actual component boundaries and every declared source identity."""
    expected = [
        {
            "source": item["source"],
            "destination": item["destination"],
            "sha256": item["sha256"],
            "bytes": source["files"][item["source"]]["bytes"],
        }
        for item in source["source_files"]
    ]
    manifest_hash = hashlib.sha256(
        json.dumps(
            expected,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode()
    ).hexdigest()
    (metadata,) = [
        item for item in source["source_files"] if item["destination"] == METADATA
    ]
    dependency_evidence = json.loads(
        _read(
            inputs / metadata["source"],
            {
                "bytes": source["files"][metadata["source"]]["bytes"],
                "sha256": metadata["sha256"],
            },
        )
    )
    if (
        proof.get("source_manifest") != expected
        or proof.get("source_manifest_sha256") != manifest_hash
        or proof.get("build_plan_sha256") != source["plan_sha256"]
        or proof.get("world") != "harness-native"
        or proof.get("imports") != sorted(IMPORTS)
        or proof.get("exports") != sorted(EXPORTS)
        or proof.get("inner", {}).get("imports") != sorted(IMPORTS)
        or proof.get("inner", {}).get("exports") != sorted([*EXPORTS, "exports"])
        or proof.get("harness_native_dependencies") != dependency_evidence
    ):
        raise ValueError("native component source manifest or typed boundary differs")
