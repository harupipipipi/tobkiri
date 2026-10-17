"""Stage the ordinary DeepThink input adapter; this is not a runtime host.

The defining DeepThink Pack owns the provided interface. This Pack only owns
its implementation and has no Store, network, secret or native authority.
"""
from __future__ import annotations

import argparse
from pathlib import Path
import shutil

import build

ROOT = Path(__file__).resolve().parent
SOURCE = ROOT / "local.deepthink-input"
PACK_ID = "local.deepthink-input"
INPUT_VIEW = "local.deepthink.continuation-input@1.0"
WORKFLOW_TYPE_VIEW = "local.deepthink.workflow@1.0"
FLOW_VIEWS = (
    "local.deepthink.control-flow@1.0",
    "local.deepthink.continuation-flow@1.0",
    "local.deepthink.regeneration-flow@1.0",
)
COMPONENT_HEADER = b"\0asm\r\0\1\0"


def permissions() -> dict:
    """Declare only the existing input view and three ordinary Flow targets."""
    return build.permission_set(ports=[INPUT_VIEW, *FLOW_VIEWS])


def consumed_ports() -> list[dict]:
    """Declare Flow targets and the owner of public record aliases.

    continuation-input imports workflow's chat-event/recovery-selection types.
    That interface also has operations, so it must be a declared WIT dependency.
    The required provider already implements the selected worker's Flows. This
    declaration does not grant permission to call its workflow operations.
    """
    return [build.port(view.rsplit("@", 1)[0])
            for view in (*FLOW_VIEWS, WORKFLOW_TYPE_VIEW)]


def stage(target_dir: Path, destination: Path | None = None,
          build_profile: str = "debug") -> dict:
    """Bind the explicitly selected compiled component and its nominal WIT."""
    artifact = build.component_artifact(
        target_dir, "tobkiri_pack_deepthink_input.wasm", build_profile,
    )
    with artifact.open("rb") as stream:
        if stream.read(8) != COMPONENT_HEADER:
            raise ValueError("input artifact does not have a Wasm component header")
    directory = destination or SOURCE
    directory.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(artifact, directory / "component.wasm")
    copies = {
        "wit/pack.wit": SOURCE / "wit/pack.wit",
        "wit/deps/core.wit": ROOT.parent / "crates/tobkiri-wit/wit/core/core.wit",
        "wit/deps/deepthink.wit": ROOT / "shapes/deepthink.wit",
    }
    for relative, source in copies.items():
        target = directory / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        if source.resolve() != target.resolve():
            shutil.copyfile(source, target)
    build.write_json(directory / "config.json", {})
    manifest = build.base_manifest("deepthink-input", {
        "kind": "wasm", "component": "component.wasm",
        "toolchain": "rust/1.94.0;wit-bindgen/0.62.0",
        "config": {"path": "config.json"},
    }, permissions())
    manifest["ports"]["provides"] = [build.port(INPUT_VIEW.rsplit("@", 1)[0])]
    manifest["ports"]["consumes"] = consumed_ports()
    # No foreign shape is redeclared here. The selected DeepThink artifact is
    # the authoritative owner of continuation-input and its stream sidecar.
    sources = [*sorted((SOURCE / "src").glob("*.rs")), SOURCE / "build.rs",
               SOURCE / "Cargo.toml", SOURCE / "wit/pack.wit",
               ROOT / "Cargo.toml", ROOT / "Cargo.lock", ROOT / "build.py",
               Path(__file__), *copies.values()]
    build.write_json(directory / "component.build.json", {
        "format": "tobkiri.component-build/1", "language": "rust",
        "tools": {"rust": "1.94.0", "wit-bindgen": "0.62.0"},
        "build_profile": build_profile,
        "artifact_sha256": build.sha256(artifact.read_bytes()),
        "artifact_bytes": artifact.stat().st_size,
        "sources": {source.relative_to(ROOT.parent).as_posix():
                    build.sha256(source.read_bytes()) for source in sources},
        "production_enabled": False,
    })
    return build.bind_files(directory, manifest, [
        "component.wasm", "config.json", *copies, "component.build.json",
    ])


def main() -> None:
    """Stage an already built adapter without invoking Cargo or Core."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target-dir", type=Path, required=True)
    parser.add_argument("--destination", type=Path)
    parser.add_argument("--build-profile", choices=("debug", "release"), default="debug")
    args = parser.parse_args()
    stage(args.target_dir, args.destination, args.build_profile)


if __name__ == "__main__":
    main()
