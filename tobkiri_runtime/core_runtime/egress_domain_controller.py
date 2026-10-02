"""Immutable Pack v4 network-domain policy for the egress proxy."""

from __future__ import annotations

import fnmatch
from types import MappingProxyType
from typing import Any, Mapping

from tobkiri_protocol.canonical import canonical_digest
from tobkiri_protocol.validation import validate_document


class DomainController:
    """Check domains against an explicitly selected Pack v4 policy snapshot.

    An empty or missing policy denies.  The controller never scans installed
    Packs, reads environment variables, or treats a legacy manifest as
    authority.
    """

    def __init__(self, policies: Mapping[str, tuple[str, ...]] | None = None) -> None:
        normalized: dict[str, tuple[str, ...]] = {}
        for pack_id, domains in (policies or {}).items():
            identity = str(pack_id or "").strip()
            values = tuple(sorted({str(item).strip().lower() for item in domains}))
            if not identity or not values or any(not item for item in values):
                raise ValueError("Pack v4 domain policy is incomplete")
            normalized[identity] = values
        self._policies: Mapping[str, tuple[str, ...]] = MappingProxyType(normalized)

    @classmethod
    def from_pack_v4_documents(
        cls,
        selected: Mapping[str, Mapping[str, Any]],
        bindings: Mapping[str, Mapping[str, str]],
    ) -> "DomainController":
        """Compile exact policies from an already-resolved Profile Pack set."""

        policies: dict[str, tuple[str, ...]] = {}
        for expected_id, document in selected.items():
            manifest = validate_document(dict(document), "pack")
            pack_id = str(manifest["pack"]["id"])
            binding = bindings.get(expected_id)
            if (
                pack_id != expected_id
                or pack_id in policies
                or not isinstance(binding, Mapping)
                or binding.get("source_identity")
                != manifest["integrity"]["source_identity"]
                or binding.get("artifact_digest") != manifest["pack"]["artifact_digest"]
                or binding.get("manifest_digest") != canonical_digest(manifest)
            ):
                raise ValueError("selected Pack v4 network identity mismatch")
            network = manifest["requirements"]["network"]
            allowed = network.get("allowed_domains") if isinstance(network, Mapping) else None
            if not isinstance(allowed, list):
                raise ValueError("selected Pack v4 network policy is missing")
            domains = tuple(str(item).strip().lower() for item in allowed)
            if domains:
                policies[pack_id] = domains
        if set(bindings) != set(selected):
            raise ValueError("selected Pack v4 network bindings are not exact")
        return cls(policies)

    def check_domain(self, pack_id: str, domain: str) -> tuple[bool, str]:
        """Allow only a domain in the exact selected Pack policy."""

        identity = str(pack_id or "").strip()
        candidate = str(domain or "").strip().lower().rstrip(".")
        patterns = self._policies.get(identity)
        if not patterns or not candidate:
            return False, "Pack v4 domain authority is unavailable"
        if any(_matches(candidate, pattern) for pattern in patterns):
            return True, ""
        return False, "Domain is outside the selected Pack v4 network policy"

    def invalidate_cache(self, pack_id: str | None = None) -> None:
        """Reject legacy mutation: a captured v4 policy snapshot is immutable."""

        del pack_id
        raise RuntimeError("Pack v4 domain policy snapshots are immutable")


def _matches(domain: str, pattern: str) -> bool:
    normalized = pattern.lower().rstrip(".")
    if normalized == "*":
        return True
    if normalized.startswith("*."):
        base = normalized[2:]
        return domain == base or domain.endswith("." + base)
    return domain == normalized or fnmatch.fnmatchcase(domain, normalized)


__all__ = ["DomainController"]
