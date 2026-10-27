"""Bind the self-contained catalog component and a separate ordinary Flow entry.

The fixed defaults are inside the actual component. No runtime config, sidecar,
Profile, approval, signature or source/IO authority is created by this tool.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from build_native import HERE, fingerprint
import bundle_catalog_inputs as inputs
from materialize_capability_projection import (
    canonical,
    identity,
    port,
    read,
    write_pack,
)

ROOT = HERE.parents[1]
CATALOG_PACK = "local.harness-bundle-catalog-python"
ENTRY = "local.harness-catalog-input"
CATALOG = CATALOG_PACK + ".catalog@1.0"
REQUESTS = ENTRY + ".requests@1.0"
FLOW = ENTRY + ".catalog-flow@1.0"


def run(supplier: Path, input_component: Path, destination: Path) -> None:
    """Use actual receipt-bound components without an implicit capability grant."""
    if os.environ.get("GITHUB_ACTIONS") != "true":
        raise ValueError("real bundle materialization is restricted to CI")
    if (
        not destination.is_absolute()
        or destination.exists()
        or destination.parent.resolve() != destination.parent
        or not destination.parent.is_dir()
    ):
        raise ValueError("new physical output directory required")
    report = json.loads(read(supplier / "components-build.json", 4 * 1024 * 1024))
    if (
        report.get("format") != "tobkiri.python313-bundle-catalog-candidates/1"
        or report.get("outcome") != "success"
        or report.get("inputs_unchanged") is not True
        or report.get("production_enabled") is not False
    ):
        raise ValueError("actual successful two-world supplier receipt required")
    cases_raw = read(supplier / "cases.json", 4 * 1024 * 1024)
    cases = json.loads(cases_raw)
    if (
        cases.get("format") != "tobkiri.bundle-catalog-comparison/1"
        or cases.get("oracle_commit") != "54e7da008997a3eb45876cbe18459dba9902cc9d"
        or cases.get("entries") != 305
        or cases.get("read_occurrences") != 453
    ):
        raise ValueError("complete pinned original bundle comparison required")
    item = report["components"]["harness-bundle-catalog"]
    component = read(
        supplier / "harness-bundle-catalog/work/component.wasm", 32 * 1024 * 1024
    )
    proof_raw = read(
        supplier / "harness-bundle-catalog/work/component.build.json", 4 * 1024 * 1024
    )
    if (
        identity(component) != item["artifact"]
        or identity(proof_raw) != item["build_receipt"]
    ):
        raise ValueError("actual component or its build receipt changed")
    proof = json.loads(proof_raw)
    inputs.verify_component(proof, item["source"], native=False)
    source = item["source"]["bundle_catalog"]
    if (
        source["original_bundle_cases"] != identity(cases_raw)
        or source["embedded_carrier"] != identity(cases["payload"].encode())
        or source["runtime_config_override_used"] is not False
        or source["original_resources"] != cases["schema_resources"]
    ):
        raise ValueError("embedded defaults differ from the independent original")
    captured = {row["destination"]: row for row in proof["source_manifest"]}
    if {
        k: captured["bundled-defaults/embedded.json"][k] for k in ("bytes", "sha256")
    } != source["embedded_resource"]:
        raise ValueError(
            "actual compiled source does not contain the complete defaults"
        )
    for member, actual in (
        ("wit/pack.wit", ROOT / inputs.PACK / "wit/pack.wit"),
        (
            "wit/deps/tobkiri-core/core.wit",
            ROOT / "crates/tobkiri-wit/wit/core/core.wit",
        ),
        ("bundle_catalog_export.py", ROOT / inputs.PACK / "python/app.py"),
        (
            "bundle_catalog_adapter.py",
            ROOT / inputs.PACK / "python/bundle_catalog_adapter.py",
        ),
        ("bundle_catalog_loader.py", HERE / "schema_probe/captured_loader.py"),
    ):
        if fingerprint(actual) != {k: captured[member][k] for k in ("bytes", "sha256")}:
            raise ValueError("ordinary wrapper, resource owner or WIT differs")
    for record in source["original_modules"].values():
        if fingerprint(ROOT / "tobkiri_runtime" / record["path"]) != {
            k: record[k] for k in ("bytes", "sha256")
        }:
            raise ValueError("original catalog source differs from the actual supplier")
    for name, record in source["original_resources"].items():
        if fingerprint(ROOT / "tobkiri_runtime" / name) != record:
            raise ValueError("original package schema resource changed")
    core_wit = read(ROOT / "crates/tobkiri-wit/wit/core/core.wit", 4 * 1024 * 1024)
    catalog_files = {
        "component.wasm": component,
        "component.build.json": proof_raw,
        "wit/pack.wit": read(ROOT / inputs.PACK / "wit/pack.wit", 4 * 1024 * 1024),
        "wit/deps/core.wit": core_wit,
        "wit/catalog.shape.json": canonical(
            {"cardinality": "one", "streams": {}, "callbacks": {}, "conformance": None}
        ),
    }
    request_files = {
        "component.wasm": read(input_component, 32 * 1024 * 1024),
        "wit/deps/core.wit": core_wit,
        "wit/deps/catalog.wit": catalog_files["wit/pack.wit"],
    }
    for name in (
        "wit/pack.wit",
        "wit/requests.shape.json",
        "wit/catalog-flow.shape.json",
        "flows/read.flow.json",
    ):
        request_files[name] = read(ROOT / "packs" / ENTRY / name, 4 * 1024 * 1024)
    sources = [
        "packs/Cargo.toml",
        "packs/Cargo.lock",
        *[
            "packs/" + ENTRY + "/" + name
            for name in ("Cargo.toml", "build.rs", "src/lib.rs", "wit/pack.wit")
        ],
        "crates/tobkiri-wit/wit/core/core.wit",
    ]
    request_files["component.build.json"] = canonical(
        {
            "format": "tobkiri.component-build/1",
            "language": "rust",
            "artifact_sha256": "sha256:"
            + identity(request_files["component.wasm"])["sha256"],
            "artifact_bytes": len(request_files["component.wasm"]),
            "sources": {
                name: "sha256:" + fingerprint(ROOT / name)["sha256"] for name in sources
            },
            "verification_scope": "source/artifact identity; successful build is a separate CI step",
            "production_enabled": False,
        }
    )
    if any(
        sum(map(len, files.values())) > 32 * 1024 * 1024
        for files in (catalog_files, request_files)
    ):
        raise ValueError("ordinary source package exceeds original 32-MiB bound")
    destination.mkdir(mode=0o700)
    records = [
        write_pack(
            destination,
            CATALOG_PACK,
            catalog_files,
            shapes=[(CATALOG, "port")],
            provides=[CATALOG],
            consumes=[],
            allowed=[],
            flows=[],
            toolchain="original-python313-producer/componentize-py-0.25.0",
        ),
        write_pack(
            destination,
            ENTRY,
            request_files,
            shapes=[(REQUESTS, "port"), (FLOW, "flow")],
            provides=[REQUESTS],
            consumes=[port(CATALOG), port(FLOW, optional=True)],
            allowed=[CATALOG, FLOW],
            flows=["flows/read.flow.json"],
            toolchain="rust/1.94.0;wit-bindgen/0.62.0",
        ),
    ]
    with (destination / "materialization.json").open("xb") as stream:
        stream.write(
            canonical(
                {
                    "format": "tobkiri.bundle-catalog-ordinary-packs/1",
                    "packs": records,
                    "defaults_embedded_in_actual_component": True,
                    "runtime_config_override_used": False,
                    "independent_unsigned_local_packs": True,
                    "actual_Profile_or_approval_created": False,
                    "ordinary_nested_Flow_executed": False,
                    "original_GUI_connected": False,
                    "production_enabled": False,
                }
            )
        )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("supplier", "input-component", "destination"):
        parser.add_argument("--" + name, type=Path, required=True)
    run(**vars(parser.parse_args()))
