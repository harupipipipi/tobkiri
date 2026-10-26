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

ROOT = Path(__file__).resolve().parents[2]
RUNTIME = "tobkiri-ui:harness-native/runtime@1.0.0"
TRANSPORT = "tobkiri-ui:harness-native/transport@1.0.0"
IMPORT = "tobkiri-native:harness/bindings@1.0.0"


def bind(artifact: Path, output: Path, github_env: Path) -> dict:
    """Verify immutable source/build identities and emit data-only Rust bindings."""
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
    if (build["format"] != "tobkiri.component-build/2"
            or build["world"] != "harness-native"
            or plan["world"] != "harness-native"
            or build["artifact_sha256"] != identity["sha256"]
            or build["artifact_bytes"] != identity["bytes"]
            or build["build_plan_sha256"] != hashlib.sha256(raw_plan).hexdigest()
            or build["imports"] != [IMPORT]
            or set(build["exports"]) != {RUNTIME, TRANSPORT}
            or build["production_enabled"] is not False):
        raise ValueError("actual native Harness artifact/source/ABI differs")

    previous = sys.path[:]
    sys.path.insert(0, str(ROOT / "scripts/wasm"))
    try:
        import stage_harness_native as stage

        base = stage.captured()
    finally:
        sys.path[:] = previous
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
    ).encode()
    if not output.is_absolute() or output.parent.resolve(strict=True) != output.parent:
        raise ValueError("new physical output directory required")
    output.mkdir(mode=0o700)
    index_path = output / "module-index.rs"
    index_path.write_bytes(header)
    env = {
        "TOBKIRI_TEST_NATIVE_HARNESS_COMPONENT": str(artifact),
        "TOBKIRI_TEST_NATIVE_HARNESS_COMPONENT_SHA256": identity["sha256"],
        "TOBKIRI_HARNESS_NATIVE_MODULE_INDEX_RS": str(index_path),
        "TOBKIRI_TEST_NATIVE_HARNESS_RUNTIME_ROOT": str(ROOT / "tobkiri_runtime"),
        "TOBKIRI_TEST_NATIVE_HARNESS_CI": "1",
    }
    if any("\n" in value or "\r" in value for value in env.values()):
        raise ValueError("native input path cannot contain an environment delimiter")
    report = {
        "format": "io.tobkiri.harness-native-ci-inputs.v1",
        "component": identity, "source_plan": fingerprint(inputs / "plan.json"),
        "source_index_sha256": index["INDEX_SHA256"], "module_anchors": len(selected),
        "native_rust_index": fingerprint(index_path), "current_sources_verified": True,
        "component_init_verified": False, "production_enabled": False,
    }
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
    args = parser.parse_args()
    print(json.dumps(bind(args.artifact, args.output, args.github_env), sort_keys=True))
