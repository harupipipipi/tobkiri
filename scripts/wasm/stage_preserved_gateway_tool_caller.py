"""Authenticate preserved components and publish an inactive two-Pack CI input.

This is byte transport and current-source validation only. It does not execute
the Python source oracle, run a producer, instantiate Wasm, or enable a Profile.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import tempfile
from typing import Any

import materialize_gateway_tool_caller_graph as graph
from stage_preserved_ordered_python import decompress_verified

FIXTURE = graph.ROOT / "crates/tobkiri-e2e/fixtures/gateway-tool-caller"
GRAPH_INDEX_SHA256 = (
    "0441bde565b5e1765a8fdc262e61db00a5fff0c160624d99d5d841f65c737d96"
)
SOURCE_OBSERVATIONS_SHA256 = (
    "854fa03de651c12cb7126d8f81e4f0c62e340a5a85f71eb50784e539e207bced"
)
FIXTURE_COMPONENTS = {
    "bridge": {
        "compressed_sha256":
            "f2ae05cd09161fb8ad65c5fcf4f168852b7ea33895ec32578244aa47c6db236f",
        "compressed_bytes": 3957460,
        "artifact_sha256":
            "30103a20b3694622a62e50cd57cb52af791cf0936657889811562db06ad3e092",
        "artifact_bytes": 17910172,
        "receipt_sha256":
            "01f8d3b20b5bcd46d16d1fae1265b16155237fef3ce2163c0cfa47e32c23a14d",
        "receipt_bytes": 8535,
    },
    "caller": {
        "compressed_sha256":
            "8f6b6769d7fe0e05a17288dc38b1e7e648df11b3c889e4441192cd82d79dc3ea",
        "compressed_bytes": 3937708,
        "artifact_sha256":
            "cb561cb9b9b85aaf1de9cb74a57ffece061bd4b8100d6030b5aabc79b2cb7fc1",
        "artifact_bytes": 17888679,
        "receipt_sha256":
            "18d900c56e25e2a5ab9d746a00f06066ebbae32cba8f0a681e6009826224f477",
        "receipt_bytes": 8451,
    },
}
FALSE_FLAGS = (
    "build_performed", "wasm_execution_performed",
    "native_core_acceptance", "production_enabled",
)


def _component(name: str) -> tuple[bytes, bytes]:
    """Capture pinned regular files before bounded single-stream decompression."""
    pin = FIXTURE_COMPONENTS[name]
    compressed = graph._read(
        FIXTURE / name / "component.wasm.xz", pin["compressed_bytes"],
    )
    receipt = graph._read(
        FIXTURE / name / "component.build.json", pin["receipt_bytes"],
    )
    if (
        len(compressed) != pin["compressed_bytes"]
        or len(receipt) != pin["receipt_bytes"]
    ):
        raise ValueError("preserved fixture length differs")
    graph.require_hash(receipt, pin["receipt_sha256"], "preserved build receipt")
    binary = decompress_verified(
        compressed, compressed_sha=pin["compressed_sha256"],
        artifact_sha=pin["artifact_sha256"], artifact_bytes=pin["artifact_bytes"],
    )
    return binary, receipt


def stage_preserved(destination: Path) -> dict[str, Any]:
    """Publish exact private graph/ and metadata, or leave no partial output.

    The existing destination parent must remain trusted and stable. All input
    captures reject links and nonregular files. Both temporary directories are
    private; publication uses atomic no-replace with no clobbering fallback.
    The existing materializer rechecks frozen observations and complete current
    source plans, without rerunning source observations or guest code.
    """
    destination = graph._path(destination)
    if destination.exists() or not destination.parent.is_dir():
        raise ValueError("destination must be fresh with an existing trusted parent")
    pins = {
        name: {
            key: FIXTURE_COMPONENTS[name][key]
            for key in ("artifact_sha256", "receipt_sha256")
        }
        for name in ("bridge", "caller")
    }
    pins["source_observations_sha256"] = SOURCE_OBSERVATIONS_SHA256
    graph._validate_pins(pins)
    with tempfile.TemporaryDirectory(
        prefix=".preserved-gateway-caller-", dir=destination.parent,
    ) as temporary:
        partial = Path(temporary)
        with tempfile.TemporaryDirectory(
            prefix="preserved-gateway-inputs-", dir=partial,
        ) as input_temporary:
            inputs = Path(input_temporary)
            descriptor = os.open(
                inputs, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
            )
            try:
                for name in ("bridge", "caller"):
                    binary, receipt = _component(name)
                    graph.caller_stage._write(
                        descriptor, f"{name}/component.wasm", binary,
                    )
                    graph.caller_stage._write(
                        descriptor, f"{name}/component.build.json", receipt,
                    )
            finally:
                os.close(descriptor)
            result = graph.materialize(
                inputs / "bridge/component.wasm", inputs / "caller/component.wasm",
                graph.ORACLE, partial / "graph", pins=pins,
            )
        graph.require_hash(
            graph._read(partial / "graph/index.json", 64 * 1024),
            GRAPH_INDEX_SHA256, "preserved graph index",
        )
        if (
            result.get("index_sha256") != GRAPH_INDEX_SHA256
            or any(result.get(flag) is not False for flag in FALSE_FLAGS)
        ):
            raise ValueError("preserved graph identity or inactive scope differs")
        report = {
            "format": "tobkiri.preserved-gateway-tool-caller/1",
            "graph_directory": "graph",
            "graph_index_sha256": GRAPH_INDEX_SHA256,
            "fixture_components": FIXTURE_COMPONENTS,
            "source_observations_sha256": SOURCE_OBSERVATIONS_SHA256,
            **dict.fromkeys(FALSE_FLAGS, False),
        }
        descriptor = os.open(
            partial, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
        )
        try:
            graph.caller_stage._write(
                descriptor, "materialization.json", graph.canonical(report),
            )
        finally:
            os.close(descriptor)
        graph.no_links(destination)
        if destination.exists():
            raise ValueError("destination appeared before publication")
        graph.bridge_pack._publish_no_replace(partial, destination)
    return report


def main() -> None:
    """Prepare only the exact reviewed inactive two-Pack native test inputs."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args()
    print(json.dumps(stage_preserved(arguments.output), sort_keys=True))


if __name__ == "__main__":
    main()
