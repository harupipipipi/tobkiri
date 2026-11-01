"""Stage a separate, inactive import-audit diagnostic for the input-stack probe."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any

from prepare_gateway_tool_caller import ROOT, bounded_read
import prepare_harness_input_stack as baseline_stager
from prepare_harness_setup import _publish


if (
    Path(baseline_stager.__file__).resolve()
    != ROOT / "scripts/wasm/prepare_harness_input_stack.py"
    or baseline_stager.ROOT != ROOT
):
    raise ValueError("original input-stack stager belongs to another repository")


PRODUCER = "tools/python313-producer/harness_input_stack_import_diagnostics"
PROBE_INTERFACE = "tobkiri-ui:native-input-stack/probe@1.0.0"
CAPTURED_INTERFACE = "tobkiri-ui:inputs/captured@1.0.0"
IMPORT_TRACE_INTERFACE = "tobkiri-ui:diagnostics/import-trace@1.0.0"
WORLD = "native-input-stack-import-diagnostics"
_BASELINE_APP = "python/app.py"
_STAGED_BASELINE_APP = "python/baseline_app.py"
_PROBE_WIT = "wit/probe.wit"
_IMPORT_TRACE_WIT = "wit/deps/diagnostics/import-trace.wit"
_EXPECTED_FILES = (
    set(baseline_stager.SOURCES)
    - {_BASELINE_APP, _PROBE_WIT}
) | {
    _BASELINE_APP,
    _STAGED_BASELINE_APP,
    _PROBE_WIT,
    _IMPORT_TRACE_WIT,
}


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _json_bytes(value: Any) -> bytes:
    return (json.dumps(value, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _source_records(
    files: dict[str, bytes], baseline_provenance: dict[str, Any]
) -> list[dict[str, Any]]:
    baseline_sources = {
        item["staged_as"]: item["source_path"]
        for item in baseline_provenance["sources"]
    }
    records = []
    for name, content in sorted(files.items()):
        if name == "provenance.json":
            continue
        if name == _STAGED_BASELINE_APP:
            source_path = baseline_sources[_BASELINE_APP]
        elif name == _BASELINE_APP:
            source_path = f"{PRODUCER}/app.py"
        elif name == _PROBE_WIT:
            source_path = f"{PRODUCER}/probe.wit"
        elif name == _IMPORT_TRACE_WIT:
            source_path = f"{PRODUCER}/import-trace.wit"
        elif name in baseline_sources:
            source_path = baseline_sources[name]
        else:
            raise ValueError("diagnostic source has no pinned source path")
        records.append(
            {
                "source_path": source_path,
                "staged_as": name,
                "sha256": _sha256(content),
                "bytes": len(content),
            }
        )
    return records


def captured() -> dict[str, bytes]:
    """Capture the original source capsule and replace only its app and world."""
    baseline_files = baseline_stager.captured()
    baseline_provenance_raw = baseline_files["provenance.json"]
    baseline_provenance = json.loads(baseline_provenance_raw)
    if baseline_provenance.get("format") != "io.tobkiri.harness-input-stack-source/1":
        raise ValueError("original input-stack source receipt is unsupported")
    baseline_provenance_copy = dict(baseline_provenance)
    baseline_metadata_sha256 = baseline_provenance_copy.pop("metadata_sha256", None)
    if baseline_metadata_sha256 != _sha256(
        _canonical_json_bytes(baseline_provenance_copy)
    ):
        raise ValueError("original input-stack source receipt changed")

    baseline_app = baseline_files[_BASELINE_APP]
    files = {
        (_STAGED_BASELINE_APP if name == _BASELINE_APP else name): content
        for name, content in baseline_files.items()
        if name not in {"provenance.json", _PROBE_WIT}
    }
    files["python/app.py"] = bounded_read(ROOT / f"{PRODUCER}/app.py")
    files[_PROBE_WIT] = bounded_read(ROOT / f"{PRODUCER}/probe.wit")
    files[_IMPORT_TRACE_WIT] = bounded_read(ROOT / f"{PRODUCER}/import-trace.wit")

    if set(files) != _EXPECTED_FILES:
        raise ValueError("diagnostic source capsule inventory changed")

    baseline_records = [
        {
            "source_path": item["source_path"],
            "original_staged_as": item["staged_as"],
            "sha256": item["sha256"],
            "bytes": item["bytes"],
        }
        for item in baseline_provenance["sources"]
    ]
    provenance: dict[str, Any] = {
        "format": "io.tobkiri.harness-input-stack-import-diagnostics-source/1",
        "generator": "scripts/wasm/prepare_harness_input_stack_import_diagnostics.py",
        "scope": (
            "private test-only import audit; every diagnostic native "
            "outcome fails"
        ),
        "source_capsule_only": True,
        "source_capsule_executable": False,
        "diagnostic_only": True,
        "diagnostic_outcome_must_fail": True,
        "baseline_source_provenance_sha256": _sha256(baseline_provenance_raw),
        "baseline_app_sha256": _sha256(baseline_app),
        "diagnostic_app_sha256": _sha256(files["python/app.py"]),
        "baseline_sources": baseline_records,
        "sources": _source_records(files, baseline_provenance),
        "original_function_bodies": baseline_provenance["original_function_bodies"],
        "outer_exports": [PROBE_INTERFACE],
        "allowed_imports": [CAPTURED_INTERFACE, IMPORT_TRACE_INTERFACE],
        "build_performed": False,
        "actual_build_performed": False,
        "wasm_component_built": False,
        "runtime_performed": False,
        "wasm_execution_performed": False,
        "guest_runtime_executed": False,
        "production_enabled": False,
        "whole_setup_performed": False,
        "full_setup_performed": False,
        "native_trace_observed": False,
        "native_result_claimed": False,
    }
    provenance["metadata_sha256"] = _sha256(_canonical_json_bytes(provenance))
    files["provenance.json"] = _json_bytes(provenance)
    return files


def plan_for(files: dict[str, bytes]) -> dict[str, Any]:
    """Return the separate diagnostic world with its exact two imports."""
    if set(files) != _EXPECTED_FILES | {"provenance.json"}:
        raise ValueError("diagnostic source capsule inventory changed")
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
        "wit": _PROBE_WIT,
        "world": WORLD,
        "entrypoint": "app",
        "sources": sources,
        "outer_exports": [PROBE_INTERFACE],
        "allowed_imports": [CAPTURED_INTERFACE, IMPORT_TRACE_INTERFACE],
    }


def stage(output: Path) -> dict[str, Any]:
    """Publish a fresh source-only diagnostic stage without building or running it."""
    files = captured()
    plan = plan_for(files)
    if captured() != files:
        raise ValueError("diagnostic source changed during staging")
    staged = dict(files)
    staged["plan.json"] = _json_bytes(plan)
    _publish(Path(output), staged)
    return plan


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    plan = stage(args.output)
    print(
        json.dumps(
            {
                "source_stage_created": True,
                "format": plan["format"],
                "diagnostic_only": True,
                "build_performed": False,
                "wasm_execution_performed": False,
                "production_enabled": False,
                "full_setup_performed": False,
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
