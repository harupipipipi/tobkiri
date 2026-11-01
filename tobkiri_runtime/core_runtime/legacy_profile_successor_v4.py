"""Fail-closed v4 successors for imported legacy startup Profiles."""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any, Mapping

from tobkiri_protocol.migration import KNOWN_PACK_ALIASES, migrate_legacy_profile
from tobkiri_protocol.validation import validate_document


_LEGACY_CONTRACT_PROVIDERS = {
    # The generic adapter is the legacy defaultspack provider.  The other
    # implementation of this contract is the explicit human-handoff Pack and
    # must never be guessed for a migrated startup Profile.
    "tobkiri.service.ai.provider.generate.v1": "rumi_provider_adapters_pack",
}


class LegacyProfileSuccessorError(ValueError):
    """Raised when legacy bytes cannot name one exact v4 successor."""


def build_legacy_profile_successor(
    legacy_profile: Mapping[str, Any],
    *,
    profile_id: str,
    catalog: Any,
    source_path: str,
    repository_root: Path | None = None,
) -> dict[str, Any]:
    """Build one non-authorizing successor for the canonical v4 surface.

    The packaged Defaults Profile is deliberately not used as a template.
    Legacy selectors are translated through a closed alias table and then
    bound to the unique verified Shell/Application pair in the locked catalog.
    Ambiguity fails the whole caller-owned transaction.
    """

    source = copy.deepcopy(dict(legacy_profile))
    source["profile_id"] = profile_id
    migrated = migrate_legacy_profile(
        source,
        source_path=source_path,
        repository_root=repository_root,
    )
    if migrated.get("status") != "review_required":
        reasons = migrated.get("blocked_reasons") or ["legacy migration was blocked"]
        raise LegacyProfileSuccessorError("; ".join(str(item) for item in reasons))
    profile = migrated.get("profile")
    if not isinstance(profile, Mapping):
        raise LegacyProfileSuccessorError("legacy migration produced no Profile")
    successor = copy.deepcopy(dict(profile))

    base_id = str(successor["base"]["pack_id"])
    bases = _catalog_mapping(catalog, "bases")
    packs = _catalog_mapping(catalog, "packs")
    shells = _catalog_mapping(catalog, "shells")
    base = bases.get(base_id)
    base_manifest = packs.get(base_id)
    if (
        not isinstance(base, Mapping)
        or not isinstance(base_manifest, Mapping)
        or base_manifest.get("pack", {}).get("kind") != "base"
    ):
        raise LegacyProfileSuccessorError(
            f"legacy Base is absent from the locked catalog: {base_id}"
        )

    selected_pack_ids = _selected_legacy_packs(source, base_id=base_id)
    for pack_id in selected_pack_ids:
        if pack_id not in packs:
            raise LegacyProfileSuccessorError(
                f"legacy Pack is absent from the locked catalog: {pack_id}"
            )
    foundational_id = _foundational_provider(packs)
    if foundational_id not in selected_pack_ids:
        selected_pack_ids.append(foundational_id)
    _add_required_contract_providers(selected_pack_ids, packs)

    shell, variant, application_id = _verified_presentation(
        base=base,
        shells=shells,
        packs=packs,
    )
    requested_packs = [
        {
            "pack_id": pack_id,
            "artifact_digest": None,
            "role": "provider",
        }
        for pack_id in selected_pack_ids
    ]
    requested_packs.append(
        {
            "pack_id": application_id,
            "artifact_digest": None,
            "role": "application",
        }
    )
    successor.update(
        {
            "profile_api_version": "io.tobkiri.profile.v4",
            "profile_id": profile_id,
            "state": "needs_resolution",
            "mode": "interactive",
            "catalog_revision": None,
            "base": {
                "pack_id": base_id,
                "artifact_digest": None,
                "definition_revision": None,
                "resolution": "verified_exact_artifact_required",
            },
            "shell": {
                "provider_id": str(shell["provider_id"]),
                "pack_id": str(shell["pack_id"]),
                "artifact_digest": None,
                "definition_revision": None,
                "contract_id": str(shell["contract_id"]),
                "platform": str(variant["platform"]),
                "architecture": str(variant["architecture"]),
            },
            "packs": requested_packs,
            "requested_edges": [],
            "authority_references": [],
            "profile_authority_snapshot_digest": None,
        }
    )
    validate_document(successor, "profile")
    return successor


def _catalog_mapping(catalog: Any, name: str) -> Mapping[str, Mapping[str, Any]]:
    value = getattr(catalog, name, None)
    if not isinstance(value, Mapping):
        raise LegacyProfileSuccessorError(f"locked catalog has no {name} inventory")
    return value


def _selected_legacy_packs(
    legacy_profile: Mapping[str, Any],
    *,
    base_id: str,
) -> list[str]:
    raw = legacy_profile.get("packs")
    if not isinstance(raw, list) or not raw:
        raise LegacyProfileSuccessorError(
            "legacy Profile has no explicit Pack selection"
        )
    selected: list[str] = []
    for item in raw:
        value = (
            item
            if isinstance(item, str)
            else item.get("pack_id")
            if isinstance(item, Mapping)
            else None
        )
        if not isinstance(value, str) or not value.strip():
            raise LegacyProfileSuccessorError("legacy Pack selection is ambiguous")
        normalized = KNOWN_PACK_ALIASES.get(value.strip(), value.strip())
        if normalized == base_id:
            continue
        if normalized not in selected:
            selected.append(normalized)
    return selected


def _foundational_provider(
    packs: Mapping[str, Mapping[str, Any]],
) -> str:
    """Return the unique locked provider for a conversation turn.

    Legacy ``defaultspack`` selected the Base and did not distinguish its
    foundational runtime provider.  The v4 resolver does, so derive that
    provider from the locked contracts instead of copying the Defaults Profile.
    """

    candidates: list[str] = []
    for pack_id, manifest in packs.items():
        contracts = manifest.get("contracts")
        if not isinstance(contracts, list):
            continue
        if any(
            isinstance(contract, Mapping)
            and contract.get("contract_id") == "conversation.turn.v1"
            and "complete" in (contract.get("operations") or [])
            for contract in contracts
        ):
            candidates.append(str(pack_id))
    if len(candidates) != 1:
        raise LegacyProfileSuccessorError(
            "locked catalog does not contain one foundational conversation provider"
        )
    return candidates[0]


def _add_required_contract_providers(
    selected: list[str],
    packs: Mapping[str, Mapping[str, Any]],
) -> None:
    """Complete non-optional contract dependencies from locked manifests."""

    providers: dict[str, list[str]] = {}
    for pack_id, manifest in packs.items():
        for contract in manifest.get("contracts") or []:
            if isinstance(contract, Mapping) and isinstance(
                contract.get("contract_id"), str
            ):
                providers.setdefault(str(contract["contract_id"]), []).append(
                    str(pack_id)
                )
    while True:
        effective_ids = _pack_dependency_closure(selected, packs)
        provided = {
            str(contract["contract_id"])
            for pack_id in effective_ids
            for contract in packs[pack_id].get("contracts") or []
            if isinstance(contract, Mapping) and contract.get("contract_id")
        }
        missing: list[str] = []
        for pack_id in effective_ids:
            requirements = packs[pack_id].get("requirements")
            if not isinstance(requirements, Mapping):
                raise LegacyProfileSuccessorError(
                    f"locked Pack requirements are invalid: {pack_id}"
                )
            for dependency in requirements.get("contract_dependencies") or []:
                if (
                    isinstance(dependency, Mapping)
                    and dependency.get("optional") is False
                    and dependency.get("contract_id") not in provided
                ):
                    contract_id = str(dependency.get("contract_id") or "")
                    if contract_id and contract_id not in missing:
                        missing.append(contract_id)
        if not missing:
            return
        changed = False
        for contract_id in missing:
            candidates = providers.get(contract_id, [])
            preferred = _LEGACY_CONTRACT_PROVIDERS.get(contract_id)
            if preferred is not None:
                if preferred not in candidates:
                    raise LegacyProfileSuccessorError(
                        f"legacy contract provider is absent: {contract_id}"
                    )
                provider_id = preferred
            elif len(candidates) == 1:
                provider_id = candidates[0]
            else:
                raise LegacyProfileSuccessorError(
                    f"legacy contract provider is ambiguous: {contract_id}"
                )
            if provider_id not in selected:
                selected.append(provider_id)
                changed = True
        if not changed:
            raise LegacyProfileSuccessorError(
                "legacy contract closure could not make progress"
            )


def _pack_dependency_closure(
    selected: list[str],
    packs: Mapping[str, Mapping[str, Any]],
) -> list[str]:
    """Return selected Packs plus their exact manifest dependencies."""

    closure = list(selected)
    pending = list(selected)
    while pending:
        pack_id = pending.pop(0)
        manifest = packs.get(pack_id)
        requirements = manifest.get("requirements") if isinstance(manifest, Mapping) else None
        if not isinstance(requirements, Mapping):
            raise LegacyProfileSuccessorError(
                f"locked Pack requirements are invalid: {pack_id}"
            )
        dependencies = requirements.get("pack_dependencies")
        if not isinstance(dependencies, Mapping):
            raise LegacyProfileSuccessorError(
                f"locked Pack dependency inventory is invalid: {pack_id}"
            )
        for dependency_id in sorted(str(item) for item in dependencies):
            if dependency_id not in packs:
                raise LegacyProfileSuccessorError(
                    f"locked Pack dependency is absent: {dependency_id}"
                )
            if dependency_id not in closure:
                closure.append(dependency_id)
                pending.append(dependency_id)
    return closure


def _verified_presentation(
    *,
    base: Mapping[str, Any],
    shells: Mapping[str, Mapping[str, Any]],
    packs: Mapping[str, Mapping[str, Any]],
) -> tuple[Mapping[str, Any], Mapping[str, Any], str]:
    requirements = base.get("shell_requirements")
    if not isinstance(requirements, Mapping):
        raise LegacyProfileSuccessorError("legacy Base has no Shell requirements")
    families = set(requirements.get("presentation_families") or [])
    capabilities = set(requirements.get("required_capabilities") or [])
    candidates: list[tuple[Mapping[str, Any], Mapping[str, Any], str]] = []
    for shell in shells.values():
        presentation = shell.get("presentation")
        launch = shell.get("launch")
        if (
            shell.get("availability") != "verified"
            or not isinstance(presentation, Mapping)
            or not isinstance(launch, Mapping)
            or presentation.get("family") not in families
            or not capabilities.issubset(set(presentation.get("capabilities") or []))
        ):
            continue
        for variant in launch.get("variants") or []:
            if not isinstance(variant, Mapping):
                continue
            application_ids = _matching_applications(variant, packs)
            if len(application_ids) == 1:
                candidates.append((shell, variant, application_ids[0]))
    if len(candidates) != 1:
        raise LegacyProfileSuccessorError(
            "locked catalog does not contain one verified Shell/Application pair"
        )
    return candidates[0]


def _matching_applications(
    variant: Mapping[str, Any],
    packs: Mapping[str, Mapping[str, Any]],
) -> list[str]:
    expected = (
        variant.get("relative_path"),
        variant.get("artifact_digest"),
        variant.get("entrypoint_digest"),
        f"{variant.get('platform')}-{variant.get('architecture')}",
        variant.get("entrypoint"),
    )
    matches: list[str] = []
    for pack_id, manifest in packs.items():
        pack = manifest.get("pack")
        if not isinstance(pack, Mapping) or pack.get("kind") != "application":
            continue
        executable = [
            item
            for item in manifest.get("artifacts") or []
            if isinstance(item, Mapping) and item.get("kind") == "executable"
        ]
        actual = [
            (
                item.get("path"),
                item.get("digest"),
                item.get("entrypoint_digest"),
                item.get("platform"),
                item.get("entrypoint"),
            )
            for item in executable
        ]
        if actual == [expected]:
            matches.append(str(pack_id))
    return matches


__all__ = [
    "LegacyProfileSuccessorError",
    "build_legacy_profile_successor",
]
