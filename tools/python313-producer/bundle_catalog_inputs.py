"""Stage the genuine bundle catalog source and its DATA resources for two worlds.

This producer captures source and dependencies only. It does not import the
original runtime or execute either native comparison or ordinary Pack code.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from build_native import HERE
from bundle_catalog_resources import resources
from capability_projection_inputs import runtime_sources
from fetch_sdk import inventory
from harness_native_dependencies import MAX_FILE, MAX_TOTAL, _read, compress_sources
from schema_probe.captured_loader import Finder

PACK = "packs/local.harness-bundle-catalog-python"
ROOTS = ("tobkiri_protocol.bundle_catalog",)
CORE_IMPORTS = [f"tobkiri:core/{name}@1.0.0" for name in
                ("types", "flow", "clock", "random", "audit")]
NATIVE_EXPORT = "tobkiri-ui:capability-projection/projection@1.0.0"
PACK_EXPORT = "local:harness-bundle-catalog-python/catalog@1.0.0"
NATIVE_COMPONENT = "harness-bundle-catalog-native"
DIAGNOSTIC_COMPONENT = "harness-bundle-catalog-reread-native"
PACK_COMPONENT = "harness-bundle-catalog"
MAX_SOURCE_FILE = MAX_FILE


def _read_source(path: Path, expected: dict | None = None) -> bytes:
    """Read one regular source file and enforce the bounded staging member."""
    raw = _read(path, expected)
    if len(raw) > MAX_SOURCE_FILE:
        raise ValueError("bundle catalog source file exceeds original member bound")
    return raw


def _original_schema_resources(repo: Path) -> tuple[dict[str, bytes], dict[str, dict]]:
    """Capture every genuine protocol schema as exact immutable source bytes."""
    runtime_root = repo / "tobkiri_runtime"
    schema_root = runtime_root / "tobkiri_protocol" / "schemas"
    if (schema_root.is_symlink() or not schema_root.is_dir()
            or schema_root.resolve() != schema_root):
        raise ValueError("physical original protocol schema directory required")
    paths = sorted(schema_root.glob("*.schema.json"))
    if not paths:
        raise ValueError("original protocol schema resources are required")
    resources: dict[str, bytes] = {}
    receipt: dict[str, dict] = {}
    for path in paths:
        if path.is_symlink() or not path.is_file() or path.resolve() != path:
            raise ValueError("regular original schema resource required")
        name = path.relative_to(runtime_root).as_posix()
        raw = _read_source(path)
        resources[name] = raw
        receipt[name] = {"bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}
    return resources, receipt


def stage(repo: Path, destination: Path, verified: dict, schema_inputs: Path,
          stdlib: Path, rpds: Path, *, native: bool, bundle_cases: Path,
          diagnostic_reread: bool = False,
          kernel_extension: Path | None = None, app_schema_backend: bool = False) -> dict:
    """Stage actual source bytes, schemas, and dependencies for one world."""
    if (repo.resolve() != repo or destination.exists()
            or type(native) is not bool or type(diagnostic_reread) is not bool
            or type(app_schema_backend) is not bool
            or (app_schema_backend and kernel_extension is None)
            or (diagnostic_reread and not native)
            or (kernel_extension is not None and (not native or diagnostic_reread))):
        raise ValueError("new physical source stage and explicit world required")

    runtime_files, modules, namespaces = runtime_sources(repo, roots=ROOTS)
    original_files = dict(runtime_files)
    resource_files, original_resources = _original_schema_resources(repo)
    files = dict(runtime_files)
    distributions = {}

    def add(name: str, raw: bytes) -> None:
        if (not name or name.startswith("/") or "\\" in name or ":" in name
                or "\0" in name or any(part in ("", ".", "..")
                                       for part in name.split("/"))
                or name in files or type(raw) is not bytes
                or len(raw) > MAX_SOURCE_FILE):
            raise ValueError("bundle catalog source collision or file bound")
        files[name] = raw

    for name, raw in resource_files.items():
        add(name, raw)
    cases_raw = _read_source(bundle_cases)
    if len(cases_raw) > 4 * 1024 * 1024:
        raise ValueError("original bundle comparison exceeds four MiB")
    cases = json.loads(cases_raw)
    if (cases.get("format") != "tobkiri.bundle-catalog-comparison/1"
            or cases.get("oracle_commit") != "54e7da008997a3eb45876cbe18459dba9902cc9d"
            or type(cases.get("payload")) is not str
            or type(cases.get("source_identity")) is not dict):
        raise ValueError("independent pinned original bundle DATA required")
    members, embedded_resources = resources(cases, diagnostic_reread=diagnostic_reread)
    for name, raw in members.items():
        add(name, raw)
    for pin in verified["inputs"]:
        native_wheel = pin["role"] == "native-oracle"
        root = schema_inputs / "packages" / (
            "rpds-py-native" if native_wheel else pin["distribution"]
        )
        for name, entry in pin["files"].items():
            if native_wheel and (not name.startswith("rpds/") or name.endswith(".so")):
                continue
            add(name, _read_source(root / name, entry))
            if not native_wheel and name.endswith(".dist-info/METADATA"):
                if pin["distribution"] in distributions:
                    raise ValueError("duplicate bundle catalog distribution")
                distributions[pin["distribution"]] = name.split("/")[0]

    if len(distributions) != 5 or "rpds/__init__.py" not in files:
        raise ValueError("complete locked schema dependency closure required")
    for name, entry in verified["stdlib"]["files"].items():
        add(name, _read_source(stdlib / "source" / name, entry))

    add("bundle_catalog_adapter.py", _read_source(
        repo / PACK / "python/bundle_catalog_adapter.py"
    ))
    entry = _read_source(HERE / "bundle_catalog/entry.py")
    export = _read_source(HERE / "bundle_catalog/native.py" if native
                          else repo / PACK / "python/app.py")
    kernel_derivation, kernel_bytes = None, None
    if kernel_extension is not None:
        from bundle_catalog_kernel_inputs import derive
        entry, export, kernel_bytes, kernel_derivation = derive(
            files, entry, export, kernel_extension, app_backend=app_schema_backend)
    packed = compress_sources(files)
    captured = Finder(
        packed,
        distributions,
        "/captured-bundle-catalog",
        namespaces,
        compressed=True,
    ).tree
    for name, raw in files.items():
        if captured.files[name] != raw:
            raise ValueError("bundle catalog capture differs from original bytes")

    data = (
        "FILES = " + repr(packed) + "\n"
        + "DISTRIBUTIONS = " + repr(distributions) + "\n"
        + "NAMESPACES = " + repr(namespaces) + "\n"
    ).encode()
    add("bundle_catalog_data.py", data)
    add("bundle_catalog_loader.py", _read_source(
        HERE / "schema_probe/captured_loader.py"
    ))
    add("rpds/rpds.abi3.so", _read_source(
        rpds / "work/stage/rpds.abi3.so", verified["extension"]
    ))
    add("bundle_catalog_export.py", export)
    add("app.py", entry)
    if kernel_bytes is not None:
        add("_tobkiri_schema_kernel.abi3.so", kernel_bytes)

    component_name = (DIAGNOSTIC_COMPONENT if diagnostic_reread
                      else NATIVE_COMPONENT if native else PACK_COMPONENT)
    if kernel_derivation is not None:
        from bundle_catalog_kernel_inputs import COMPONENT, APP_COMPONENT
        component_name = APP_COMPONENT if app_schema_backend else COMPONENT
    source_receipt = {
        "format": "tobkiri.bundle-catalog-source/1",
        "component_name": component_name,
        "original_modules": modules,
        "original_resources": original_resources,
        "embedded_resources": embedded_resources,
        "embedded_carrier": {"bytes": len(cases["payload"].encode()), "sha256": hashlib.sha256(cases["payload"].encode()).hexdigest()},
        "original_bundle_cases": {"bytes": len(cases_raw), "sha256": hashlib.sha256(cases_raw).hexdigest()},
        "runtime_config_override_used": False,
        "schema_dependencies": verified,
        "adapter_sha256": hashlib.sha256(files["bundle_catalog_adapter.py"]).hexdigest(),
        "source_storage": "same original bytes, per-file zlib/identity, no expansion cache",
        "captured_tree_accounted_bytes": captured.accounted_bytes,
        "scope": "immutable bundle DATA and source-owned schema interpretation",
        "platform_artifact_io_claimed": False,
        "active_profile_or_plan_authority_claimed": False,
        "full_setup_gui_wiring_claimed": False,
        "installed_dmg_admission_claimed": False,
        "production_enabled": False,
    }
    if kernel_derivation is not None:
        source_receipt["kernel_derivation"] = kernel_derivation
        change = ("declared App validator import derivation; original keyword bytes unchanged; "
                  if app_schema_backend else
                  "declared keyword source derivation; other original bytes unchanged; ")
        source_receipt["source_storage"] = change + "per-file zlib/identity, no expansion cache"
    add("bundle-catalog-source.json", (
        json.dumps(source_receipt, sort_keys=True, indent=2) + "\n"
    ).encode())

    code = dict(files)
    staged_files = {"python/" + name: raw for name, raw in code.items()}
    staged_files["wit/pack.wit"] = _read_source(
        HERE / "bundle_catalog/native.wit" if native
        else repo / PACK / "wit/pack.wit"
    )
    if not native:
        staged_files["wit/deps/tobkiri-core/core.wit"] = _read_source(
            repo / "crates/tobkiri-wit/wit/core/core.wit"
        )
    if any(len(raw) > MAX_SOURCE_FILE for raw in staged_files.values()):
        raise ValueError("bundle catalog source file exceeds four MiB")
    if sum(map(len, staged_files.values())) > MAX_TOTAL:
        raise ValueError("bundle catalog source stage exceeds original 32 MiB bound")

    plan = {
        "format": "tobkiri.python-build/3",
        "entrypoint": "app",
        "wit": "wit/pack.wit",
        "world": "capability-projection" if native else "pack",
        "stub_wasi": True,
        "allowed_imports": [] if native else CORE_IMPORTS,
        "outer_exports": [NATIVE_EXPORT] if native else [
            "tobkiri:core/lifecycle@1.0.0", PACK_EXPORT
        ],
        "sources": [
            {
                "source": name,
                "destination": name.removeprefix("python/"),
                "sha256": hashlib.sha256(raw).hexdigest(),
            }
            for name, raw in sorted(staged_files.items())
        ],
    }
    destination.mkdir(mode=0o700)
    for name, raw in staged_files.items():
        path = destination / name
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with path.open("xb") as stream:
            stream.write(raw)
    plan_raw = (json.dumps(plan, sort_keys=True, indent=2) + "\n").encode()
    (destination / "plan.json").write_bytes(plan_raw)
    provenance = {
        "format": "tobkiri.bundle-catalog-stage/1",
        "component_name": component_name,
        "original_files": {
            name: hashlib.sha256(raw).hexdigest()
            for name, raw in original_files.items()
        },
        "original_resources": original_resources,
        "source_receipt": source_receipt,
        "production_enabled": False,
    }
    (destination / "provenance.json").write_text(
        json.dumps(provenance, sort_keys=True, indent=2) + "\n"
    )
    return {
        "plan_sha256": hashlib.sha256(plan_raw).hexdigest(),
        "source_files": plan["sources"],
        "files": inventory(destination),
        "component_name": component_name,
        "bundle_catalog": source_receipt,
    }


def verify_component(proof: dict, source: dict, *, native: bool,
                     diagnostic_reread: bool = False,
                     kernel_derived: bool = False, app_schema_backend: bool = False) -> None:
    """Require the exact closed source manifest and typed component boundary."""
    expected = [
        {**item, "bytes": source["files"][item["source"]]["bytes"]}
        for item in source["source_files"]
    ]
    exports = [NATIVE_EXPORT] if native else [
        "tobkiri:core/lifecycle@1.0.0", PACK_EXPORT
    ]
    imports = [] if native else CORE_IMPORTS
    if type(diagnostic_reread) is not bool or (diagnostic_reread and not native):
        raise ValueError("explicit valid diagnostic world required")
    expected_name = (DIAGNOSTIC_COMPONENT if diagnostic_reread
                     else NATIVE_COMPONENT if native else PACK_COMPONENT)
    if kernel_derived:
        from bundle_catalog_kernel_inputs import COMPONENT, APP_COMPONENT
        if not native or diagnostic_reread:
            raise ValueError("separate native-only kernel derivation required")
        expected_name = APP_COMPONENT if app_schema_backend else COMPONENT
    if type(app_schema_backend) is not bool or (app_schema_backend and not kernel_derived):
        raise ValueError("explicit App backend derivation required")
    if kernel_derived and source.get("bundle_catalog", {}).get("kernel_derivation", {}).get("app_schema_backend", False) != app_schema_backend:
        raise ValueError("actual App backend source mode differs")
    if type(kernel_derived) is not bool:
        raise ValueError("explicit kernel derivation flag required")
    if ("kernel_derivation" in source.get("bundle_catalog", {})) != kernel_derived:
        raise ValueError("explicit matching kernel derivation scope required")
    if (
        source.get("component_name") != expected_name
        or proof.get("source_manifest") != expected
        or proof.get("build_plan_sha256") != source["plan_sha256"]
        or proof.get("world") != ("capability-projection" if native else "pack")
        or proof.get("imports") != sorted(imports)
        or proof.get("exports") != sorted(exports)
        or proof.get("inner", {}).get("imports") != sorted(imports)
        or proof.get("inner", {}).get("exports") != sorted([*exports, "exports"])
    ):
        raise ValueError("actual bundle catalog component source or boundary differs")
