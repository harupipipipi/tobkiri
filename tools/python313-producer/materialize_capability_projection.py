"""Bind real CI component bytes into two independent ordinary local Packs.

Local is the explicitly unsigned publisher allowed by R2.3. No signature,
approval, Profile, User document or effect grant is synthesized by this tool.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path

from build_native import HERE, fingerprint
import capability_projection_inputs as inputs

ROOT = HERE.parents[1]
PROJECTOR = "local.harness-capability-projection-python"
ENTRY = "local.harness-capability-input"
PROJECTION = PROJECTOR + ".projection@1.0"
REQUESTS = ENTRY + ".requests@1.0"
FLOW = ENTRY + ".projection-flow@1.0"


def read(path: Path, bound: int) -> bytes:
    """Read one physical, bounded member without traversing linked parents."""
    if not path.is_absolute() or path.resolve() != path or not path.is_file():
        raise ValueError("physical materialization input required")
    with path.open("rb") as stream:
        raw = stream.read(bound + 1)
    if len(raw) > bound:
        raise ValueError("materialization input exceeds original bound")
    return raw


def identity(raw: bytes) -> dict:
    return {"bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}


def canonical(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True,
                      separators=(",", ":")).encode()


def permissions(ports: list[str]) -> dict:
    """Concrete ordinary declarations within the original native-sized ceiling."""
    return {
        "network": {"origins": []}, "fs": [], "proc": [], "pty": False,
        "secrets": [], "storage": {"private": False, "public_read": False, "public_write": False},
        "native": False, "cross_profile_storage": False, "audit_read": False,
        "audit_append": False, "host_io": [], "ports": ports, "mediator": False,
        "wasm": {"fuel_per_call": 2_000_000_000, "memory_mib": 64, "wall_ms": 30_000,
                 "table_elements": 65536, "output_bytes": 4 * 1024 * 1024},
        "loops": {"max_iterations": 16, "max_wall_ms": 30_000, "max_cost_units": 100_000},
    }


def port(view: str, *, optional: bool = False) -> dict:
    name, version = view.rsplit("@", 1)
    return {"shape": name, "version": version, "optional": optional}


def write_pack(destination: Path, pack_id: str, files: dict[str, bytes], *,
               shapes: list[tuple[str, str]], provides: list[str], consumes: list[dict],
               allowed: list[str], flows: list[str], toolchain: str) -> dict:
    """Produce a normal independently hash-bound manifest over actual members."""
    manifest = {
        "format": "tobkiri.pack/1", "id": pack_id, "version": "0.1.0", "publisher_key": None,
        "runtime": {"kind": "wasm", "component": "component.wasm", "toolchain": toolchain,
                    "core_modules": {}, "config": None},
        "shapes": [{"name": view.rsplit("@", 1)[0], "version": view.rsplit("@", 1)[1],
                    "kind": kind, "wit": "wit/pack.wit"} for view, kind in shapes],
        "ports": {"provides": [port(view) for view in provides], "consumes": consumes, "untyped": []},
        "inputs": [], "flows": flows, "overlays": [], "stores": [],
        "permissions": permissions(allowed), "ui": None,
        "files": [{"path": name, "sha256": "sha256:" + hashlib.sha256(raw).hexdigest(),
                   "size": len(raw)} for name, raw in sorted(files.items())],
    }
    target = destination / "packs" / pack_id
    target.mkdir(mode=0o700, parents=True)
    for name, raw in files.items():
        if (not name or name.startswith("/") or "\\" in name or ":" in name
                or any(p in ("", ".", "..") for p in name.split("/"))):
            raise ValueError("invalid materialized member")
        p = target / name
        p.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with p.open("xb") as stream:
            stream.write(raw)
    raw = canonical(manifest)
    with (target / "pack.json").open("xb") as stream:
        stream.write(raw)
    return {"id": pack_id, "manifest_sha256": hashlib.sha256(raw).hexdigest(),
            "component": identity(files["component.wasm"])}


def run(supplier: Path, input_component: Path, destination: Path) -> None:
    if os.environ.get("GITHUB_ACTIONS") != "true":
        raise ValueError("real component materialization is restricted to CI")
    if (not destination.is_absolute() or destination.exists()
            or destination.parent.resolve() != destination.parent or not destination.parent.is_dir()):
        raise ValueError("new physical output directory required")
    report = json.loads(read(supplier / "components-build.json", 4 * 1024 * 1024))
    if (report.get("format") != "tobkiri.python313-capability-projection-candidates/1"
            or report.get("outcome") != "success" or report.get("inputs_unchanged") is not True
            or report.get("production_enabled") is not False):
        raise ValueError("successful original two-world component receipt required")
    item = report["components"]["harness-capability-projection"]
    component = read(supplier / "harness-capability-projection/work/component.wasm", 32 * 1024 * 1024)
    proof = read(supplier / "harness-capability-projection/work/component.build.json", 4 * 1024 * 1024)
    if identity(component) != item["artifact"] or identity(proof) != item["build_receipt"]:
        raise ValueError("ordinary Python component or actual build receipt changed")
    decoded_proof = json.loads(proof)
    inputs.verify_component(decoded_proof, item["source"], native=False)
    captured = {row["destination"]: row for row in decoded_proof["source_manifest"]}
    for member, actual in (
        ("wit/pack.wit", ROOT / inputs.PACK / "wit/pack.wit"),
        ("wit/deps/tobkiri-core/core.wit", ROOT / "crates/tobkiri-wit/wit/core/core.wit"),
        ("projection_export.py", ROOT / inputs.PACK / "python/app.py"),
    ):
        if fingerprint(actual) != {k: captured[member][k] for k in ("bytes", "sha256")}:
            raise ValueError("ordinary wrapper or authoritative WIT differs from actual component")
    current = item["source"]["capability_projection"]
    for module in current["original_modules"].values():
        if fingerprint(ROOT / "tobkiri_runtime" / module["path"]) != {
                k: module[k] for k in ("bytes", "sha256")}:
            raise ValueError("original projection source differs from the supplier")
    if hashlib.sha256(read(ROOT / inputs.PACK / "python/projection_adapter.py", 4 * 1024 * 1024)).hexdigest() != current["adapter_sha256"]:
        raise ValueError("original projection adapter changed")
    core_wit = read(ROOT / "crates/tobkiri-wit/wit/core/core.wit", 4 * 1024 * 1024)
    projector_files = {"component.wasm": component, "component.build.json": proof,
                       "wit/pack.wit": read(ROOT / inputs.PACK / "wit/pack.wit", 4 * 1024 * 1024),
                       "wit/deps/core.wit": core_wit,
                       "wit/projection.shape.json": canonical({"cardinality": "one", "streams": {},
                                                               "callbacks": {}, "conformance": None})}
    request_files = {"component.wasm": read(input_component, 32 * 1024 * 1024),
                     "wit/deps/core.wit": core_wit}
    for name in ("wit/pack.wit", "wit/requests.shape.json", "wit/projection-flow.shape.json", "flows/project.flow.json"):
        request_files[name] = read(ROOT / "packs" / ENTRY / name, 4 * 1024 * 1024)
    sources = ["packs/Cargo.toml", "packs/Cargo.lock", *[
        "packs/" + ENTRY + "/" + name for name in ("Cargo.toml", "build.rs", "src/lib.rs", "wit/pack.wit")],
        "crates/tobkiri-wit/wit/core/core.wit"]
    request_files["component.build.json"] = canonical({
        "format": "tobkiri.component-build/1", "language": "rust",
        "artifact_sha256": "sha256:" + identity(request_files["component.wasm"])["sha256"],
        "artifact_bytes": len(request_files["component.wasm"]),
        "sources": {name: "sha256:" + fingerprint(ROOT / name)["sha256"] for name in sources},
        "verification_scope": "source and actual component identity; successful build is a separate CI step",
        "production_enabled": False,
    })
    destination.mkdir(mode=0o700)
    records = [write_pack(destination, PROJECTOR, projector_files,
                          shapes=[(PROJECTION, "port")], provides=[PROJECTION], consumes=[], allowed=[],
                          flows=[], toolchain="original-python313-producer/componentize-py-0.25.0"),
               write_pack(destination, ENTRY, request_files,
                          shapes=[(REQUESTS, "port"), (FLOW, "flow")], provides=[REQUESTS],
                          consumes=[port(PROJECTION), port(FLOW, optional=True)],
                          allowed=[PROJECTION, FLOW], flows=["flows/project.flow.json"],
                          toolchain="rust/1.94.0;wit-bindgen/0.62.0")]
    evidence = {"format": "tobkiri.capability-ordinary-packs/1", "packs": records,
                "independent_unsigned_local_packs": True, "actual_Profile_or_approval_created": False,
                "ordinary_nested_Flow_executed": False, "original_GUI_connected": False,
                "production_enabled": False}
    with (destination / "materialization.json").open("xb") as stream:
        stream.write(canonical(evidence))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("supplier", "input-component", "destination"):
        parser.add_argument("--" + name, type=Path, required=True)
    run(**vars(parser.parse_args()))
