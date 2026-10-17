"""Build/stage the ordinary Rust/Wasm DeepThink Pack, with real byte hashes.

This is a development build utility, never a Python runtime or Host adapter.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import shutil
import subprocess

from build import (
    base_manifest, bind_files, component_artifact, permission_set, port, sha256, write_json,
)


ROOT = Path(__file__).resolve().parent
SOURCE = ROOT / "local.deepthink"


def default_config() -> dict:
    """Declare only known capabilities; inventory is supplied by its owner."""
    return {
        "max_review_rounds": 2,
        "max_tool_rounds": 3,
        "max_contract_retries": 2,
        "structured_provider": True,
        "tools": [], "skills": [], "profile_checks": [],
        "environment": {
            "execution_mode": "verified_wasm", "approval_policy": "core_gate",
            "output_token_limit": "provider_default",
            "artifact_scope": "logical tool-owned private storage; no physical workspace",
        },
        "capability_gaps": [
            "Physical filesystem, shell, PTY, 3D editing and Computer use are not implemented by this Profile.",
            "No skill catalog or Profile additional workflow checks were supplied.",
        ],
    }


def stage(target_dir: Path, config: dict | None = None,
          destination: Path | None = None, build_profile: str = "debug") -> dict:
    """Stage a compiled component and bind its WIT/config/overlay provenance."""
    directory = destination or SOURCE
    artifact = component_artifact(target_dir, "tobkiri_pack_deepthink.wasm", build_profile)
    directory.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(artifact, directory / "component.wasm")
    for relative in ["wit/pack.wit", "wit/deps/core.wit", "wit/deps/deepthink.wit",
                     "wit/deps/harness-chat.wit", "wit/deps/harness-agent.wit",
                     "overlays/generate.overlay.json", "flows/continuation.flow.json"]:
        destination_file = directory / relative
        destination_file.parent.mkdir(parents=True, exist_ok=True)
        source = SOURCE / relative
        if source.resolve() != destination_file.resolve():
            shutil.copyfile(source, destination_file)
    write_json(directory / "config.json", default_config() if config is None else config)
    write_json(directory / "wit/workflow.shape.json", {
        "cardinality": "one", "streams": {"run": "chat-event", "continue-run": "chat-event"},
        "callbacks": {}, "conformance": None,
    })
    write_json(directory / "wit/records.shape.json", {
        "cardinality": "one", "streams": {}, "callbacks": {}, "conformance": None,
    })
    write_json(directory / "wit/continuation-input.shape.json", {
        "cardinality": "one", "streams": {"submit": "flow-frame"},
        "callbacks": {}, "conformance": None,
    })
    write_json(directory / "wit/continuation-flow.shape.json", {
        "cardinality": "one", "streams": {}, "callbacks": {}, "conformance": None,
    })
    views = ["local.harness-chat.llm-provider@1.0",
             "local.harness-chat.structured-llm-provider@1.0",
             "local.harness-agent.tool-provider@1.0",
             "local.harness-chat.assistant-output@1.0",
             "local.harness-chat.generate@1.0", "local.deepthink.workflow@1.0",
             "local.harness-chat.conversation-store@1.0",
             "local.deepthink.continuation-flow@1.0", "local.deepthink.continuation-input@1.0"]
    manifest = base_manifest("deepthink", {
        "kind": "wasm", "component": "component.wasm",
        "toolchain": "rust/1.94.0;wit-bindgen/0.62.0",
        "config": {"path": "config.json"},
    }, permission_set(ports=views, private=True))
    manifest["ports"]["provides"] = [port("local.deepthink.workflow"), port("local.deepthink.continuation-input")]
    manifest["ports"]["consumes"] = [
        port("local.harness-chat.llm-provider"),
        port("local.harness-chat.structured-llm-provider", optional=True),
        port("local.harness-agent.tool-provider", optional=True),
        port("local.harness-chat.assistant-output"),
        port("local.deepthink.workflow"),
        port("local.deepthink.continuation-input"),
        port("local.deepthink.continuation-flow"),
        port("local.harness-chat.conversation-store"),
    ]
    manifest["shapes"] = [{"name": "local.deepthink." + name, "version": "1.0",
        "kind": kind, "wit": "wit/deps/deepthink.wit"}
        for name, kind in [("workflow", "port"), ("records", "data"),
                           ("continuation-input", "port"), ("continuation-flow", "flow")]]
    manifest["flows"] = ["flows/continuation.flow.json"]
    manifest["overlays"] = ["overlays/generate.overlay.json"]
    manifest["stores"] = [{"name": "workflows", "tier": "private",
                           "data_shape": "local.deepthink.records@1.0"}]
    paths = ["component.wasm", "config.json", "wit/pack.wit", "wit/deps/core.wit",
             "wit/deps/deepthink.wit", "wit/workflow.shape.json", "wit/records.shape.json",
             "wit/deps/harness-chat.wit", "wit/deps/harness-agent.wit",
             "wit/continuation-input.shape.json", "wit/continuation-flow.shape.json",
             "flows/continuation.flow.json", "overlays/generate.overlay.json", "component.build.json"]
    sources = [*sorted((SOURCE / "src").glob("*.rs")), SOURCE / "build.rs",
               SOURCE / "Cargo.toml", ROOT / "Cargo.toml", ROOT / "Cargo.lock",
               SOURCE / "wit/pack.wit", ROOT / "shapes/deepthink.wit",
               ROOT / "shapes/harness-chat.wit", ROOT / "shapes/harness-agent.wit",
               ROOT.parent / "crates/tobkiri-wit/wit/core/core.wit"]
    write_json(directory / "component.build.json", {
        "format": "tobkiri.component-build/1", "language": "rust",
        "tools": {"rust": "1.94.0", "wit-bindgen": "0.62.0"},
        "build_profile": build_profile,
        "artifact_sha256": sha256(artifact.read_bytes()),
        "artifact_bytes": artifact.stat().st_size,
        "sources": {source.relative_to(ROOT.parent).as_posix(): sha256(source.read_bytes())
                    for source in sources},
        "production_enabled": False,
    })
    return bind_files(directory, manifest, paths)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target-dir", type=Path, required=True)
    parser.add_argument("--skip-build", action="store_true")
    parser.add_argument("--config", type=Path)
    parser.add_argument("--build-profile", choices=("debug", "release"), default="debug")
    args = parser.parse_args()
    if not args.skip_build:
        command = ["cargo", "build", "--manifest-path", str(ROOT / "Cargo.toml"),
                   "--target", "wasm32-wasip2", "-p", "tobkiri-pack-deepthink",
                   "--target-dir", str(args.target_dir)]
        if args.build_profile == "release":
            command.append("--release")
        subprocess.run(command, check=True)
    config = json.loads(args.config.read_text(encoding="utf-8")) if args.config else None
    stage(args.target_dir, config, build_profile=args.build_profile)


if __name__ == "__main__":
    main()
