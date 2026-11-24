"""Stage an inactive source-only CPython captured-input stack probe."""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
from pathlib import Path
from typing import Any

from prepare_gateway_tool_caller import ROOT, bounded_read
from prepare_harness_setup import _publish

PRODUCER = "tools/python313-producer/harness_input_stack"
PROBE_INTERFACE = "tobkiri-ui:native-input-stack/probe@1.0.0"
CAPTURED_INTERFACE = "tobkiri-ui:inputs/captured@1.0.0"
NATIVE_INPUT_WIT = "crates/tobkiri-wasm/wit/native-inputs.wit"
ORDERED_CODEC = "packs/local.ai-tool-bridge-ordered-python"
PROTOCOL = "tobkiri_runtime/tobkiri_protocol"

SOURCES = {
    "python/app.py": f"{PRODUCER}/app.py",
    "python/logic.py": f"{PRODUCER}/logic.py",
    "python/ordered_json.py": f"{ORDERED_CODEC}/ordered_json.py",
    "python/ordered_json_v2.py": f"{ORDERED_CODEC}/ordered_json_v2.py",
    **{
        f"python/tobkiri_protocol/{name}.py": f"{PROTOCOL}/{name}.py"
        for name in (
            "__init__", "canonical", "errors", "ids", "captured_inputs",
            "platform_artifact",
        )
    },
    "wit/probe.wit": f"{PRODUCER}/probe.wit",
    "wit/deps/captured-inputs/native-inputs.wit": NATIVE_INPUT_WIT,
}

FUNCTIONS = {
    f"{PROTOCOL}/platform_artifact.py": (
        "artifact_digest",
        "_regular_binary_reader",
        "_stable_update_digest",
        "_read_bounded_header",
        "verify_platform_artifact",
        "_verify_binary_architecture",
    ),
    f"{PROTOCOL}/captured_inputs.py": (
        "CapturedInputs.__init__",
        "CapturedInputs.root",
        "CapturedPath.open_binary",
        "CapturedReader.read",
    ),
}


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _json_bytes(value: Any) -> bytes:
    return (json.dumps(value, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, allow_nan=False, sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _function_records(sources: dict[str, bytes]) -> list[dict[str, Any]]:
    records = []
    for path, names in FUNCTIONS.items():
        source = sources[path]
        tree = ast.parse(source, filename=path)
        for qualified_name in names:
            parts = qualified_name.split(".")
            parent: ast.AST = tree
            for index, part in enumerate(parts):
                nodes = parent.body if hasattr(parent, "body") else ()
                expected_type = ast.ClassDef if index < len(parts) - 1 else (
                    ast.FunctionDef, ast.AsyncFunctionDef,
                )
                matches = [node for node in nodes
                           if isinstance(node, expected_type) and node.name == part]
                if len(matches) != 1:
                    raise ValueError(
                        f"one original function is required: {path}:{qualified_name}"
                    )
                parent = matches[0]
            node = parent
            start = node.lineno
            if node.decorator_list:
                start = min(start, *(item.lineno for item in node.decorator_list))
            lines = source.splitlines(keepends=True)
            begin = sum(len(line) for line in lines[:start - 1])
            finish = sum(len(line) for line in lines[:node.end_lineno])
            body = source[begin:finish]
            records.append({
                "source_path": path,
                "symbol": qualified_name,
                "source_start_line": start,
                "source_end_line": node.end_lineno,
                "source_start_byte": begin,
                "source_end_byte": finish,
                "sha256": _sha256(body),
                "bytes": len(body),
                "complete_definition": True,
            })
    return records


def captured() -> dict[str, bytes]:
    """Return original source DATA, dependency WIT, and provenance bytes."""
    sources = {path: bounded_read(ROOT / path) for path in sorted(set(SOURCES.values()))}
    imported_wit = sources[NATIVE_INPUT_WIT]
    files = {destination: sources[source]
             for destination, source in SOURCES.items()}
    source_records = [
        {
            "source_path": source,
            "staged_as": destination,
            "sha256": _sha256(sources[source]),
            "bytes": len(sources[source]),
        }
        for destination, source in sorted(SOURCES.items())
    ]
    if files["wit/probe.wit"] == files["wit/deps/captured-inputs/native-inputs.wit"]:
        raise ValueError("probe WIT must remain distinct from imported captured-input WIT")
    if files["wit/deps/captured-inputs/native-inputs.wit"] != imported_wit:
        raise ValueError("captured-input WIT copy differs from original bytes")
    provenance: dict[str, Any] = {
        "format": "io.tobkiri.harness-input-stack-source/1",
        "generator": "scripts/wasm/prepare_harness_input_stack.py",
        "scope": "source capsule only: independent non-production captured-input verifier probe",
        "source_capsule_only": True,
        "source_capsule_executable": False,
        "sources": source_records,
        "original_function_bodies": _function_records(sources),
        "outer_exports": [PROBE_INTERFACE],
        "allowed_imports": [CAPTURED_INTERFACE],
        "build_performed": False,
        "wasm_component_built": False,
        "wasm_execution_performed": False,
        "production_enabled": False,
        "native_os_proof_performed": False,
        "actual_os_owner_proof": False,
        "whole_setup_performed": False,
        "whole_setup_compatible": False,
        "sync_stack_async_wait_proven": False,
        "native_authority_created": False,
        "method_rewrites": [],
    }
    provenance["metadata_sha256"] = _sha256(_canonical_json_bytes(provenance))
    files["provenance.json"] = _json_bytes(provenance)
    return files


def plan_for(files: dict[str, bytes]) -> dict[str, Any]:
    sources = [
        {
            "source": name,
            "destination": name.removeprefix("python/"),
            "sha256": _sha256(content),
        }
        for name, content in sorted(files.items())
        if name != "provenance.json"
    ]
    return {
        "format": "tobkiri.python-build/3",
        "stub_wasi": True,
        "wit": "wit/probe.wit",
        "world": "native-input-stack",
        "entrypoint": "app",
        "sources": sources,
        "outer_exports": [PROBE_INTERFACE],
        "allowed_imports": [CAPTURED_INTERFACE],
    }


def stage(output: Path) -> dict[str, Any]:
    """Publish a fresh hash-bound plan-3 input stage; never build or run it."""
    files = captured()
    plan = plan_for(files)
    if captured() != files:
        raise ValueError("native input stack source changed during staging")
    staged = dict(files)
    staged["plan.json"] = _json_bytes(plan)
    _publish(Path(output), staged)
    return plan


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    plan = stage(args.output)
    print(json.dumps({
        "source_stage_created": True,
        "format": plan["format"],
        "build_performed": False,
        "wasm_execution_performed": False,
        "production_enabled": False,
        "native_os_proof_performed": False,
        "whole_setup_performed": False,
    }, sort_keys=True))


if __name__ == "__main__":
    main()
