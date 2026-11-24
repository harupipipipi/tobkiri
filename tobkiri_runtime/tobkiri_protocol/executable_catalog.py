"""Shared executable-catalog identity rules for planning and generation."""

from __future__ import annotations

import re
from typing import Any, Mapping


_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")


def materialization_catalog_digest(
    manifest: Mapping[str, Any],
    executable: Mapping[str, Any],
) -> str:
    """Return the unambiguous executable catalog digest used at materialization.

    Source-bound generated Pack projections must carry a separate canonical
    materialization pin. Canonical and externally admitted Packs use their own
    catalog digest and may not introduce an alias field.
    """

    integrity = manifest.get("integrity")
    provenance = manifest.get("provenance")
    source_identity = integrity.get("source_identity") if isinstance(integrity, Mapping) else None
    is_projection = (
        isinstance(provenance, Mapping)
        and provenance.get("schema") == "io.tobkiri.provenance.v2"
        and provenance.get("source_kind") == "generated"
        and provenance.get("source_digest") == source_identity
    )
    projected_pin = executable.get("materialization_catalog_digest")
    if is_projection:
        if not isinstance(projected_pin, str) or not _DIGEST_RE.fullmatch(projected_pin):
            raise ValueError("projected Pack materialization executable catalog digest is missing")
        return projected_pin
    if projected_pin is not None:
        raise ValueError("non-projected Pack cannot replace its executable catalog identity")
    catalog_digest = executable.get("catalog_digest")
    if not isinstance(catalog_digest, str) or not _DIGEST_RE.fullmatch(catalog_digest):
        raise ValueError("Pack executable catalog digest is invalid")
    return catalog_digest
