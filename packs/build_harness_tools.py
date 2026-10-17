"""Stage the real private-storage tool component and its declared inventory."""
from __future__ import annotations

import argparse
from pathlib import Path
import shutil
from typing import Any

import build

ROOT = Path(__file__).resolve().parent
REPO = ROOT.parent


def inventory() -> list[dict[str, Any]]:
    """Return exactly the operations implemented by local.harness-tools."""
    path = {"type": "string", "minLength": 1, "maxLength": 240,
            "description": "Logical relative artifact path, at most 240 UTF-8 bytes; not a host file path."}

    def tool(name: str, description: str, properties: dict[str, Any],
             required: list[str]) -> dict[str, Any]:
        return {"name": name, "description": description,
                "parameters": {"type": "object", "properties": properties,
                               "required": required, "additionalProperties": False}}

    return [
        tool("artifact.write", "Save UTF-8 text in this Pack's private Core store. "
             "Requires a stable outer call id; repeating that id returns its receipt.",
             {"path": path, "content": {"type": "string", "maxLength": 262144,
                                       "description": "At most 262144 UTF-8 bytes."}},
             ["path", "content"]),
        tool("artifact.read", "Read a stored text artifact, byte size and SHA-256.",
             {"path": path}, ["path"]),
        tool("artifact.stat", "Verify artifact existence, byte size and SHA-256.",
             {"path": path}, ["path"]),
        tool("artifact.list", "List artifact metadata with bounded pagination.",
             {"prefix": {"type": "string", "maxLength": 240},
              "after": {"type": "string", "maxLength": 240},
              "limit": {"type": "integer", "minimum": 1, "maximum": 100}}, []),
        tool("artifact.delete", "Mark a logical artifact deleted. Requires a stable "
             "outer call id. Prior content and immutable receipts remain for safe continuation.",
             {"path": path}, ["path"]),
        tool("calculator", "Checked StrictV1 safe integer arithmetic, left to "
             "right. Inputs and results must be within +/-9007199254740991. "
             "Division must be exact. No expression or code evaluation.",
             {"operation": {"enum": ["add", "subtract", "multiply", "divide"]},
              "operands": {"type": "array", "minItems": 2, "maxItems": 64,
                           "items": {"type": "integer", "minimum": -(2**53-1),
                                     "maximum": 2**53-1}}},
             ["operation", "operands"]),
        tool("json.validate", "Check JSON syntax and report type or error position.",
             {"text": {"type": "string", "maxLength": 262144}}, ["text"]),
        tool("clock.now", "Read the Core clock as Unix milliseconds.", {}, []),
        tool("random.bytes", "Read 1..256 bytes from the Core random source as hex.",
             {"length": {"type": "integer", "minimum": 1, "maximum": 256}},
             ["length"]),
    ]


def stage(target_dir: Path) -> dict[str, Any]:
    """Hash actual component bytes; do not invent build or execution receipts."""
    directory = ROOT / "local.harness-tools"
    component = (target_dir / "wasm32-wasip2/debug" /
                 "tobkiri_pack_harness_tools.wasm")
    shutil.copyfile(component, directory / "component.wasm")
    build.write_json(directory / "config.json", {})
    build.write_json(directory / "tools.json", inventory())
    permissions = build.permission_set(private=True)
    manifest = build.base_manifest("harness-tools", {
        "kind": "wasm", "component": "component.wasm",
        "toolchain": "rust/1.94.0;wit-bindgen/0.62.0",
        "config": {"path": "config.json"},
    }, permissions)
    manifest["ports"]["provides"] = [build.port("local.harness-agent.tool-provider")]
    manifest["shapes"] = [{"name": "local.harness-tools.artifacts",
                           "version": "1.0", "kind": "data",
                           "wit": "wit/artifacts.wit"}]
    manifest["stores"] = [{"name": "artifacts", "tier": "private",
                           "data_shape": "local.harness-tools.artifacts@1.0"}]
    build.write_json(directory / "wit/artifacts.shape.json", {
        "cardinality": "one", "streams": {}, "callbacks": {}, "conformance": None,
    })
    sources = [directory / "Cargo.toml", directory / "build.rs",
               *sorted((directory / "src").glob("*.rs")),
               directory / "wit/pack.wit", ROOT / "shapes/harness-tools.wit",
               ROOT / "shapes/harness-agent.wit", ROOT / "Cargo.lock",
               ROOT / "Cargo.toml", Path(__file__).resolve(),
               REPO / "crates/tobkiri-wit/wit/core/core.wit"]
    build.write_json(directory / "component.build.json", {
        "format": "tobkiri.component-build/1", "language": "rust",
        "tools": {"rust": "1.94.0", "wit-bindgen": "0.62.0"},
        "artifact_sha256": build.sha256(component.read_bytes()),
        "artifact_bytes": component.stat().st_size,
        "sources": {p.relative_to(REPO).as_posix(): build.sha256(p.read_bytes())
                    for p in sources}, "production_enabled": False,
    })
    return build.bind_files(directory, manifest, [
        "component.wasm", "component.build.json", "config.json", "tools.json",
        "wit/pack.wit", "wit/artifacts.wit", "wit/artifacts.shape.json",
        "wit/deps/core.wit", "wit/deps/harness-agent.wit",
    ])


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target-dir", type=Path, required=True)
    arguments = parser.parse_args()
    stage(arguments.target_dir)
