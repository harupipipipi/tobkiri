"""Select only a separately built libc for the inactive CI comparison producer.

This is explicit producer packaging, not a runtime asset override or approval
to use the candidate in the product. ABI/provider/schema proof stays separate.
"""

import hashlib
from pathlib import Path

from build_native import HERE, fingerprint
import build_ui_poll_libc
import fetch_sdk
import fetch_ui_poll_libc
from packager import package
import ui_empty_poll_source
import ui_poll_wit_workspace


def select(inputs: dict, build: Path) -> tuple[dict, dict, dict, dict]:
    """Bind actual successful private build outputs before changing one asset."""
    if (
        not build.is_absolute()
        or build.resolve() != build
        or not build.is_dir()
        or set(inputs) != package.ASSETS
    ):
        raise ValueError("physical libc build and complete original assets required")
    receipt_path = build / "libc-build.json"
    report = build_ui_poll_libc.json_file(receipt_path)
    original = (HERE / "tests/fixtures/wasi-libc-161b3195-poll.c").read_bytes()
    _, derivation = ui_empty_poll_source.derive(original)
    recipe_bytes = build_ui_poll_libc.RECIPE.encode()
    recipe_identity = {
        "bytes": len(recipe_bytes),
        "sha256": hashlib.sha256(recipe_bytes).hexdigest(),
    }
    if (
        report.get("format") != "tobkiri.ui-empty-poll-libc-build/1"
        or report.get("outcome") != "success"
        or any(
            report.get(name) is not True
            for name in (
                "build_performed",
                "inputs_unchanged",
                "wit_workspace_validated",
            )
        )
        or report.get("production_enabled") is not False
        or report.get("source_pin") != fetch_ui_poll_libc._pin()
        or report.get("derivation") != derivation
        or report.get("sdk_archive")
        != {"bytes": fetch_sdk.ARCHIVE_BYTES, "sha256": fetch_sdk.SHA256}
        or report.get("build_recipe") != recipe_identity
        or report.get("candidate_libc", {}).get("path") != "work/cmake/libc.so"
        or report.get("supervision", {}).get("outcome") != "success"
        or report["supervision"].get("exit_code") != 0
        or report["supervision"].get("remaining_known_processes") != []
    ):
        raise ValueError("inactive libc lacks its exact successful build binding")
    recipe = build / "build-libc.sh"
    if fingerprint(recipe) != recipe_identity:
        raise ValueError("libc recipe changed after its successful build")
    candidate = build / "work/cmake/libc.so"
    if candidate.resolve() != candidate:
        raise ValueError("physical candidate libc required")
    identity = fingerprint(candidate, 32 * 1024 * 1024)
    if identity != {key: report["candidate_libc"][key] for key in ("bytes", "sha256")}:
        raise ValueError("candidate libc differs from its actual build output")
    with candidate.open("rb") as stream:
        if stream.read(8) != b"\0asm\1\0\0\0":
            raise ValueError("candidate libc must remain a core module")
    stock, logical, previous_pin = inputs["libc.so.zst"]
    if (
        logical != "libc.so"
        or previous_pin is not None
        or stock.resolve() != stock
        or fingerprint(stock, 32 * 1024 * 1024) != report["original_libc"]
    ):
        raise ValueError("comparison stock libc differs from the original SDK")
    source = build / "candidate-source"
    build_ui_poll_libc.physical(source)
    source_inventory = fetch_sdk.inventory(source)
    if report.get("candidate_source_inventory") != source_inventory:
        raise ValueError(
            "complete candidate source inventory changed after compilation"
        )
    if fingerprint(source / ui_empty_poll_source.POLL_PATH) != derivation["candidate"]:
        raise ValueError("candidate poll source changed after compilation")
    if (
        ui_poll_wit_workspace.observe(
            build / "work", report["wit_workspace_before"], successful=True
        )
        != report["wit_workspace"]
    ):
        raise ValueError("derived WIT workspace changed after compilation")
    selected = dict(inputs)
    selected["libc.so.zst"] = (candidate, logical, identity["sha256"])
    controls = {
        str(path): fingerprint(path)
        for path in (receipt_path, recipe, source / ui_empty_poll_source.POLL_PATH)
    }
    snapshots = {str(source): source_inventory}
    marker = {
        "format": "tobkiri.ui-empty-poll-ci-asset/1",
        "build_receipt": controls[str(receipt_path)],
        "source_pin": report["source_pin"],
        "derivation": derivation,
        "original_libc": report["original_libc"],
        "candidate_libc": identity,
        "changed_asset": "libc.so.zst",
        "runtime_asset_override": False,
        "scope": "inactive CI original/candidate comparison producer only",
        "c_abi_verified": False,
        "final_provider_resolution_verified": False,
        "cold_schema_verified": False,
        "production_enabled": False,
    }
    return selected, marker, controls, snapshots


def require_cli_mode(asset_report: dict, allowed: bool) -> dict | None:
    """Make ordinary producer invocations reject inactive candidate assets."""
    marker = asset_report.get("ui_empty_poll_candidate")
    if marker is None:
        if allowed:
            raise ValueError("explicit inactive libc mode requires candidate assets")
        return None
    if (
        not allowed
        or not isinstance(marker, dict)
        or marker.get("format") != "tobkiri.ui-empty-poll-ci-asset/1"
        or marker.get("changed_asset") != "libc.so.zst"
        or marker.get("production_enabled") is not False
        or marker.get("runtime_asset_override") is not False
    ):
        raise ValueError("inactive candidate assets require the explicit CI-only mode")
    return marker
