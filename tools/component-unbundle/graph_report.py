"""Bind a fully validated Rust graph observation to every actual core occurrence.

Graph rows retain local component namespaces and source ranges. This report
does not turn initial bindings into call-time table, TLS or provider proof.
"""

from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "quality"))
sys.path.insert(0, str(HERE.parent / "python313-producer"))
import component_module_inventory as inventory  # noqa: E402
import poll_graph_inputs  # noqa: E402
from build_native import fingerprint  # noqa: E402

OUTPUT_LIMIT = 64 * 1024 * 1024
MAX_ROWS = 1_000_000


def read_rows(path: Path) -> list[list[str]]:
    """Reject truncated/oversized or non-string diagnostic records."""
    fingerprint(path, OUTPUT_LIMIT)
    rows = []
    with path.open("rb") as stream:
        for raw in stream:
            if len(raw) > 2 * 1024 * 1024 or not raw.endswith(b"\n"):
                raise ValueError("oversized or incomplete graph row")
            row = json.loads(raw)
            if (
                type(row) is not list
                or not row
                or any(type(v) is not str for v in row)
                or len(rows) >= MAX_ROWS
            ):
                raise ValueError("invalid graph row shape or count")
            rows.append(row)
    if (
        rows[:1] != [["format", "tobkiri.core-graph-observation/1"]]
        or len(rows) < 8
        or rows[-3:]
        != [
            ["final_provider_resolution_verified", "false"],
            ["errno_storage_identity_verified", "false"],
            ["production_enabled", "false"],
        ]
        or len(rows[-4]) != 3
        or rows[-4][0] != "complete"
    ):
        raise ValueError("incomplete or incorrectly scoped graph observation")
    return rows


def summarize(component: Path, rows: list[list[str]], libraries: list[dict]) -> dict:
    """Independently reconcile every Rust module range with binary framing."""
    observed = inventory.inspect_component(component)
    modules = [row for row in rows if row[0] == "module"]
    if len(modules) != len(observed["modules"]):
        raise ValueError("Rust observation omitted a core module occurrence")
    data = component.read_bytes()
    if (
        len(data) != observed["bytes"]
        or hashlib.sha256(data).hexdigest() != observed["sha256"]
    ):
        raise ValueError("component changed during summary")
    result = []
    for ordinal, (row, framed) in enumerate(
        zip(modules, observed["modules"], strict=True)
    ):
        if len(row) != 6 or [int(row[3]), int(row[4]), int(row[5])] != [
            ordinal,
            framed["offset"],
            framed["bytes"],
        ]:
            raise ValueError("module occurrence order or byte range differs")
        module = {
            **framed,
            "id": ordinal,
            "scope": int(row[1]),
            "local_module_index": int(row[2]),
        }
        module["exact_input_matches"] = [
            lib["name"]
            for lib in libraries
            if lib["bytes"] == framed["bytes"] and lib["sha256"] == framed["sha256"]
        ]
        module["imports"] = [
            r[2:] for r in rows if r[0] == "import" and r[1] == str(ordinal)
        ]
        module["exports"] = [
            r[2:] for r in rows if r[0] == "export" and r[1] == str(ordinal)
        ]
        functions = []
        for r in rows:
            if r[0] != "body" or r[1] != str(ordinal):
                continue
            if len(r) != 5:
                raise ValueError("malformed function body range")
            index, offset, size = map(int, r[2:])
            if (
                not framed["offset"]
                <= offset
                < offset + size
                <= framed["offset"] + framed["bytes"]
            ):
                raise ValueError("body range escapes its actual embedded module")
            functions.append(
                {
                    "index": index,
                    "offset": offset,
                    "bytes": size,
                    "sha256": hashlib.sha256(data[offset : offset + size]).hexdigest(),
                }
            )
        module["bodies"] = functions
        result.append(module)
    complete = rows[-4]
    if int(complete[2]) != len(result):
        raise ValueError("graph completion count differs")
    return {
        "component": {k: observed[k] for k in ("bytes", "sha256")},
        "all_core_module_occurrences_observed": True,
        "modules": result,
        "row_counts": dict(Counter(row[0] for row in rows)),
        "component_instance_resolution_complete": False,
        "final_provider_resolution_verified": False,
        "errno_storage_identity_verified": False,
        "production_enabled": False,
    }


def run(bundle: Path, authentication: Path, worker: Path, destination: Path) -> None:
    """Analyze exact CI-only inputs without instantiating or invoking a guest."""
    if sys.platform != "linux" or os.environ.get("CI") != "true":
        raise ValueError("actual component analysis runs on CI only")
    pins = poll_graph_inputs.read_json(poll_graph_inputs.PINS)
    poll_graph_inputs.authenticate(pins, authentication)
    before = poll_graph_inputs.verify_bundle(bundle, pins)
    if destination.exists() or destination.parent.resolve() != destination.parent:
        raise ValueError("fresh physical graph destination required")
    destination.mkdir(mode=0o700)
    worker_identity = fingerprint(worker)
    comparison = poll_graph_inputs.read_json(bundle / "comparison.json")
    report = {
        "format": "tobkiri.ui-poll-all-module-observations/1",
        "outcome": "incomplete",
        "supplier": pins,
        "worker": worker_identity,
        "guest_executed": False,
        "final_provider_resolution_verified": False,
        "errno_storage_identity_verified": False,
        "production_enabled": False,
        "variants": {},
    }
    try:
        for variant in ("original", "candidate"):
            component = bundle / variant / "work/stage/component.wasm"
            raw = destination / f"{variant}-graph.jsonl"
            with component.open("rb") as incoming, raw.open("xb") as outgoing:
                os.chmod(raw, 0o600)
                subprocess.run(
                    [str(worker), "--diagnostic-graph"],
                    stdin=incoming,
                    stdout=outgoing,
                    check=True,
                    timeout=120,
                )
            rows = read_rows(raw)
            libraries = comparison["variants"][variant]["reference"]["trace"][
                "libraries"
            ]
            report["variants"][variant] = summarize(component, rows, libraries)
            report["variants"][variant]["raw_observation"] = fingerprint(
                raw, OUTPUT_LIMIT
            )
        if (
            fingerprint(worker) != worker_identity
            or poll_graph_inputs.verify_bundle(bundle, pins) != before
        ):
            raise ValueError("analyzed worker or inputs changed")
        report["outcome"] = "success"
    finally:
        (destination / "graph.json").write_text(json.dumps(report, indent=2) + "\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("bundle", "authentication", "worker", "destination"):
        parser.add_argument("--" + name, type=Path, required=True)
    run(**vars(parser.parse_args()))
