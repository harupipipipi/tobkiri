"""Closed dependency inputs for the explicit App schema provider Harness build.

Only the source-built Rust extension, the two App provider sources and one fixed
preinit import of that extension are added. Nothing here imports or executes
them, prepares a validation result, loads a schema, or changes a budget.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

from build_native import HERE, fingerprint
from fetch_sdk import inventory

SOURCE = HERE / "schema_kernel"
KERNEL_NAME = "_tobkiri_schema_kernel.abi3.so"
PROVIDER_SOURCES = {
    "app_schema_provider.py": "app_provider.py",
    "app_schema_model.py": "app_schema_model.py",
}
ARTIFACT = "work/target/wasm32-wasip2/release/_tobkiri_schema_kernel.wasm"
FORMAT = "tobkiri.harness-native-app-schema-provider-dependencies/1"
ENTRY_ANCHOR = b"import rpds.rpds  # noqa: E402\n"
ENTRY_ADDITION = b'''import _tobkiri_schema_kernel as _schema_kernel  # noqa: E402

# The jsonschema modules above are the genuine preinit imports. The explicit App
# provider is only imported later, by the original validation module, and no
# validation (or schema load) may have happened before that first selection.
if (
    _schema_kernel.production_enabled is not False
    or not _schema_kernel.__file__.endswith("/_tobkiri_schema_kernel.abi3.so")
    or _schema_kernel.app_schema_entry_count() != 0
    or _schema_kernel.app_schema_frame_count() != 0
):
    raise RuntimeError("actual source-built App schema kernel required before use")
'''


def verify_extension(root: Path) -> tuple[bytes, dict]:
    """Authenticate the actual source-built kernel; return bytes and its receipt."""
    if (os.environ.get("GITHUB_ACTIONS") != "true" or not root.is_absolute()
            or root.resolve(strict=True) != root):
        raise ValueError("physical CI extension build required")
    report_path = root / "extension-build.json"
    report = json.loads(report_path.read_bytes())
    if (report.get("format") != "tobkiri.app-schema-kernel-extension/1"
            or report.get("outcome") != "success"
            or report.get("guest_executed") is not False
            or report.get("production_enabled") is not False
            or report.get("source_head") != os.environ.get("GITHUB_SHA")
            or report.get("features") != ["app-schema-backend"]
            or report.get("source_files") != inventory(SOURCE)
            or report.get("artifact_path") != ARTIFACT):
        raise ValueError("actual source-built App backend extension required")
    artifact = root / ARTIFACT
    if (artifact.resolve(strict=True) != artifact
            or fingerprint(artifact, 4 * 1024 * 1024) != report["artifact"]):
        raise ValueError("actual Wasm kernel bytes differ")
    return artifact.read_bytes(), {
        "extension_build": fingerprint(report_path, 4 * 1024 * 1024),
        "extension": report["artifact"], "kernel_source": report["source_files"],
        "features": report["features"],
    }


def derive_entry(entry: bytes) -> bytes:
    """Add only the fixed kernel preinit import after the genuine rpds import."""
    if entry.count(ENTRY_ANCHOR) != 1 or b"_tobkiri_schema_kernel" in entry:
        raise ValueError("exact original entry derivation anchor required")
    derived = entry.replace(ENTRY_ANCHOR, ENTRY_ANCHOR + ENTRY_ADDITION)
    if derived.replace(ENTRY_ADDITION, b"") != entry:
        raise ValueError("entry derivation changed an original statement")
    return derived


def additions(root: Path) -> tuple[dict[str, bytes], dict]:
    """Return the closed derived dependency members and their identity receipt."""
    kernel, receipt = verify_extension(root)
    members = {KERNEL_NAME: kernel}
    for name, source in PROVIDER_SOURCES.items():
        path = SOURCE / source
        if path.is_symlink() or not path.is_file():
            raise ValueError("regular App provider source required")
        members[name] = path.read_bytes()
    receipt["provider_sources"] = {
        name: {"bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}
        for name, raw in members.items() if name != KERNEL_NAME
    }
    return members, receipt
