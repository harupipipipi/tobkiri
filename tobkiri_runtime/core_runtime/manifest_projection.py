"""Non-executable metadata tombstone for the removed runtime projection.

Projection owner: ``scripts/offline_legacy_projection.py``.
Canonical source: ``rumi.pack.v3.json``.
Runtime execution: forbidden.
"""

from __future__ import annotations

import json
from typing import Any, Mapping

from .global_contracts.canonical import content_identity

LEGACY_ECOSYSTEM_FORMAT = "rumi.ecosystem.v1"
PROJECTION_GENERATOR = "tobkiri.core_runtime.manifest_projection/v2"
PROJECTION_OWNER = "scripts/offline_legacy_projection.py"
PROJECTION_SOURCE = "rumi.pack.v3.json"
PROJECTION_RUNTIME_EXECUTABLE = False


class ManifestProjectionError(ValueError):
    """Compatibility exception type retained for metadata readers."""


def source_manifest_identity(manifest: Mapping[str, Any]) -> str:
    """Return canonical source identity without generating compatibility data."""
    payload = json.loads(json.dumps(manifest, ensure_ascii=False))
    payload.pop("content_identity", None)
    provenance = payload.get("provenance")
    if isinstance(provenance, dict):
        provenance.pop("content_hash", None)
    return content_identity(payload)


__all__ = [
    "LEGACY_ECOSYSTEM_FORMAT",
    "ManifestProjectionError",
    "PROJECTION_GENERATOR",
    "PROJECTION_OWNER",
    "PROJECTION_RUNTIME_EXECUTABLE",
    "PROJECTION_SOURCE",
    "source_manifest_identity",
]
