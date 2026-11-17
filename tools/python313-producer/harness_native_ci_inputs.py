"""Bind the actual native Harness artifact and current module index for Mac CI.

No artifact is executed here. The Rust consumer must perform Runtime.init and
the original calls under genuine physical-root funding before proving success.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import sys

from build_native import fingerprint
from harness_native_dependencies import DIAGNOSTIC_FORMAT, ENTRYPOINT, METADATA, _read
from harness_native_dependencies import verify_component_receipt
from schema_probe.captured_loader import Tree

ROOT = Path(__file__).resolve().parents[2]
RUNTIME = "tobkiri-ui:harness-native/runtime@1.0.0"
TRANSPORT = "tobkiri-ui:harness-native/transport@1.0.0"
IMPORT = "tobkiri-native:harness/bindings@1.0.0"
NORMAL_SOURCE = "io.tobkiri.harness-native-component-source.v1"
DIAGNOSTIC_SOURCE = "io.tobkiri.harness-native-schema-call-diagnostic-source.v1"
NORMAL_DEPENDENCIES = "tobkiri.harness-native-dependencies/1"
CAPTURED_DATA = "harness_native_dependency_data.py"
PROFILE_SCHEMA_PATHS = {
    version: f"tobkiri_protocol/schemas/profile_{version}.schema.json"
    for version in ("v4", "v5")
}
PROFILE_REFERENCE_IDS = {
    "common": "https://schemas.tobkiri.local/io.tobkiri/common.v1.schema.json",
    "content_projection": (
        "https://schemas.tobkiri.local/io.tobkiri/profile-content-projection.v1.schema.json"
    ),
}
PROFILE_REFERENCE_PATHS = {
    "common": "tobkiri_protocol/schemas/common_v1.schema.json",
    "content_projection": "tobkiri_protocol/schemas/profile_content_projection_v1.schema.json",
}


def verify_producer_selection(
    artifact: Path, build: dict, plan: dict, identity: dict, *, diagnostic: bool,
    app_backend: bool = False,
) -> None:
    """Bind this same-run component to its successful producer and all inputs."""
    bundle = artifact.parent.parent
    if app_backend and diagnostic:
        raise ValueError("App provider and schema diagnostics are separate Harness variants")
    variant = "app-frame" if app_backend else "schema-diagnostic" if diagnostic else "candidate"
    name = "harness-native" if variant == "candidate" else f"harness-native-{variant}"
    receipt_path = bundle.parent / "components-build.json"
    fingerprint(receipt_path, 4 * 1024**2)
    receipt = json.loads(receipt_path.read_bytes())
    if (
        receipt.get("format") != f"tobkiri.python313-native-harness-{variant}/1"
        or receipt.get("outcome") != "success"
        or receipt.get("inputs_unchanged") is not True
        or receipt.get("production_enabled") is not False
        or receipt.get("diagnostic_only", False) is not diagnostic
        or receipt.get("app_schema_backend", False) is not app_backend
        or set(receipt.get("components", {})) != {name}
        or bundle.name != name
    ):
        raise ValueError("native Harness producer selection differs")
    member = receipt["components"][name]
    source = member["source"]
    inputs = bundle / "inputs"
    metadata = fingerprint(inputs / "python" / METADATA, 4 * 1024**2)
    # Artifact transport preserves file bytes but normalizes producer file modes.
    provenance_file = source["files"]["provenance.json"]
    metadata_file = source["files"]["python/" + METADATA]
    if (
        member["artifact"] != identity
        or member["build_receipt"] != fingerprint(
            artifact.with_name("component.build.json"), 4 * 1024**2
        )
        or {key: provenance_file[key] for key in ("bytes", "sha256")}
        != fingerprint(inputs / "provenance.json")
        or {key: metadata_file[key] for key in ("bytes", "sha256")} != metadata
        or source["source_files"] != plan["sources"]
        or build["compiler_executable_sha256"] != receipt["producer"]["sha256"]
        or build["producer_ci_receipt_sha256"] != receipt["producer_receipt"]["sha256"]
        or plan["entrypoint"] != ENTRYPOINT
    ):
        raise ValueError("native Harness files differ from the successful producer")
    verify_component_receipt(build, source, inputs)
    dependencies = build["harness_native_dependencies"]
    if (
        dependencies.get("format") != (DIAGNOSTIC_FORMAT if diagnostic else NORMAL_DEPENDENCIES)
        or dependencies.get("diagnostic_only", False) is not diagnostic
        or ("app_schema_backend" in dependencies) is not app_backend
        or dependencies.get("original_source_files_unchanged") is not (not diagnostic and not app_backend)
    ):
        raise ValueError("native Harness dependency mode differs")


def verify_app_source_bindings(build: dict, actual: dict) -> None:
    from app_schema_extension_inputs import EXTENSION, PROVIDER_FILES, SOURCE
    from app_schema_extension_inputs import derive_harness_entry, verify_report

    evidence = build["harness_native_dependencies"]["app_schema_backend"]
    report = evidence["extension_build"]
    if evidence.get("validation_results_prepared") is not False:
        raise ValueError("App provider requires live schema evaluation")
    verify_report(report, ["app-schema-backend"])
    wrapper = derive_harness_entry((SOURCE.parent / "harness_native/entry_wrapper.py").read_bytes())
    expected = {EXTENSION: report["artifact"]["sha256"],
                ENTRYPOINT + ".py": hashlib.sha256(wrapper).hexdigest()}
    expected.update({name: report["source_files"][member]["sha256"]
                     for name, member in PROVIDER_FILES.items()})
    for name, digest in expected.items():
        row = actual.get("python/" + name, {})
        if row.get("sha256") != digest or row.get("destination") != name:
            raise ValueError(f"native Harness App dependency differs: {name}")


def _profile_schema_resources(
    inputs: Path, base: dict[str, bytes], manifest: list[dict],
) -> dict[str, bytes]:
    """Select the original Profile roots and references from captured DATA."""
    def row_for(destination: str) -> dict:
        source = "python/" + destination
        rows = [row for row in manifest
                if row["source"] == source or row["destination"] == destination]
        if (len(rows) != 1 or rows[0]["source"] != source
                or rows[0]["destination"] != destination):
            raise ValueError(f"invalid fixed capture source: {destination}")
        return rows[0]

    capture = row_for(CAPTURED_DATA)
    tree = ast.parse(_read(inputs / capture["source"], capture))
    tables = [node.value for node in tree.body if isinstance(node, ast.Assign)
              and any(isinstance(target, ast.Name) and target.id == "FILES"
                      for target in node.targets)]
    if len(tables) != 1 or not isinstance(tables[0], ast.Tuple):
        raise ValueError("native captured DATA must have one literal FILES table")
    paths = {**PROFILE_SCHEMA_PATHS, **PROFILE_REFERENCE_PATHS}
    wanted = set(paths.values())
    selected = tuple(ast.literal_eval(row) for row in tables[0].elts
                     if isinstance(row, ast.Tuple) and row.elts
                     and isinstance(row.elts[0], ast.Constant)
                     and row.elts[0].value in wanted)
    if len(selected) != len(wanted) or {row[0] for row in selected} != wanted:
        raise ValueError("native captured DATA must contain each Profile schema and reference once")
    resources = Tree(selected, compressed=True)
    result = {}
    for key, name in paths.items():
        row = row_for(name)
        raw, digest = resources.path(name)._read_verified_bytes()
        if (not raw or len(raw) != row["bytes"] or digest != row["sha256"]
                or raw != base.get(row["source"])):
            raise ValueError(f"captured Profile schema differs from original source: {name}")
        if key in PROFILE_REFERENCE_IDS:
            document = json.loads(raw)
            if (not isinstance(document, dict)
                    or document.get("$id") != PROFILE_REFERENCE_IDS[key]):
                raise ValueError(f"captured Profile reference has an unexpected $id: {name}")
        result[key] = raw
    return result


def _rust_profile_schemas(component_sha256: str, schemas: dict[str, bytes]) -> bytes:
    """Write immutable Rust literals without source-language string ambiguities."""
    def literal(raw: bytes) -> str:
        chunks = ["".join(f"\\x{byte:02x}" for byte in raw[start:start + 20])
                  for start in range(0, len(raw), 20)]
        return 'b"' + "\\\n        ".join(chunks) + '"'

    lines = [
        "\npub static PROFILE_SCHEMAS: BootstrapProfileSchemas = BootstrapProfileSchemas {",
        f'    component_sha256: "sha256:{component_sha256}",',
        *(f"    {version}: {literal(schemas[version])}," for version in PROFILE_SCHEMA_PATHS),
        "    references: Some([",
        *(f"        {literal(schemas[key])}," for key in PROFILE_REFERENCE_PATHS),
        "    ]),",
    ]
    return ("\n".join([*lines, "};", ""])).encode("ascii")


def _profile_schema_receipt(schemas: dict[str, bytes]) -> dict:
    def row(key: str) -> dict[str, str]:
        raw = schemas[key]
        return {"sha256": hashlib.sha256(raw).hexdigest(), "content": raw.decode("utf-8")}

    return {
        **{version: row(version) for version in PROFILE_SCHEMA_PATHS},
        "references": {key: row(key) for key in PROFILE_REFERENCE_PATHS},
    }


def bind(
    artifact: Path, output: Path, github_env: Path, *,
    schema_call_diagnostic: bool = False,
    app_backend: bool = False,
) -> dict:
    """Verify immutable source/build identities and emit data-only Rust bindings."""
    if type(schema_call_diagnostic) is not bool or type(app_backend) is not bool:
        raise TypeError("native Harness binding requires explicit variant booleans")
    if schema_call_diagnostic and app_backend:
        raise ValueError("App provider and schema diagnostics are separate Harness variants")
    if sys.platform != "darwin" or os.environ.get("GITHUB_ACTIONS") != "true":
        raise ValueError("native Harness inputs are restricted to actual Mac CI")
    if not artifact.is_absolute() or artifact.resolve(strict=True) != artifact:
        raise ValueError("physical component artifact required")
    build_path = artifact.with_name("component.build.json")
    inputs = artifact.parent.parent / "inputs"
    for name in ("plan.json", "provenance.json"):
        fingerprint(inputs / name, 4 * 1024**2)
    fingerprint(build_path, 4 * 1024**2)
    build = json.loads(build_path.read_bytes())
    raw_plan = (inputs / "plan.json").read_bytes()
    plan = json.loads(raw_plan)
    identity = fingerprint(artifact, 32 * 1024**2)
    if (identity["bytes"] == 0
            or build["format"] != "tobkiri.component-build/2"
            or build["world"] != "harness-native"
            or plan["world"] != "harness-native"
            or build["artifact_sha256"] != identity["sha256"]
            or build["artifact_bytes"] != identity["bytes"]
            or build["build_plan_sha256"] != hashlib.sha256(raw_plan).hexdigest()
            or build["imports"] != [IMPORT]
            or build["exports"] != [RUNTIME, TRANSPORT]
            or build["production_enabled"] is not False):
        raise ValueError("actual native Harness artifact/source/ABI differs")
    previous = sys.path[:]
    sys.path.insert(0, str(ROOT / "scripts/wasm"))
    try:
        import stage_harness_native as stage

        base = (stage.captured(schema_call_diagnostic=True)
                if schema_call_diagnostic else stage.captured())
    finally:
        sys.path[:] = previous
    provenance = (inputs / "provenance.json").read_bytes()
    if provenance != base["provenance.json"]:
        raise ValueError("native Harness provenance differs from the selected source")
    derived = json.loads(provenance)
    if derived.get("format") != (DIAGNOSTIC_SOURCE if schema_call_diagnostic else NORMAL_SOURCE):
        raise ValueError("native Harness source mode differs")
    original_plan = (json.dumps(stage.plan_for(base), sort_keys=True, indent=2) + "\n").encode()
    if app_backend:
        from app_schema_extension_inputs import derive_harness_sources

        changes = derive_harness_sources({name.removeprefix("python/"): raw
                                          for name, raw in base.items()})
        base.update({"python/" + name: raw for name, raw in changes.items()})
    diagnostic_contract = None
    if schema_call_diagnostic:
        diagnostic_contract = derived.get("checkpoint_contract", {})
        if (derived.get("diagnostic_only") is not True
                or derived.get("original_source_files_unchanged") is not False
                or derived.get("other_original_members_unchanged") is not True
                or diagnostic_contract.get("tag_count") != 69
                or len(diagnostic_contract.get("schema_tags", [])) != 69
                or diagnostic_contract.get("maximum_sequence") != 139
                or diagnostic_contract.get("overflow_sequence") != 140
                or diagnostic_contract.get("overflow_notification_limit") != 1
                or derived.get("production_enabled") is not False):
            raise ValueError("explicit bounded schema diagnostic source required")
    actual = {row["source"]: row for row in plan["sources"]}
    if len(actual) != len(plan["sources"]):
        raise ValueError("duplicate actual component input")
    for name, data in base.items():
        if name == "provenance.json":
            continue
        row = actual.get(name)
        if (row is None or row["sha256"] != hashlib.sha256(data).hexdigest()
                or row["destination"] != name.removeprefix("python/")):
            raise ValueError(f"actual component lacks current original source: {name}")
    verify_producer_selection(artifact, build, plan, identity, diagnostic=schema_call_diagnostic,
                              app_backend=app_backend)
    if app_backend:
        verify_app_source_bindings(build, actual)
    if build["harness_native_dependencies"]["original_plan_sha256"] != hashlib.sha256(
        original_plan
    ).hexdigest():
        raise ValueError("native Harness original source plan differs")
    profile_schemas = _profile_schema_resources(inputs, base, build["source_manifest"])
    tree = ast.parse(base["python/harness_native_source_index.py"])
    index = {row.targets[0].id: ast.literal_eval(row.value)
             for row in tree.body if isinstance(row, ast.Assign)}
    modules = {row[0]: row for row in index["MODULES"]}
    selected = []
    for name in index["PATH_MODULES"]:
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*", name):
            raise ValueError("invalid fixed module name")
        row = modules[name]
        relative = row[1].removeprefix("tobkiri_runtime/")
        path = PurePosixPath(relative)
        if (not relative.isascii() or path.is_absolute()
                or path.as_posix() != relative or any(p in (".", "..") for p in path.parts)
                or any(c in relative for c in "\\\0:\r\n")):
            raise ValueError("invalid fixed module resource path")
        selected.append((name, relative))
    if not selected or len(selected) > 128 or len(set(selected)) != len(selected):
        raise ValueError("invalid original module binding inventory")
    header = (
        "// Generated data from the hash-bound actual component source index.\n"
        "pub static INDEX: BootstrapModuleIndex = BootstrapModuleIndex {\n"
        f"    sha256: {json.dumps(index['INDEX_SHA256'])},\n    modules: &[\n"
        + "".join("        BootstrapModule { module: " + json.dumps(name)
                  + ", relative_file: " + json.dumps(relative) + " },\n"
                  for name, relative in selected)
        + "    ],\n};\n"
    ).encode() + _rust_profile_schemas(identity["sha256"], profile_schemas)
    if diagnostic_contract is not None:
        header += (
            '#[cfg(feature = "catalog-diagnostics-ci")]\n'
            'pub const CI_SCHEMA_TAG_COUNT: usize = 69;\n'
        ).encode()
    if not output.is_absolute() or output.parent.resolve(strict=True) != output.parent:
        raise ValueError("new physical output directory required")
    output.mkdir(mode=0o700)
    index_path = output / "module-index.rs"
    index_path.write_bytes(header)
    launcher_receipt = None
    if not schema_call_diagnostic and not app_backend:
        # Data only. A separately pinned copy of this receipt can be consumed
        # by the Launcher build; this CI verification does not enable release.
        launcher_receipt = output / "launcher-component.json"
        launcher_receipt.write_text(json.dumps({
            "format": "io.tobkiri.launcher-setup-component.v2",
            "component": identity,
            "source_index_sha256": index["INDEX_SHA256"],
            "modules": [
                {"module": module, "relative_file": relative}
                for module, relative in selected
            ],
            "profile_schemas": _profile_schema_receipt(profile_schemas),
        }, sort_keys=True) + "\n", encoding="utf-8")
    env = {
        "TOBKIRI_TEST_NATIVE_HARNESS_COMPONENT": str(artifact),
        "TOBKIRI_TEST_NATIVE_HARNESS_COMPONENT_SHA256": identity["sha256"],
        "TOBKIRI_HARNESS_NATIVE_MODULE_INDEX_RS": str(index_path),
        "TOBKIRI_TEST_NATIVE_HARNESS_RUNTIME_ROOT": str(ROOT / "tobkiri_runtime"),
        "TOBKIRI_TEST_NATIVE_HARNESS_CI": "1",
    }
    if schema_call_diagnostic:
        env["TOBKIRI_TEST_NATIVE_HARNESS_SCHEMA_DIAGNOSTIC"] = "1"
    if any("\n" in value or "\r" in value for value in env.values()):
        raise ValueError("native input path cannot contain an environment delimiter")
    report = {
        "format": "io.tobkiri.harness-native-ci-inputs.v1",
        "component": identity, "source_plan": fingerprint(inputs / "plan.json"),
        "source_index_sha256": index["INDEX_SHA256"], "module_anchors": len(selected),
        "native_rust_index": fingerprint(index_path), "current_sources_verified": True,
        "component_init_verified": False, "production_enabled": False,
        "producer_selection_verified": True,
    }
    if launcher_receipt is not None:
        report["launcher_component_receipt"] = fingerprint(launcher_receipt)
    if diagnostic_contract is not None:
        report.update({
            "schema_call_diagnostic": True,
            "original_source_files_unchanged": False,
            "actual_derived_source_index_verified": True,
            "diagnostic_source_provenance": fingerprint(inputs / "provenance.json"),
            "checkpoint_contract": diagnostic_contract,
            "whole_catalog_acceptance": False,
        })
    if app_backend:
        report.update(app_schema_backend=True, original_source_files_unchanged=False,
                      actual_derived_source_index_verified=True, whole_catalog_acceptance=False)
    (output / "inputs.json").write_text(json.dumps(report, indent=2) + "\n")
    with github_env.open("a", encoding="utf-8") as stream:
        for name, value in env.items():
            stream.write(f"{name}={value}\n")
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifact", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--github-env", type=Path, required=True)
    parser.add_argument("--schema-call-diagnostic", action="store_true")
    parser.add_argument("--app-backend", action="store_true")
    args = parser.parse_args()
    print(json.dumps(bind(
        args.artifact, args.output, args.github_env,
        schema_call_diagnostic=args.schema_call_diagnostic,
        app_backend=args.app_backend,
    ), sort_keys=True))
