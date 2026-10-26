"""Stage the actual whole native Harness component for the isolated CI producer.

This supplies the original module closure and real dispatcher, rather than a
second probe application. Building it does not prove that native imports,
external dependencies or the original production reachpoint are connected.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys

from prepare_gateway_tool_caller import ROOT, bounded_read
from prepare_harness_setup import _publish
import prepare_harness_native as original

ADAPTER_ROOT = "tools/python313-producer/harness_native/"
RUNTIME_INTERFACE = "tobkiri-ui:harness-native/runtime@1.0.0"
TRANSPORT_INTERFACE = "tobkiri-ui:harness-native/transport@1.0.0"
BINDINGS_INTERFACE = "tobkiri-native:harness/bindings@1.0.0"
ADAPTERS = (
    "bootstrap.py", "dispatcher.py", "exception_graph.py", "native_bootstrap.py",
    "native_module_loader.py", "native_path.py", "value_graph.py",
)


def _json_bytes(value: object) -> bytes:
    return (json.dumps(value, sort_keys=True, indent=2) + "\n").encode("utf-8")


def captured() -> dict[str, bytes]:
    """Map the authenticated original package into its actual import layout."""
    previous = sys.path[:]
    sys.path.insert(0, str(ROOT / "tools/python313-producer"))
    try:
        from harness_native.source_package import assemble

        capsule = original.captured()
        package = assemble(capsule)
    finally:
        sys.path[:] = previous
    files = {}
    prefix = "source-package/tobkiri_runtime/"
    for source, content in package.items():
        if source.startswith(prefix):
            relative = source.removeprefix(prefix)
            if relative.endswith((".py", ".json")):
                files[f"python/{relative}"] = content
    for name in ADAPTERS:
        files[f"python/harness_native/{name}"] = package[
            f"source-package/{ADAPTER_ROOT}{name}"
        ]
    files["python/app.py"] = package[f"source-package/{ADAPTER_ROOT}app.py"]
    files["python/harness_native_source_index.py"] = package[
        "source-package/harness_native_source_index.py"
    ]
    for name in ("ordered_json.py", "ordered_json_v2.py"):
        files[f"python/{name}"] = bounded_read(
            ROOT / "packs/local.ai-tool-bridge-ordered-python" / name
        )
    files["wit/native.wit"] = package[f"source-package/{ADAPTER_ROOT}native.wit"]
    files["wit/deps/native-bindings/bindings.wit"] = package[
        f"source-package/{ADAPTER_ROOT}deps/native-bindings/bindings.wit"
    ]
    manifest = json.loads(package["source-package/provenance.json"])
    files["provenance.json"] = _json_bytes({
        "format": "io.tobkiri.harness-native-component-source.v1",
        "source_capsule_sha256": hashlib.sha256(capsule["provenance.json"]).hexdigest(),
        "original_source_package_sha256": hashlib.sha256(
            package["source-package/provenance.json"]
        ).hexdigest(),
        "source_index": manifest["source_index"],
        "sources": [
            {"path": name, "bytes": len(data),
             "sha256": hashlib.sha256(data).hexdigest()}
            for name, data in sorted(files.items())
        ],
        "external_runtime_dependencies": manifest["external_runtime_dependencies"],
        "external_dependencies_staged": False,
        "build_performed": False,
        "component_init_performed": False,
        "native_imports_connected": False,
        "production_enabled": False,
    })
    return files


def plan_for(files: dict[str, bytes]) -> dict:
    """Use the unchanged SDK plan and the native-only WIT world."""
    return {
        "format": "tobkiri.python-build/3",
        "stub_wasi": True,
        "wit": "wit/native.wit",
        "world": "harness-native",
        "entrypoint": "app",
        "sources": [
            {"source": name, "destination": name.removeprefix("python/"),
             "sha256": hashlib.sha256(data).hexdigest()}
            for name, data in sorted(files.items()) if name != "provenance.json"
        ],
        "outer_exports": [RUNTIME_INTERFACE, TRANSPORT_INTERFACE],
        "allowed_imports": [BINDINGS_INTERFACE],
    }


def stage(output: Path) -> dict:
    """Publish fresh bounded sources without importing the original runtime."""
    files = captured()
    plan = plan_for(files)
    if captured() != files:
        raise ValueError("native Harness sources changed during staging")
    _publish(output, {**files, "plan.json": _json_bytes(plan)})
    return plan


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    stage(parser.parse_args().output)
