"""Bind two real inactive producer builds differing only in their libc asset.

This validates build/input custody, not component linking or C execution.
"""

import argparse
import json
import os
from pathlib import Path
import sys

from build_assets import verify_outputs
from build_native import fingerprint
import ci_link_trace_source
import fetch_sdk
import runtime_inputs
import ui_empty_poll_assets


def one(assets: Path, producer: Path, *, candidate: bool) -> dict:
    """Revalidate actual receipts, packaged bytes, source tree and CLI binary."""
    for path in (assets, producer):
        if not path.is_absolute() or path.resolve() != path or not path.is_dir():
            raise ValueError("physical producer/asset roots required")
    asset_path = assets / "assets-build.json"
    asset = runtime_inputs.report(asset_path)
    marker = ui_empty_poll_assets.require_cli_mode(asset, candidate)
    outputs = verify_outputs(
        assets / "work/assets", asset["manifest"]["sha256"], asset["worker"]["sha256"]
    )
    if outputs != asset["outputs"] or asset["inputs_unchanged"] is not True:
        raise ValueError("paired asset build lacks unchanged successful outputs")
    report_path = producer / "cli-build.json"
    report = runtime_inputs.report(report_path)
    source_path = producer / "cli-source.json"
    fingerprint(source_path, 16 * 1024 * 1024)
    source = json.loads(source_path.read_bytes())
    for path in (asset_path, source_path):
        if report["predecessors"].get(str(path)) != fingerprint(path):
            raise ValueError(
                "producer predecessor does not bind the actual paired build"
            )
    if (
        report["inputs"] != asset["inputs"]
        or report["inputs_unchanged"] is not True
        or report["producer_cli"] is not True
        or report["production_enabled"] is not False
        or report.get("ui_empty_poll_libc_candidate") != marker
        or report.get("ci_link_trace_candidate") != source.get("ci_link_trace")
        or source.get("ci_link_trace", {}).get("feature")
        != ci_link_trace_source.FEATURE
        or source["ci_link_trace"].get("upstream_link_sha256")
        != ci_link_trace_source.ORIGINAL_LINK_SHA256
    ):
        raise ValueError(
            "pair requires the same explicit inactive observed producer mode"
        )
    if fetch_sdk.inventory(producer / "source") != source["files"]:
        raise ValueError("producer source or embedded assets changed after build")
    binary = producer / "work/target/release/componentize-py"
    if fingerprint(binary, 128 * 1024 * 1024) != report["binary"]:
        raise ValueError("paired producer binary differs from its actual build")
    return {
        "assets": asset,
        "producer": report,
        "source": source,
        "receipts": {
            str(path): fingerprint(path)
            for path in (asset_path, source_path, report_path)
        },
    }


def require_pair(original: dict, candidate: dict) -> None:
    """Require all other assets, source, lock and build inputs to remain equal."""
    old, new = original["assets"], candidate["assets"]
    if (
        old["inputs"] != new["inputs"]
        or old["runtime_receipt"] != new["runtime_receipt"]
    ):
        raise ValueError("paired assets do not share original source/runtime inputs")
    differences = {
        name
        for name in old["outputs"] | new["outputs"]
        if old["outputs"].get(name) != new["outputs"].get(name)
    }
    if differences != {"libc.so.zst"}:
        raise ValueError("paired producer assets differ beyond the single libc")
    old, new = original["source"], candidate["source"]
    differences = {
        name
        for name in old["files"] | new["files"]
        if old["files"].get(name) != new["files"].get(name)
    }
    if (
        differences != {"libc.so.zst"}
        or old["original_files"] != new["original_files"]
        or {k: v for k, v in old.items() if k != "files"}
        != {k: v for k, v in new.items() if k != "files"}
    ):
        raise ValueError("producer source/lock/trace/bindings configuration differs")
    a, b = original["producer"], candidate["producer"]
    for name in (
        "inputs",
        "original_recipe_sha256",
        "source_delta",
        "candidate_recipe",
        "ci_link_trace_predecessor_recipe",
        "ci_link_trace_candidate",
        "ui_wall_clock_candidate",
        "ui_monotonic_clock_candidate",
    ):
        if a.get(name) != b.get(name):
            raise ValueError("paired compiler configuration differs: " + name)


def run(
    original_assets: Path,
    candidate_assets: Path,
    original_producer: Path,
    candidate_producer: Path,
    helper: Path,
    output: Path,
) -> dict:
    """Publish only an honest build-pair receipt, with all runtime flags false."""
    if sys.platform != "linux" or os.environ.get("GITHUB_ACTIONS") != "true":
        raise ValueError("producer pairing is restricted to Linux CI")
    if (
        not output.is_absolute()
        or output.parent.resolve() != output.parent
        or not output.parent.is_dir()
        or os.path.lexists(output)
    ):
        raise ValueError("new physical pair receipt required")
    if not helper.is_absolute() or helper.resolve() != helper or not helper.is_dir():
        raise ValueError("physical helper build required")
    a = one(original_assets, original_producer, candidate=False)
    b = one(candidate_assets, candidate_producer, candidate=True)
    require_pair(a, b)
    h = runtime_inputs.report(helper / "helper-build.json")
    if (
        h["compiled"] is not True
        or h["inputs_unchanged"] is not True
        or h["inspection"]["shared_imports_verified"] is not True
        or fingerprint(helper / "work/_tobkiri_poll_oracle.abi3.so") != h["helper"]
    ):
        raise ValueError("paired C helper lacks its actual shared-import build proof")
    report = {
        "format": "tobkiri.ui-empty-poll-producer-pair/1",
        "outcome": "success",
        "changed_asset": "libc.so.zst",
        "original": a,
        "candidate": b,
        "helper": h["helper"],
        "helper_receipt": fingerprint(helper / "helper-build.json"),
        "same_helper_required_for_both_components": True,
        "componentization": False,
        "guest_execution": False,
        "trace_observed": False,
        "final_provider_resolution_verified": False,
        "c_abi_verified": False,
        "cold_schema_verified": False,
        "production_enabled": False,
    }
    encoded = (json.dumps(report, indent=2) + "\n").encode()
    if len(encoded) > 16 * 1024 * 1024:
        raise ValueError("paired build metadata exceeds its existing receipt bound")
    output.write_bytes(encoded)
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    for name in (
        "original-assets",
        "candidate-assets",
        "original-producer",
        "candidate-producer",
        "helper",
        "output",
    ):
        parser.add_argument("--" + name, type=Path, required=True)
    result = run(**vars(parser.parse_args()))
    print(
        json.dumps(
            {
                key: result[key]
                for key in (
                    "outcome",
                    "changed_asset",
                    "componentization",
                    "guest_execution",
                    "c_abi_verified",
                    "production_enabled",
                )
            }
        )
    )
