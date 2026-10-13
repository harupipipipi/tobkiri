"""Register explicitly selected bootstrap definitions without replacing user state."""

import time
from copy import deepcopy
from pathlib import Path
from typing import Any, Mapping

from tobkiri_protocol.canonical import canonical_digest
from tobkiri_protocol.secure_persistence import SecureDirectory

from ..authority.v4 import AuthorityStore
from ..active_profile_store_v4 import ActiveProfileStore, exclusive_profile_lock
from ..profile_definition_store_v4 import (
    ProfileDefinitionStore,
    ProfileDefinitionStoreConflict,
)
from .profile_predecessor import require_inactive_bootstrap_predecessor
from .profile_pack_pin_recovery import verified_pack_pin_successor_predecessor
from .profile_pack_pin_recovery import require_exact_pack_pin_predecessor_binding
from .profile_source_update import (
    interrupted_source_update_predecessor,
    profile_binding_renewal_required,
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
    binding_renewal_required = False
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
    selected_ids = {item["pack_id"] for item in active_profile["packs"]}
    closure_ids = selected_ids | {
        active_profile["base"]["pack_id"],
        active_profile["shell"]["pack_id"],
    }
    if not closure_ids.issubset(catalog.packs):
        from ..pack_control_v4 import catalog_with_admitted_pack_closure

        # Renewal must compare the verified active plan against the same
        # admitted catalog used by Pack control, not the bundled subset.
        # This loader still authenticates every external Pack and dependency.
        catalog, _ = catalog_with_admitted_pack_closure(catalog, sorted(closure_ids))
    if not successor_required:
        binding_renewal_required = profile_binding_renewal_required(
            active.resolved.plan, catalog
        )
    candidate = deepcopy(dict(registered.profile))
    pack_pin_recovery = False
    if definition_digest != canonical_digest(registered.profile):
        try:
            candidate = interrupted_source_update_predecessor(
                ProfileDefinitionStore(user_data).snapshot(),
                registered.profile,
                definition_digest,
                catalog.profiles[profile_id],
                successor_required=successor_required,
            )
        except ProfileDefinitionStoreConflict:
            if not include_source_additions:
                raise
            pins = {
                str(row["pack_id"]): str(row["artifact_digest"])
                for row in registered.profile["packs"]
                if row.get("artifact_digest") is not None
            }
            from ..pack_control_v4 import catalog_with_admitted_pack_closure

            catalog, _ = catalog_with_admitted_pack_closure(
                catalog,
                [str(row["pack_id"]) for row in registered.profile["packs"]],
                artifact_pins=pins,
            )
            snapshot = ProfileDefinitionStore(user_data).snapshot()
            verified_pack_pin_successor_predecessor(
                snapshot,
                registered.profile,
                definition_digest,
                catalog,
            )
            candidate = deepcopy(dict(registered.profile))
            pack_pin_recovery = True
    if successor_required and not pack_pin_recovery:
        candidate["shell"] = deepcopy(catalog.profiles[profile_id]["shell"])
    binding_renewal_predecessor = deepcopy(candidate)
    if (successor_required or include_source_additions) and not pack_pin_recovery:
        candidate = profile_scope_successor(candidate, catalog.profiles[profile_id])
    if include_source_additions and not pack_pin_recovery:
        try:
            updated = profile_source_additions(candidate, catalog.profiles[profile_id])
        except ProfileDefinitionStoreConflict as error:
            raise runtime.denied("source update conflicts with the registered Profile") from error
        if (
            not successor_required
            and updated == binding_renewal_predecessor
            and not binding_renewal_required
        ):
            raise runtime.denied("source update requires reconfirmation")
        candidate = updated
    declared_ids = {item["pack_id"] for item in candidate["packs"]}
    dependency_ids = {
        dependency
        for pack_id in closure_ids
        for dependency in catalog.packs[pack_id]["requirements"]["pack_dependencies"]
    }
    # Keep optional roots out of the definition: promoting them to mandatory
    # Packs would prevent the user from disabling them after the update.
    additional_ids = tuple(sorted(selected_ids - declared_ids - dependency_ids))
    if additional_ids:
        from ..pack_control_v4 import verified_reconfirmed_pack_ids

        # A preserved optional Pack is carried only while its install and
        # approval receipts still verify.  Excluding the unverifiable rest
        # keeps the upgrade path reachable; stale approvals stay invalid
        # and must be re-approved before such a Pack can be enabled again.
        additional_ids = verified_reconfirmed_pack_ids(
            profile_id, additional_ids, catalog
        )
    return (
        runtime.catalog_with_profiles(catalog, {**catalog.profiles, profile_id: candidate}),
        additional_ids,
    )


def register_bootstrap_definition(
    user_data: Path,
    source: Mapping[str, Any],
    *,
    approved_predecessor_digest: str | None = None,
    approved_inactive_predecessor: Mapping[str, Any] | None = None,
    approved_pack_pin_predecessor: Mapping[str, Any] | None = None,
    admitted_catalog: Any | None = None,
) -> None:
    """Register a source or append its explicitly confirmed predecessor's successor."""
    definitions = ProfileDefinitionStore(user_data)
    if approved_pack_pin_predecessor is not None:
        if admitted_catalog is None:
            raise ProfileDefinitionStoreConflict(
                "Pack pin confirmation has no admitted catalog"
            )
        _require_pack_pin_predecessor_current(
            definitions=definitions,
            user_data=user_data,
            catalog=admitted_catalog,
            source=source,
            binding=approved_pack_pin_predecessor,
        )
        return
    if approved_inactive_predecessor is not None:
        # The caller holds the application's workspace activation lock. This
        # shared Host selection lock excludes another Profile publication while
        # the reviewed inactive definition is appended under registry CAS.
        with exclusive_profile_lock(
            SecureDirectory(user_data / "profiles", create=False),
            ".active-profile.lock", timeout_seconds=5.0,
            monotonic_clock=time.monotonic, retry_sleep=time.sleep,
        ):
            snapshot = definitions.snapshot()
            require_inactive_bootstrap_predecessor(
                user_data=user_data, source=source,
                expected=approved_inactive_predecessor, snapshot=snapshot,
            )
            if (
                approved_predecessor_digest
                != approved_inactive_predecessor["definition_digest"]
            ):
                raise ProfileDefinitionStoreConflict(
                    "inactive bootstrap predecessor approval is mismatched"
                )
            definitions.update_profile(
                str(source["profile_id"]), source,
                expected_profile_revision=approved_inactive_predecessor[
                    "profile_revision"
                ],
                expected_store_generation=approved_inactive_predecessor[
                    "store_generation"
                ],
            )
        return
    snapshot = definitions.snapshot()
    generation = snapshot["generation"]
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
        and _is_unmodified_first_activation_template(
            user_data=user_data,
            snapshot=snapshot,
            profile_id=existing.profile_id,
            profile_revision=existing.profile_revision,
        )
    ):
        definitions.update_profile(
            existing.profile_id,
            source,
            expected_profile_revision=existing.profile_revision,
            expected_store_generation=generation,
        )
        return
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


def require_bootstrap_pack_pin_predecessor_current(
    *, user_data: Path, catalog: Any, binding: Mapping[str, Any]
) -> None:
    """CAS-check the reviewed pin-only candidate and its active predecessor."""

    _require_pack_pin_predecessor_current(
        definitions=ProfileDefinitionStore(user_data),
        user_data=user_data,
        catalog=catalog,
        source=None,
        binding=binding,
    )


def _require_pack_pin_predecessor_current(
    *,
    definitions: ProfileDefinitionStore,
    user_data: Path,
    catalog: Any,
    source: Mapping[str, Any] | None,
    binding: Mapping[str, Any],
) -> None:
    """Recheck the entire reviewed binding at the registry no-op CAS boundary."""

    with exclusive_profile_lock(
        SecureDirectory(user_data / "profiles", create=False),
        ".active-profile.lock",
        timeout_seconds=5.0,
        monotonic_clock=time.monotonic,
        retry_sleep=time.sleep,
    ):
        snapshot = definitions.snapshot()
        profile_id = str(binding.get("profile_id", ""))
        registered = definitions.get_profile(profile_id)
        pointer = ActiveProfileStore(user_data).load(verify_snapshot=True)
        if registered is None or registered.tombstone or pointer is None:
            raise ProfileDefinitionStoreConflict(
                "Pack pin review predecessor is no longer current"
            )
        if source is not None and dict(registered.profile) != dict(source):
            raise ProfileDefinitionStoreConflict(
                "Pack pin candidate changed after review"
            )
        require_exact_pack_pin_predecessor_binding(
            binding,
            pointer=pointer,
            active_definition_digest=str(binding.get("active_definition_digest", "")),
            registry_snapshot=snapshot,
            registered=registered,
            catalog=catalog,
        )
        definitions.require_current_profile(
            profile_id,
            expected_profile_revision=str(binding["registry_profile_revision"]),
            expected_store_generation=int(binding["registry_store_generation"]),
        )


def _is_unmodified_first_activation_template(
    *,
    user_data: Path,
    snapshot: Mapping[str, Any],
    profile_id: str,
    profile_revision: str,
) -> bool:
    """Return whether a packaged template remains safe to supersede once.

    The template is only staging data for the setup ceremony.  The exact source
    selected by that ceremony may differ in build provenance and Shell artifact
    digests, so the first activation may append it as the template's successor.
    Any edit, tombstone, or active pointer makes the registry authoritative and
    keeps the normal conflict behavior.
    """
    bootstrap = snapshot.get("bootstrap")
    if not isinstance(bootstrap, Mapping) or bootstrap != {
        "state": "template_available",
        "template_profile_revision": profile_revision,
    }:
        return False
    entries = [
        entry
        for entry in snapshot.get("profiles", ())
        if isinstance(entry, Mapping) and entry.get("profile_id") == profile_id
    ]
    if len(entries) != 1:
        return False
    entry = entries[0]
    revisions = entry.get("revisions")
    if (
        entry.get("tombstone") is not False
        or entry.get("current_revision") != profile_revision
        or not isinstance(revisions, list)
        or len(revisions) != 1
        or not isinstance(revisions[0], Mapping)
        or revisions[0].get("profile_revision") != profile_revision
        or revisions[0].get("tombstone") is not False
    ):
        return False
    active_paths = (
        user_data / "profiles" / "active.json",
        user_data / "workspaces" / profile_id / "activation" / "active.json",
    )
    return not any(path.exists() or path.is_symlink() for path in active_paths)


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
