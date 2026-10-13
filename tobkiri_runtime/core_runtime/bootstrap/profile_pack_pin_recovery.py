"""Recognize one exact registered Pack artifact-pin successor for setup review."""

from __future__ import annotations

from copy import deepcopy
from typing import Any, Mapping

from tobkiri_protocol.canonical import canonical_digest
from tobkiri_protocol.ids import validate_artifact_digest

from ..profile_definition_store_v4 import ProfileDefinitionStoreConflict

PACK_PIN_PREDECESSOR_SCHEMA = "io.tobkiri.defaults-pack-pin-predecessor.v1"
PACK_PIN_PREDECESSOR_KEYS = frozenset(
    {
        "schema",
        "pointer_digest",
        "profile_id",
        "profile_revision",
        "activation_id",
        "plan_digest",
        "lock_digest",
        "generation",
        "active_definition_digest",
        "registry_profile_revision",
        "registry_store_generation",
        "registry_snapshot_digest",
    }
)


def verified_pack_pin_successor_predecessor(
    registry: Mapping[str, Any],
    registered: Mapping[str, Any],
    verified_definition_digest: str,
    catalog: Any,
) -> dict[str, Any]:
    """Return the verified direct predecessor of an exact pin-only successor.

    The registered revision must differ from its parent only in existing
    provider Pack artifact digests, and each changed digest must be the exact
    artifact admitted by the supplied catalog. This helper grants no
    execution authority; callers still resolve and confirm the registered
    candidate through the normal activation path.
    """

    profile_id = registered.get("profile_id")
    current_digest = canonical_digest(registered)
    if not isinstance(profile_id, str):
        raise _conflict()
    for entry in registry.get("profiles", ()):
        if (
            not isinstance(entry, Mapping)
            or entry.get("profile_id") != profile_id
            or entry.get("tombstone") is not False
            or entry.get("current_revision") != current_digest
        ):
            continue
        revisions = entry.get("revisions")
        if not isinstance(revisions, list):
            break
        current_rows = [
            row for row in revisions
            if isinstance(row, Mapping)
            and row.get("profile_revision") == current_digest
        ]
        previous_rows = [
            row for row in revisions
            if isinstance(row, Mapping)
            and row.get("profile_revision") == verified_definition_digest
        ]
        if len(current_rows) != 1 or len(previous_rows) != 1:
            break
        current_row, previous_row = current_rows[0], previous_rows[0]
        previous = previous_row.get("profile")
        if (
            current_row.get("parent_revision") != verified_definition_digest
            or not isinstance(previous, Mapping)
            or canonical_digest(previous) != verified_definition_digest
            or not _only_existing_provider_pins_changed(previous, registered, catalog)
        ):
            break
        return deepcopy(dict(previous))
    raise _conflict()


def pack_pin_predecessor_binding(
    *,
    pointer: Any,
    active_definition_digest: str,
    registry_snapshot: Mapping[str, Any],
    registered: Any,
) -> dict[str, Any]:
    """Bind review to the full active pointer and exact candidate revision."""

    if not isinstance(active_definition_digest, str):
        raise _conflict()
    pointer_payload = pointer.to_dict()
    revision = getattr(registered, "profile_revision", None)
    profile_id = getattr(registered, "profile_id", None)
    generation = registry_snapshot.get("generation")
    if (
        not isinstance(revision, str)
        or not isinstance(profile_id, str)
        or pointer_payload.get("profile_id") != profile_id
        or type(generation) is not int
    ):
        raise _conflict()
    return {
        "schema": PACK_PIN_PREDECESSOR_SCHEMA,
        "pointer_digest": pointer_payload["pointer_digest"],
        "profile_id": profile_id,
        "profile_revision": pointer_payload["profile_revision"],
        "activation_id": pointer_payload["activation_id"],
        "plan_digest": pointer_payload["plan_digest"],
        "lock_digest": pointer_payload["lock_digest"],
        "generation": pointer_payload["generation"],
        "active_definition_digest": active_definition_digest,
        "registry_profile_revision": revision,
        "registry_store_generation": generation,
        "registry_snapshot_digest": canonical_digest(registry_snapshot),
    }


def require_exact_pack_pin_predecessor_binding(
    binding: Mapping[str, Any],
    *,
    pointer: Any,
    active_definition_digest: str,
    registry_snapshot: Mapping[str, Any],
    registered: Any,
    catalog: Any,
) -> None:
    """Reject changed pointer, registry candidate, history, or admitted pins."""

    expected = pack_pin_predecessor_binding(
        pointer=pointer,
        active_definition_digest=active_definition_digest,
        registry_snapshot=registry_snapshot,
        registered=registered,
    )
    if set(binding) != PACK_PIN_PREDECESSOR_KEYS or dict(binding) != expected:
        raise _conflict()
    verified_pack_pin_successor_predecessor(
        registry_snapshot,
        registered.profile,
        active_definition_digest,
        catalog,
    )


def _only_existing_provider_pins_changed(
    previous: Mapping[str, Any], registered: Mapping[str, Any], catalog: Any
) -> bool:
    """Validate same ordered definition with changed selected provider pins."""

    before = deepcopy(dict(previous))
    after = deepcopy(dict(registered))
    before_packs = before.get("packs")
    after_packs = after.get("packs")
    manifests = getattr(catalog, "packs", None)
    if (
        not isinstance(before_packs, list)
        or not isinstance(after_packs, list)
        or len(before_packs) != len(after_packs)
        or not isinstance(manifests, Mapping)
    ):
        return False
    base = previous.get("base")
    shell = previous.get("shell")
    excluded_ids = {
        value.get("pack_id")
        for value in (base, shell)
        if isinstance(value, Mapping) and isinstance(value.get("pack_id"), str)
    }
    changed = False
    for old, new in zip(before_packs, after_packs):
        if not isinstance(old, Mapping) or not isinstance(new, Mapping):
            return False
        if old.get("pack_id") != new.get("pack_id"):
            return False
        old_pin = old.get("artifact_digest")
        new_pin = new.get("artifact_digest")
        old.pop("artifact_digest", None)
        new.pop("artifact_digest", None)
        if dict(old) != dict(new):
            return False
        if old_pin == new_pin:
            continue
        if (
            old.get("role", "provider") != "provider"
            or new.get("role", "provider") != "provider"
            or new.get("pack_id") in excluded_ids
        ):
            return False
        if not isinstance(old_pin, str) or not isinstance(new_pin, str):
            return False
        try:
            validate_artifact_digest(new_pin, field="artifact_digest")
        except (TypeError, ValueError):
            return False
        manifest = manifests.get(str(new.get("pack_id")))
        pack = manifest.get("pack") if isinstance(manifest, Mapping) else None
        if (
            not isinstance(pack, Mapping)
            or pack.get("artifact_digest") != new_pin
            or pack.get("kind") not in {"normal_sandbox", "host_extension"}
        ):
            return False
        changed = True
    before["packs"] = before_packs
    after["packs"] = after_packs
    before.pop("packs")
    after.pop("packs")
    return changed and before == after


def _conflict() -> ProfileDefinitionStoreConflict:
    return ProfileDefinitionStoreConflict(
        "bootstrap review does not match a verified Pack pin update"
    )
