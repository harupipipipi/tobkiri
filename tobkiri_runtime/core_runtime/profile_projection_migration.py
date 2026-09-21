"""One-release migration from declarative Pack IDs to Profile projections."""

from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import Any, Iterable, Mapping

from tobkiri_protocol.canonical import canonical_digest

from .profile_content_projection import resolve_intent_projection


MIGRATION_ID = "retired-declarative-packs-to-profile-projections-v1"
COMPATIBILITY_RELEASE = "v1.10.3"
REMOVE_NO_EARLIER_THAN_RELEASE = "v1.10.4"
SUNSET_AT = "2026-10-31"


@dataclass(frozen=True)
class RetiredPackProjection:
    """Compatibility mapping into a neutral immutable content root."""

    legacy_pack_id: str
    projection_id: str
    kind: str
    artifact_root: str

    def resolved(self) -> dict[str, Any]:
        """Return the exact descriptor embedded into a Profile revision."""

        resolved, _files = resolve_intent_projection(
            {
                "projection_id": self.projection_id,
                "kind": self.kind,
                "artifact_root": self.artifact_root,
                "content_digest": None,
                "source_legacy_pack_id": self.legacy_pack_id,
            }
        )
        return resolved


RETIREMENTS = (
    RetiredPackProjection(
        "rumi_agent_services_pack",
        "tobkiri.profile-content.agent-services.v1",
        "profile_content",
        "profile_projections/agent-services",
    ),
    RetiredPackProjection(
        "rumi_local_agent_pack",
        "tobkiri.profile-content.local-agent.v1",
        "profile_content",
        "profile_projections/local-agent",
    ),
    RetiredPackProjection(
        "rumi_pack_suite_pack",
        "tobkiri.profile-content.pack-suite.v1",
        "profile_content",
        "profile_projections/pack-suite",
    ),
    RetiredPackProjection(
        "rumi_reference_ui_pack",
        "tobkiri.ui-projection.reference.v1",
        "ui_projection",
        "profile_projections/reference-ui",
    ),
)


class ProfileProjectionMigrationError(ValueError):
    """A retired Pack migration receipt or preimage is invalid."""


def migrate_profile_document(
    profile: Mapping[str, Any],
) -> tuple[dict[str, Any], tuple[str, ...]]:
    """Remove retired Pack selections and bind their content to this Profile."""

    mapping = {item.legacy_pack_id: item for item in RETIREMENTS}
    result = copy.deepcopy(dict(profile))
    packs = result.get("packs")
    if not isinstance(packs, list):
        return result, ()
    kept: list[Any] = []
    selected: list[str] = []
    for item in packs:
        pack_id = (
            str(item.get("pack_id") or "")
            if isinstance(item, Mapping)
            else str(item or "")
        )
        if pack_id in mapping:
            selected.append(pack_id)
        else:
            kept.append(item)
    if not selected:
        return result, ()
    current = result.get("content_projections")
    current_items = current if isinstance(current, list) else []
    projections = [
        copy.deepcopy(dict(item))
        for item in current_items
        if isinstance(item, Mapping)
    ]
    present = {str(item.get("projection_id") or "") for item in projections}
    for legacy_id in sorted(set(selected)):
        descriptor = mapping[legacy_id].resolved()
        if descriptor["projection_id"] not in present:
            projections.append(descriptor)
            present.add(str(descriptor["projection_id"]))
    result["packs"] = kept
    result["content_projections"] = sorted(
        projections, key=lambda item: str(item["projection_id"])
    )
    return result, tuple(sorted(set(selected)))


def migrate_pack_control_envelope(
    envelope: Mapping[str, Any],
    *,
    profile_id: str,
    profile_revision: str,
    enabled_pack_ids: Iterable[str],
    approval_digests: Mapping[str, str],
) -> tuple[dict[str, Any], Mapping[str, Any] | None]:
    """Move legacy installed/approval/enable state in one atomic envelope."""

    mapping = {item.legacy_pack_id: item for item in RETIREMENTS}
    state = copy.deepcopy(dict(envelope))
    installed_value = state.get("installed", {})
    if not isinstance(installed_value, Mapping):
        raise ProfileProjectionMigrationError("installed Pack state is invalid")
    installed = copy.deepcopy(dict(installed_value))
    migrations = state.get("migrations", {})
    if not isinstance(migrations, Mapping):
        raise ProfileProjectionMigrationError("Pack migration ledger is invalid")
    existing = migrations.get(MIGRATION_ID)
    retired = sorted(set(installed).intersection(mapping))
    if not retired:
        return state, dict(existing) if isinstance(existing, Mapping) else None

    projections_value = state.get("profile_projections", {})
    if not isinstance(projections_value, Mapping):
        raise ProfileProjectionMigrationError("Profile projection state is invalid")
    projections = copy.deepcopy(dict(projections_value))
    preimage = {
        "installed": {pack_id: installed[pack_id] for pack_id in retired},
        "profile_projections": copy.deepcopy(projections),
    }
    enabled = {str(item) for item in enabled_pack_ids}
    pre_digest = canonical_digest(
        {"profile_id": profile_id, "profile_revision": profile_revision, **preimage}
    )
    approval_count = 0
    enabled_count = 0
    for legacy_id in retired:
        descriptor = mapping[legacy_id].resolved()
        approval_digest = approval_digests.get(legacy_id)
        was_enabled = legacy_id in enabled
        approval_count += int(approval_digest is not None)
        enabled_count += int(was_enabled)
        projections[descriptor["projection_id"]] = {
            **descriptor,
            "source_legacy_pack_id": legacy_id,
            "legacy_install_state_digest": canonical_digest(installed.pop(legacy_id)),
            "legacy_approval_digest": approval_digest,
            "was_enabled": was_enabled,
            "read_only": True,
            "migration_id": MIGRATION_ID,
        }
    postimage = {"installed": installed, "profile_projections": projections}
    post_digest = canonical_digest(
        {"profile_id": profile_id, "profile_revision": profile_revision, **postimage}
    )
    receipt = {
        "schema": "io.tobkiri.pack-projection-migration-receipt.v1",
        "migration_id": MIGRATION_ID,
        "profile_id": profile_id,
        "profile_revision": profile_revision,
        "compatibility_release": COMPATIBILITY_RELEASE,
        "remove_no_earlier_than_release": REMOVE_NO_EARLIER_THAN_RELEASE,
        "sunset_at": SUNSET_AT,
        "migrated_pack_ids": retired,
        "migrated_count": len(retired),
        "approved_count": approval_count,
        "enabled_count": enabled_count,
        "pre_state_digest": pre_digest,
        "post_state_digest": post_digest,
        "preimage": preimage,
        "rollback_digest": canonical_digest(preimage),
    }
    next_migrations = copy.deepcopy(dict(migrations))
    next_migrations[MIGRATION_ID] = receipt
    return (
        {
            **state,
            "version": "io.tobkiri.pack-control-state.v4",
            "profile_id": profile_id,
            **postimage,
            "migrations": next_migrations,
        },
        receipt,
    )


def rollback_pack_control_envelope(
    envelope: Mapping[str, Any],
) -> dict[str, Any]:
    """Restore the exact preimage; aliases remain excluded from Pack authority."""

    result = copy.deepcopy(dict(envelope))
    migrations = result.get("migrations")
    if not isinstance(migrations, Mapping):
        raise ProfileProjectionMigrationError("migration receipt is unavailable")
    receipt = migrations.get(MIGRATION_ID)
    if not isinstance(receipt, Mapping):
        raise ProfileProjectionMigrationError("migration receipt is unavailable")
    preimage = receipt.get("preimage")
    if not isinstance(preimage, Mapping) or canonical_digest(preimage) != receipt.get(
        "rollback_digest"
    ):
        raise ProfileProjectionMigrationError("migration rollback preimage is invalid")
    installed = result.get("installed")
    if not isinstance(installed, Mapping):
        raise ProfileProjectionMigrationError("installed Pack state is invalid")
    restored = copy.deepcopy(dict(installed))
    restored.update(copy.deepcopy(dict(preimage["installed"])))
    result["installed"] = restored
    result["profile_projections"] = copy.deepcopy(preimage["profile_projections"])
    next_migrations = copy.deepcopy(dict(migrations))
    next_migrations.pop(MIGRATION_ID, None)
    result["migrations"] = next_migrations
    return result


__all__ = [
    "COMPATIBILITY_RELEASE",
    "MIGRATION_ID",
    "REMOVE_NO_EARLIER_THAN_RELEASE",
    "RETIREMENTS",
    "SUNSET_AT",
    "migrate_pack_control_envelope",
    "migrate_profile_document",
    "rollback_pack_control_envelope",
]
