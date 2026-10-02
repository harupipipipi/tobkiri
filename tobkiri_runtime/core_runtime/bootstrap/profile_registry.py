"""Register explicitly selected bootstrap definitions without replacing user state."""

from copy import deepcopy
from pathlib import Path
from typing import Any, Mapping

from tobkiri_protocol.canonical import canonical_digest

from ..authority.v4 import AuthorityStore
from ..active_profile_store_v4 import ActiveProfileStore
from ..profile_definition_store_v4 import (
    ProfileDefinitionStore,
    ProfileDefinitionStoreConflict,
)
from .profile_source_update import (
    interrupted_source_update_predecessor,
    profile_scope_successor,
    profile_source_additions,
)


def bootstrap_review_catalog(
    *,
    runtime: Any,
    catalog: Any,
    user_data: Path,
    profile_id: str,
    include_source_additions: bool = False,
) -> tuple[Any, tuple[str, ...]]:
    """Keep the verified active definition when reviewing a packaged Shell update."""
    pointer_path = user_data / "profiles" / "active.json"
    if not pointer_path.exists() and not pointer_path.is_symlink():
        if include_source_additions:
            raise runtime.denied("source update requires an active Profile")
        return catalog, ()
    pointer = ActiveProfileStore(user_data).load(verify_snapshot=True)
    if pointer is None or pointer.profile_id != profile_id:
        if include_source_additions:
            raise runtime.denied("source update requires the active Profile")
        return catalog, ()
    registered = ProfileDefinitionStore(user_data).get_profile(profile_id)
    if registered is None:
        if include_source_additions:
            raise runtime.denied("source update requires a registered Profile")
        return catalog, ()
    workspace = user_data / "workspaces" / profile_id
    successor_required = False
    with AuthorityStore(user_data / "authority" / "v4.sqlite3") as authority:
        try:
            active = runtime.activation_store(
                root=workspace / "activation",
                workspace=workspace,
                profile_id=profile_id,
                authority=authority,
                catalog=catalog,
            ).load_active_snapshot()
        except Exception as error:
            if not runtime.is_reconfirmation_required(error):
                raise
            definition_digest = getattr(error, "verified_profile_definition_digest", None)
            identity = getattr(error, "verified_activation_identity", None)
            active_profile = getattr(error, "verified_profile", None)
            successor_required = True
        else:
            definition_digest = active.resolved.plan["profile_definition_digest"]
            identity = (
                active.resolved.plan["profile_revision"],
                active.activation["activation_id"],
                active.resolved.plan["plan_digest"],
                active.resolved.lock["lock_digest"],
            )
            active_profile = active.resolved.profile
    if not isinstance(definition_digest, str) or identity != (
        pointer.profile_revision,
        pointer.activation_id,
        pointer.plan_digest,
        pointer.lock_digest,
    ):
        raise ProfileDefinitionStoreConflict(
            "bootstrap review does not match the verified active definition"
        )
    if not isinstance(active_profile, Mapping):
        raise runtime.denied("bootstrap review has no verified Pack selection")
    candidate = deepcopy(dict(registered.profile))
    if definition_digest != canonical_digest(registered.profile):
        candidate = interrupted_source_update_predecessor(
            ProfileDefinitionStore(user_data).snapshot(),
            registered.profile,
            definition_digest,
            catalog.profiles[profile_id],
            successor_required=successor_required,
        )
    if successor_required:
        candidate["shell"] = deepcopy(catalog.profiles[profile_id]["shell"])
    if successor_required or include_source_additions:
        candidate = profile_scope_successor(candidate, catalog.profiles[profile_id])
    if include_source_additions:
        try:
            updated = profile_source_additions(candidate, catalog.profiles[profile_id])
        except ProfileDefinitionStoreConflict as error:
            raise runtime.denied("source update conflicts with the registered Profile") from error
        if not successor_required and updated == candidate:
            raise runtime.denied("source update requires reconfirmation")
        candidate = updated
    declared_ids = {item["pack_id"] for item in candidate["packs"]}
    selected_ids = {item["pack_id"] for item in active_profile["packs"]}
    closure_ids = selected_ids | {
        active_profile["base"]["pack_id"],
        active_profile["shell"]["pack_id"],
    }
    if not closure_ids.issubset(catalog.packs):
        from ..pack_control_v4 import catalog_with_admitted_pack_closure

        catalog, _ = catalog_with_admitted_pack_closure(catalog, sorted(closure_ids))
    dependency_ids = {
        dependency
        for pack_id in closure_ids
        for dependency in catalog.packs[pack_id]["requirements"]["pack_dependencies"]
    }
    # Keep optional roots out of the definition: promoting them to mandatory
    # Packs would prevent the user from disabling them after the update.
    additional_ids = tuple(sorted(selected_ids - declared_ids - dependency_ids))
    return (
        runtime.catalog_with_profiles(catalog, {**catalog.profiles, profile_id: candidate}),
        additional_ids,
    )


def register_bootstrap_definition(
    user_data: Path,
    source: Mapping[str, Any],
    *,
    approved_predecessor_digest: str | None = None,
) -> None:
    """Register a source or append its explicitly confirmed predecessor's successor."""
    definitions = ProfileDefinitionStore(user_data)
    generation = definitions.snapshot()["generation"]
    existing = definitions.get_profile(str(source["profile_id"]), include_tombstone=True)
    if existing is None:
        try:
            definitions.create_profile(source, expected_store_generation=generation)
            return
        except ProfileDefinitionStoreConflict:
            existing = definitions.get_profile(str(source["profile_id"]), include_tombstone=True)
            if existing is None:
                raise
    if (
        existing is not None
        and not existing.tombstone
        and dict(existing.profile) != dict(source)
        and approved_predecessor_digest is not None
        and canonical_digest(existing.profile) == approved_predecessor_digest
    ):
        definitions.update_profile(
            existing.profile_id,
            source,
            expected_profile_revision=existing.profile_revision,
            expected_store_generation=generation,
        )
        return
    if existing is None or existing.tombstone or dict(existing.profile) != dict(source):
        raise ProfileDefinitionStoreConflict(
            "bootstrap Profile conflicts with the existing definition"
        )


def recover_bootstrap_definition(
    *, user_data: Path, pointer: Any, runtime: Any, catalog: Any
) -> None:
    """Restore only the exact source of a verified, already committed activation."""
    definitions = ProfileDefinitionStore(user_data)
    if definitions.get_profile(pointer.profile_id, include_tombstone=True) is not None:
        raise ProfileDefinitionStoreConflict(
            "active bootstrap Profile has an unavailable existing definition"
        )
    workspace = user_data / "workspaces" / pointer.profile_id
    with AuthorityStore(user_data / "authority" / "v4.sqlite3") as authority:
        active = runtime.activation_store(
            root=workspace / "activation",
            workspace=workspace,
            profile_id=pointer.profile_id,
            authority=authority,
            catalog=catalog,
        ).load_active_snapshot()
    if (
        active.resolved.plan["profile_revision"],
        active.activation["activation_id"],
        active.resolved.plan["plan_digest"],
        active.resolved.lock["lock_digest"],
    ) != (
        pointer.profile_revision,
        pointer.activation_id,
        pointer.plan_digest,
        pointer.lock_digest,
    ):
        raise runtime.denied("Host active pointer does not match the Profile activation")
    source = catalog.profiles.get(pointer.profile_id)
    if source is None or canonical_digest(source) != active.resolved.plan.get(
        "profile_definition_digest"
    ):
        raise runtime.denied("bootstrap definition differs from its committed source")
    register_bootstrap_definition(user_data, source)


def verify_registered_bootstrap_successor(
    *,
    runtime: Any,
    catalog: Any,
    registered: Mapping[str, Any],
    pointer: Any,
    workspace: Path,
    authority: Any,
) -> None:
    """Surface a verified update of the selected source without changing state."""
    try:
        runtime.activation_store(
            root=workspace / "activation",
            workspace=workspace,
            profile_id=pointer.profile_id,
            authority=authority,
            catalog=catalog,
        ).load_active_snapshot()
    except Exception as error:
        if (
            runtime.is_reconfirmation_required(error)
            and getattr(error, "verified_profile_definition_digest", None)
            == canonical_digest(registered)
            and getattr(error, "verified_activation_identity", None)
            == (
                pointer.profile_revision,
                pointer.activation_id,
                pointer.plan_digest,
                pointer.lock_digest,
            )
        ):
            raise
        # This is only a recovery classification; the caller retains its
        # original denial unless this exact predecessor was verified.
