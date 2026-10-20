"""Publish two inactive Python Packs, never compose, execute, or activate them.

Explicit component/receipt pins and complete current source plans establish
byte identity only. The native harness must independently verify both Packs
and prove actual Core/Gate dispatch. No runtime acceptance follows from this
materializer, a receipt, or a component header.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import stat
import tempfile
from typing import Any

import materialize_ordered_tool_bridge_v2 as bridge_pack
import replay_gateway_tool_caller as caller_reference
import stage_gateway_tool_caller as caller_stage
import stage_ordered_tool_bridge_v2 as bridge_stage
from materialize_ordered_tool_bridge import canonical, digest, no_links, require_hash

ROOT = Path(__file__).resolve().parents[2]
ORACLE = ROOT / "tests/fixtures/issue1520/gateway-tool-caller.current-source.json"
MAX_GRAPH_BYTES = 64 * 1024 * 1024
MAX_METADATA_BYTES = 4 * 1024 * 1024
MAX_OUTER_BYTES = 4 * 1024 * 1024
CALLER_ID = "local.ai-gateway-tool-caller-python"
CALLER_VERSION = "1.0.0"
CALLER_SHAPE = CALLER_ID + ".caller"
CALLER_SHAPE_VERSION = "1.0"
BRIDGE_SHAPE = bridge_pack.SHAPE + "@" + bridge_pack.SHAPE_VERSION
OPERATIONS = frozenset((
    "generate-intents", "stream-intents", "stream-event-intents",
))
REVIEWED_PINS = {
    "bridge": {
        "artifact_sha256":
            "30103a20b3694622a62e50cd57cb52af791cf0936657889811562db06ad3e092",
        "receipt_sha256":
            "01f8d3b20b5bcd46d16d1fae1265b16155237fef3ce2163c0cfa47e32c23a14d",
    },
    "caller": {
        "artifact_sha256":
            "cb561cb9b9b85aaf1de9cb74a57ffece061bd4b8100d6030b5aabc79b2cb7fc1",
        "receipt_sha256":
            "18d900c56e25e2a5ab9d746a00f06066ebbae32cba8f0a681e6009826224f477",
    },
    "source_observations_sha256":
        "854fa03de651c12cb7126d8f81e4f0c62e340a5a85f71eb50784e539e207bced",
}
CALLER_WIT_CLOSURE = {
    "caller_source_destination": "wit/pack.wit",
    "caller_source_sha256":
        "34cad3a098d41f9103386b0dab1988624c0de7f81721807b4da1e5c0f64bdd41",
    "caller_source_bytes": 731,
    "bridge_source_destination": "wit/deps/bridge-v2.wit",
    "bridge_source_sha256":
        "513454d839ac468205bc5b4ed93032625ed4f5d3064aa17f643f907e80bfeb78",
    "bridge_source_bytes": 364,
    "bridge_package_semicolon_offset": 49,
    "member": "wit/pack.wit",
    "member_sha256":
        "687f47f6c592ee1bc195d481d02efd0c051f664729e965cc0271850735ffaa04",
    "member_bytes": 1099,
}
CALLER_MEMBERS = frozenset((
    "component.wasm", "component.build.json", "config.json",
    "wit/caller.shape.json", "wit/pack.wit", "wit/deps/core.wit",
))


def _path(path: Path) -> Path:
    """Require a normalized path without ambiguous traversal or linked parents."""
    path = Path(path)
    if ".." in path.parts or any(char in str(path) for char in "\\:\0"):
        raise ValueError("evidence requires a normalized no-link path")
    path = path.absolute()
    no_links(path)
    return path


def _read(path: Path, limit: int) -> bytes:
    """Capture one bounded, unlinked regular file through no-follow descriptors."""
    path = _path(path)
    directory = descriptor = None
    try:
        directory = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        for part in path.parts[1:-1]:
            child = os.open(
                part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                dir_fd=directory,
            )
            os.close(directory)
            directory = child
        descriptor = os.open(
            path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
            dir_fd=directory,
        )
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode) or before.st_nlink != 1
            or before.st_size > limit
        ):
            raise ValueError("evidence must be a bounded, unlinked regular file")
        with os.fdopen(descriptor, "rb") as stream:
            descriptor = None
            data = stream.read(limit + 1)
            after = os.fstat(stream.fileno())
        def identity(item: os.stat_result) -> tuple[int, ...]:
            return (
                item.st_dev, item.st_ino, item.st_size, item.st_nlink,
                item.st_mtime_ns, item.st_ctime_ns,
            )
        if (
            len(data) > limit or len(data) != before.st_size
            or identity(before) != identity(after)
        ):
            raise ValueError("evidence changed during bounded capture")
        return data
    except OSError as error:
        raise ValueError(
            "evidence is missing, linked, or not a regular file",
        ) from error
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if directory is not None:
            os.close(directory)


def _validate_pins(pins: dict[str, Any]) -> None:
    if (
        type(pins) is not dict
        or set(pins) != {"bridge", "caller", "source_observations_sha256"}
        or any(
            type(pins[name]) is not dict
            or set(pins[name]) != {"artifact_sha256", "receipt_sha256"}
            for name in ("bridge", "caller")
        )
        or pins != REVIEWED_PINS
    ):
        raise ValueError("explicit pins differ from reviewed two-Pack identities")


def _component(
    artifact: Path, pins: dict[str, str], staging: Any,
) -> tuple[dict[str, bytes], dict[str, Any], dict[str, bytes]]:
    """Reconstruct and match every source-plan record without guest execution."""
    binary = _read(artifact, MAX_GRAPH_BYTES)
    receipt_bytes = _read(artifact.with_suffix(".build.json"), MAX_METADATA_BYTES)
    require_hash(binary, pins["artifact_sha256"], "component")
    require_hash(receipt_bytes, pins["receipt_sha256"], "build receipt")
    if binary[:8] != b"\0asm\x0d\0\x01\0":
        raise ValueError("artifact is not a Wasm component")
    receipt = bridge_stage.strict_json(receipt_bytes)
    if type(receipt) is not dict:
        raise ValueError("build receipt must be an object")
    before = staging.reviewed_inputs()
    with tempfile.TemporaryDirectory(prefix="caller-graph-review-") as temporary:
        stage = Path(temporary) / "stage"
        plan = staging.stage(stage)
        sources = {
            entry["destination"]: _read(stage / entry["source"], MAX_METADATA_BYTES)
            for entry in plan["sources"]
        }
        manifest = [
            {**entry, "bytes": len(sources[entry["destination"]])}
            for entry in plan["sources"]
        ]
        plan_sha = digest(_read(stage / "plan.json", MAX_METADATA_BYTES))
    expected = {
        "format": "tobkiri.component-build/2", "language": "python",
        "world": plan["world"], "artifact_sha256": digest(binary),
        "artifact_bytes": len(binary), "build_plan_sha256": plan_sha,
        "source_manifest": manifest,
        "source_manifest_sha256": digest(canonical(manifest)),
        "imports": sorted(plan["allowed_imports"]),
        "exports": sorted(plan["outer_exports"]),
    }
    if (
        type(receipt.get("artifact_bytes")) is not int
        or any(receipt.get(key) != value for key, value in expected.items())
    ):
        raise ValueError("build receipt differs from complete current source plan")
    if staging.reviewed_inputs() != before:
        raise ValueError("reviewed sources changed during evidence validation")
    return (
        {"component.wasm": binary, "component.build.json": receipt_bytes,
         "config.json": b"{}"},
        receipt,
        sources,
    )


def package_caller_wit(caller: bytes, bridge: bytes) -> bytes:
    """Package exact sources reversibly, without changing any ABI declaration.

    Production parses each document before merging packages, so splitting the
    caller interface and its world into different files is invalid. Keep that
    entire document unchanged and nest the canonical bridge package once.
    This is a fixed reviewed closure, not a general-purpose WIT rewriter.
    """
    binding = CALLER_WIT_CLOSURE
    for name, source in (("caller", caller), ("bridge", bridge)):
        require_hash(source, binding[f"{name}_source_sha256"], f"{name} WIT")
        if len(source) != binding[f"{name}_source_bytes"]:
            raise ValueError(f"{name} WIT length differs")
    offset = binding["bridge_package_semicolon_offset"]
    if bridge[offset:offset + 1] != b";":
        raise ValueError("canonical bridge package delimiter differs")
    combined = (
        caller + b"\n" + bridge[:offset] + b" {" + bridge[offset + 1:] + b"}\n"
    )
    require_hash(combined, binding["member_sha256"], "caller WIT closure")
    if len(combined) != binding["member_bytes"]:
        raise ValueError("caller WIT closure length differs")
    start = len(caller) + 1
    reconstructed = (
        combined[start:start + offset] + b";" + combined[start + offset + 2:-2]
    )
    if combined[:len(caller)] != caller or reconstructed != bridge:
        raise ValueError("caller WIT closure does not reconstruct exact sources")
    return combined


def envelope_bounds(observations: dict[str, Any]) -> dict[str, int]:
    """Bound native caller argument/result envelopes, keeping inner JSON opaque."""
    requests, responses = [], []
    for case in observations["cases"]:
        if (
            type(case.get("operation")) is not str
            or case["operation"] not in OPERATIONS
            or type(case.get("input_json")) is not str
        ):
            raise ValueError("invalid caller operation or outer-envelope payload")
        requests.append(len(canonical({"payload": case["input_json"]})))
        kind = case.get("classification")
        if kind == "success":
            if type(case.get("output_json")) is not str:
                raise ValueError("outer-envelope success payload must be a string")
            responses.append(len(canonical(case["output_json"])))
        elif kind not in {"provider-invalid", "provider-nonfinite"}:
            raise ValueError("unknown caller source classification")
    if any(size > MAX_OUTER_BYTES for size in requests + responses):
        raise ValueError("caller case exceeds native outer-envelope budget")
    return {
        "core_strict_v1_max_bytes": MAX_OUTER_BYTES,
        "max_request_bytes": max(requests, default=0),
        "max_success_response_bytes": max(responses, default=0),
    }


def _caller_manifest(files: dict[str, bytes]) -> dict[str, Any]:
    if set(files) != CALLER_MEMBERS:
        raise ValueError("caller Pack requires the exact six-member WIT closure")
    manifest = bridge_pack._manifest(files)
    manifest.update(id=CALLER_ID, version=CALLER_VERSION)
    manifest["shapes"] = [{
        "name": CALLER_SHAPE, "version": CALLER_SHAPE_VERSION,
        "kind": "port", "wit": CALLER_WIT_CLOSURE["member"],
    }]
    manifest["ports"] = {
        "provides": [{
            "shape": CALLER_SHAPE, "version": CALLER_SHAPE_VERSION,
            "optional": False,
        }],
        "consumes": [{
            "shape": bridge_pack.SHAPE, "version": bridge_pack.SHAPE_VERSION,
            "optional": False,
        }],
        "untyped": [],
    }
    manifest["permissions"]["ports"] = [BRIDGE_SHAPE]
    return manifest


def materialize(
    bridge: Path, caller: Path, oracle: Path, destination: Path,
    *, pins: dict[str, Any],
) -> dict[str, Any]:
    """Publish a fresh, bounded, inactive two-Pack graph with no code execution.

    The existing destination parent must remain trusted and stable. Publication
    uses the existing Linux/macOS atomic no-replace primitive; failures clean up
    only the private temporary directory. No Profile or runtime is created.
    """
    _validate_pins(pins)
    destination = _path(destination)
    if destination.exists() or not destination.parent.is_dir():
        raise ValueError("destination must be fresh with an existing trusted parent")
    bridge, caller, oracle = map(_path, (bridge, caller, oracle))
    source_bytes = _read(oracle, MAX_METADATA_BYTES)
    require_hash(
        source_bytes, pins["source_observations_sha256"], "source observations",
    )
    bridge_files, bridge_receipt, bridge_sources = _component(
        bridge, pins["bridge"], bridge_stage,
    )
    caller_files, caller_receipt, caller_sources = _component(
        caller, pins["caller"], caller_stage,
    )
    # Validate captured bytes, not a mutable external oracle path.
    with tempfile.TemporaryDirectory(prefix="caller-graph-oracle-") as temporary:
        captured = Path(temporary) / "source.json"
        with captured.open("xb") as stream:
            stream.write(source_bytes)
        observed, observations = caller_reference.validate_observations(
            captured, pins["source_observations_sha256"],
        )
    if observed != source_bytes:
        raise ValueError("source observations changed during validation")
    bounds = envelope_bounds(observations)
    if (
        caller_sources["wit/deps/bridge-v2.wit"] != bridge_sources["wit/pack-v2.wit"]
        or caller_sources["wit/deps/core.wit"] != bridge_sources["wit/deps/core.wit"]
    ):
        raise ValueError("separate Packs disagree on exact canonical dependencies")
    sidecar = canonical({
        "cardinality": "one", "streams": {}, "callbacks": {}, "conformance": None,
    })
    bridge_files.update({
        "wit/pack-v2.wit": bridge_sources["wit/pack-v2.wit"],
        "wit/deps/core.wit": bridge_sources["wit/deps/core.wit"],
        "wit/bridge.shape.json": sidecar,
    })
    wit = package_caller_wit(
        caller_sources["wit/pack.wit"], caller_sources["wit/deps/bridge-v2.wit"],
    )
    caller_files.update({
        "wit/pack.wit": wit,
        "wit/deps/core.wit": caller_sources["wit/deps/core.wit"],
        "wit/caller.shape.json": sidecar,
    })
    manifests = {
        "bridge": bridge_pack._manifest(bridge_files),
        "caller": _caller_manifest(caller_files),
    }
    members: dict[str, bytes] = {"source-observations.json": source_bytes}
    components = {}
    for name, files, receipt in (
        ("bridge", bridge_files, bridge_receipt),
        ("caller", caller_files, caller_receipt),
    ):
        manifest = manifests[name]
        manifest_bytes = canonical(manifest)
        members.update({f"{name}/{key}": value for key, value in files.items()})
        members[f"{name}/pack.json"] = manifest_bytes
        shape_definition = manifest["shapes"][0]
        components[name] = {
            "directory": name, "pack_id": manifest["id"],
            "pack_version": manifest["version"],
            "shape": shape_definition["name"] + "@" + shape_definition["version"],
            "pack_sha256": digest(manifest_bytes),
            "artifact_sha256": digest(files["component.wasm"]),
            "artifact_bytes": len(files["component.wasm"]),
            "receipt_sha256": digest(files["component.build.json"]),
            "build_plan_sha256": receipt["build_plan_sha256"],
            "source_manifest_sha256": receipt["source_manifest_sha256"],
        }
    index = {
        "format": "tobkiri.gateway-tool-caller-native-inputs/1",
        "protocol": "ordered-finite-json-v2",
        "scope": "two-separate-python-packs-not-finalizer",
        "components": components,
        "source_observations": {
            "path": "source-observations.json", "sha256": digest(source_bytes),
            "bytes": len(source_bytes),
        },
        "component_source_bytes": sum(
            component["artifact_bytes"] for component in components.values()
        ),
        "core_component_catalog_limit_bytes": MAX_GRAPH_BYTES,
        "caller_wit_closure": dict(CALLER_WIT_CLOSURE),
        "outer_budget": bounds,
        "build_performed": False, "wasm_execution_performed": False,
        "native_core_acceptance": False, "production_enabled": False,
    }
    index_bytes = canonical(index)
    members["index.json"] = index_bytes
    if sum(map(len, members.values())) > MAX_GRAPH_BYTES:
        raise ValueError("two-Pack graph exceeds unchanged 64 MiB catalog bound")
    with tempfile.TemporaryDirectory(
        prefix=".gateway-caller-graph-", dir=destination.parent,
    ) as temporary:
        partial = Path(temporary)
        descriptor = os.open(partial, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            for name, data in sorted(members.items()):
                caller_stage._write(descriptor, name, data)
        finally:
            os.close(descriptor)
        no_links(destination)
        if destination.exists():
            raise ValueError("destination appeared before publication")
        bridge_pack._publish_no_replace(partial, destination)
    return {**index, "index_sha256": digest(index_bytes)}


def main() -> None:
    """Validate explicit immutable input pins and publish inactive Packs only."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bridge", type=Path, required=True)
    parser.add_argument("--caller", type=Path, required=True)
    parser.add_argument("--oracle", type=Path, default=ORACLE)
    parser.add_argument("--pins", type=Path, required=True)
    parser.add_argument("--pins-sha256", required=True)
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args()
    raw = _read(arguments.pins, 16 * 1024)
    require_hash(raw, arguments.pins_sha256, "input pins")
    pins = bridge_stage.strict_json(raw)
    report = materialize(
        arguments.bridge, arguments.caller, arguments.oracle, arguments.output,
        pins=pins,
    )
    print(json.dumps(report, sort_keys=True))


if __name__ == "__main__":
    main()
