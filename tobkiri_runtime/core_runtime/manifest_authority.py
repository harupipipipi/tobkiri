"""Offline inventory assertion for canonical Pack v4 authority."""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Iterable, Literal

ManifestAuthority = Literal["v4-authoritative"]

CATALOG_PATH = (
    Path(__file__).parents[1] / "schemas" / "manifest_authority.v1.json"
)
_VALID_AUTHORITIES = {"v4-authoritative"}


class ManifestAuthorityError(ValueError):
    """Raised when Pack authority metadata is missing or inconsistent."""


@lru_cache(maxsize=1)
def load_manifest_authority_catalog() -> dict[str, ManifestAuthority]:
    """Load and validate the repository Pack authority catalog."""
    try:
        payload = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ManifestAuthorityError(
            f"manifest authority catalog is unreadable: {exc}"
        ) from exc
    if not isinstance(payload, dict) or payload.get("version") != 1:
        raise ManifestAuthorityError("manifest authority catalog version must be 1")
    packs = payload.get("packs")
    if not isinstance(packs, dict):
        raise ManifestAuthorityError("manifest authority catalog packs must be an object")
    result: dict[str, ManifestAuthority] = {}
    for pack_id, authority in packs.items():
        if not isinstance(pack_id, str) or authority not in _VALID_AUTHORITIES:
            raise ManifestAuthorityError(
                f"invalid manifest authority classification: {pack_id!r}={authority!r}"
            )
        result[pack_id] = authority
    return result


def repository_manifest_authority(pack_id: str) -> ManifestAuthority:
    """Assert that a shipped Pack is owned by its canonical v4 artifacts."""
    authority = load_manifest_authority_catalog().get(pack_id)
    if authority is None:
        raise ManifestAuthorityError(
            f"Pack '{pack_id}' is not classified in manifest authority catalog"
        )
    return authority


def validate_manifest_authority_scope(
    pack_ids: Iterable[str] | None,
    *,
    require_complete_catalog: bool = False,
) -> None:
    """Validate an explicit Pack scope without discovering installed Packs.

    Runtime callers pass a ResolvedProfile effective set or an explicit shipped
    catalog. Repository-wide discovery belongs only to offline build tooling,
    which passes its already-discovered IDs with ``require_complete_catalog``.
    """
    if pack_ids is None or isinstance(pack_ids, (str, bytes)):
        raise ManifestAuthorityError("manifest authority scope must be explicit")
    values = tuple(pack_ids)
    if any(not isinstance(pack_id, str) or not pack_id for pack_id in values):
        raise ManifestAuthorityError("manifest authority scope has an invalid Pack ID")
    if len(set(values)) != len(values):
        raise ManifestAuthorityError("manifest authority scope has duplicate Pack IDs")
    scoped = set(values)
    classified = set(load_manifest_authority_catalog())
    extra = sorted(scoped - classified)
    stale = sorted(classified - scoped) if require_complete_catalog else []
    if extra or stale:
        raise ManifestAuthorityError(
            f"manifest authority catalog mismatch: extra={extra}, stale={stale}"
        )
