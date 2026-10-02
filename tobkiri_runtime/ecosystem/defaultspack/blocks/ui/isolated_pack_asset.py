"""Generic opaque-frame asset server scoped to the resolved profile."""

from __future__ import annotations

import hashlib
import json
import mimetypes
from pathlib import Path
from typing import Any, Mapping

from blocks._common import error
from core_runtime.pack_artifact_integrity import verify_declared_artifacts
from core_runtime.paths import resolve_pack_locations
from core_runtime.resolved_profile_scope import persisted_resolved_profile


def run(input_data: dict, context: dict) -> dict:
    """Serve an effective pack's built UI with restrictive containment headers."""
    del context
    data = input_data if isinstance(input_data, dict) else {}
    pack_id = str(data.get("pack_id") or "").strip()
    asset_path = str(data.get("asset_path") or "index.html").strip()
    plan = persisted_resolved_profile()
    if plan is None or pack_id not in plan.effective_pack_set:
        return error("isolated UI pack is not active", "PACK_NOT_ACTIVE")
    locations = resolve_pack_locations((pack_id,))
    if len(locations) != 1:
        return error("isolated UI pack is unavailable", "PACK_UNAVAILABLE")
    relative = Path(asset_path.replace("\\", "/"))
    if relative.is_absolute() or ".." in relative.parts:
        return error("invalid isolated UI path", "INVALID_PATH")
    root = (locations[0].pack_subdir / "ui").resolve()
    candidate = (root / relative).resolve()
    try:
        candidate.relative_to(root)
    except ValueError:
        return error("invalid isolated UI path", "INVALID_PATH")
    if not candidate.is_file():
        return error("isolated UI asset not found", "ASSET_NOT_FOUND")
    try:
        asset_bytes = candidate.read_bytes()
    except OSError:
        return error("isolated UI asset not found", "ASSET_NOT_FOUND")
    manifest = _read_manifest(locations[0].ecosystem_json_path)
    expected_pack_hash = next(
        (
            item.content_hash
            for item in plan.packs
            if item.pack_id == pack_id
        ),
        "",
    )
    provenance = manifest.get("provenance") if isinstance(manifest, Mapping) else None
    if (
        not isinstance(manifest, Mapping)
        or not isinstance(provenance, Mapping)
        or str(provenance.get("content_hash") or "") != expected_pack_hash
    ):
        return error("isolated UI pack identity changed", "PACK_INTEGRITY_FAILED")
    integrity_ok, _ = verify_declared_artifacts(locations[0].pack_subdir, manifest)
    if not integrity_ok or not _is_declared_isolated_asset(
        locations[0].pack_subdir,
        manifest,
        relative,
        asset_bytes,
    ):
        return error(
            "isolated UI asset is not verified for the active pack",
            "PACK_INTEGRITY_FAILED",
        )
    content_type = mimetypes.guess_type(candidate.name)[0] or "application/octet-stream"
    if content_type.startswith("text/") or content_type in {
        "application/javascript",
        "application/json",
    }:
        try:
            body: bytes | str = asset_bytes.decode("utf-8")
        except UnicodeDecodeError:
            return error("isolated UI text asset is invalid", "ASSET_INVALID")
        content_type += "; charset=utf-8"
    else:
        body = asset_bytes
    return {
        "_binary": True,
        "status_code": 200,
        "content_type": content_type,
        "body": body,
        "headers": {
            "Cache-Control": "no-store",
            "Content-Security-Policy": (
                "default-src 'none'; script-src 'self' http://127.0.0.1:* "
                "http://localhost:*; style-src 'self' 'unsafe-inline' "
                "http://127.0.0.1:* http://localhost:*; img-src 'self' data:; "
                "connect-src 'none'; frame-ancestors 'self'; "
                "base-uri 'none'; form-action 'none'"
            ),
            # Sandboxed frames have an opaque origin; assets remain loopback-only.
            "Cross-Origin-Resource-Policy": "cross-origin",
            "Referrer-Policy": "no-referrer",
            "X-Content-Type-Options": "nosniff",
        },
    }


def _read_manifest(path: Path) -> Mapping[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, Mapping) else None


def _is_declared_isolated_asset(
    pack_root: Path,
    manifest: Mapping[str, Any],
    relative: Path,
    asset_bytes: bytes,
) -> bool:
    """Require each served isolated asset to be bound by the pack evidence."""
    metadata = manifest.get("metadata")
    integrity = metadata.get("integrity") if isinstance(metadata, Mapping) else None
    artifact_relative = (
        str(integrity.get("artifact_manifest") or "").strip()
        if isinstance(integrity, Mapping)
        else ""
    )
    if not artifact_relative:
        return False
    artifact_path = (pack_root / artifact_relative).resolve()
    try:
        artifact_path.relative_to(pack_root.resolve())
        payload = json.loads(artifact_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return False
    artifacts = payload.get("artifacts") if isinstance(payload, dict) else None
    expected_path = f"ui/{relative.as_posix()}"
    expected_hash = next(
        (
            str(item.get("sha256") or "").strip()
            for item in artifacts if isinstance(item, Mapping)
            and str(item.get("path") or "") == expected_path
        ),
        "",
    )
    if not expected_hash:
        return False
    if not expected_hash.startswith("sha256:"):
        expected_hash = f"sha256:{expected_hash}"
    actual_hash = "sha256:" + hashlib.sha256(asset_bytes).hexdigest()
    return actual_hash == expected_hash
