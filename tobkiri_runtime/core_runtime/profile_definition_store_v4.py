"""Host-owned, immutable Profile definition storage.

Packaged catalog documents are inputs to the Host, not the user's Profile
registry.  This module provides the small durable registry that was missing in
the v4 cutover: every update creates a new content-addressed revision and a
delete leaves a tombstone.  The registry is intentionally independent of
``workspaces/<profile_id>`` so no Profile workspace can become the authority
for all other Profiles.
"""

from __future__ import annotations

import copy
import hashlib
import os
import re
import stat
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping, Sequence

from tobkiri_protocol.canonical import canonical_digest, canonical_json, strict_loads
from tobkiri_protocol.ids import validate_artifact_digest, validate_canonical_id
from tobkiri_protocol.secure_persistence import (
    SecureDirectory,
    SecurePersistenceError,
)
from tobkiri_protocol.platform_paths import canonical_platform_path

from .active_profile_store_v4 import exclusive_profile_lock

PROFILE_STORE_SCHEMA = "io.tobkiri.profile-definition-store.v1"
PROFILE_STORE_FILENAME = "index.json"
_LEGACY_LOCALE_RE = re.compile(r"^[A-Za-z]{2,8}(?:[-_][A-Za-z0-9]{1,8})*$")
_DESCRIPTOR_OPERATIONS_AVAILABLE = (
    os.name != "nt"
    and bool(getattr(os, "O_NOFOLLOW", 0))
    and hasattr(os, "supports_dir_fd")
    and all(
        getattr(os, name, None) in os.supports_dir_fd
        for name in ("open", "mkdir", "stat", "rename", "unlink", "rmdir")
    )
)


class ProfileDefinitionStoreError(RuntimeError):
    """Base error for authoritative Profile registry operations."""


class ProfileDefinitionStoreIntegrityError(ProfileDefinitionStoreError):
    """Raised when the registry bytes or an entry are malformed."""


class ProfileDefinitionStoreConflict(ProfileDefinitionStoreError):
    """Raised when a mutation uses a stale Profile/store revision."""


class ProfileDefinitionNotFound(ProfileDefinitionStoreError):
    """Raised when a requested live Profile does not exist."""


@dataclass(frozen=True)
class StoredProfile:
    """One current Profile view from the authoritative registry."""

    profile_id: str
    profile_revision: str
    profile: Mapping[str, Any]
    order: int
    parent_revision: str | None
    tombstone: bool
    created_at: int
    updated_at: int
    legacy_ids: tuple[str, ...] = ()

    @property
    def display_name(self) -> str:
        """Return the user-facing name without changing the definition."""

        return str(self.profile.get("display_name") or self.profile_id)

    def to_dict(self) -> dict[str, Any]:
        """Return a detached record suitable for an API response."""

        return {
            "profile_id": self.profile_id,
            "profile_revision": self.profile_revision,
            "profile": copy.deepcopy(dict(self.profile)),
            "order": self.order,
            "parent_revision": self.parent_revision,
            "tombstone": self.tombstone,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "legacy_ids": list(self.legacy_ids),
        }


@dataclass(frozen=True)
class LegacyMigrationResult:
    """Receipt for a lossless legacy collection import."""

    source_digest: str
    profile_ids: tuple[str, ...]
    legacy_id_map: Mapping[str, str]
    active_profile_id: str | None
    last_launched_profile_id: str | None
    copied_workspaces: Mapping[str, str]

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-compatible migration receipt."""

        return {
            "source_digest": self.source_digest,
            "profile_ids": list(self.profile_ids),
            "legacy_id_map": dict(self.legacy_id_map),
            "active_profile_id": self.active_profile_id,
            "last_launched_profile_id": self.last_launched_profile_id,
            "copied_workspaces": dict(self.copied_workspaces),
        }


@dataclass(frozen=True)
class _StagedLegacyWorkspace:
    """One staged workspace whose names are safe descriptor-relative names."""

    candidate_id: str
    staged_identity: tuple[int, int]
    destination_path: Path


@dataclass
class _LegacyWorkspaceStage:
    """Pinned descriptors kept alive from staging through publication."""

    source_profiles_descriptor: int
    source_profiles_path: Path
    source_profiles_identity: tuple[int, int]
    user_data_descriptor: int
    user_data_path: Path
    workspaces_descriptor: int
    workspaces_path: Path
    workspaces_identity: tuple[int, int]
    workspaces_created: bool
    staging_descriptor: int
    staging_name: str
    staging_path: Path
    staging_identity: tuple[int, int]
    workspaces: list[_StagedLegacyWorkspace]
    closed: bool = False


class ProfileDefinitionStore:
    """Persist the Host-global Profile definition collection.

    ``user_data_root`` normally points at the runtime's ``user_data`` folder;
    definitions are stored in ``<root>/profiles/index.json``.  Passing a path
    whose final component is ``profiles`` is also supported for focused tests
    and migration tools.
    """

    def __init__(
        self,
        user_data_root: Path,
        *,
        lock_timeout_seconds: float = 5.0,
        monotonic_clock: Callable[[], float] = time.monotonic,
        retry_sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.time,
    ) -> None:
        requested = Path(user_data_root)
        if requested.is_symlink():
            raise ProfileDefinitionStoreIntegrityError(
                "Profile store root must not be a symlink"
            )
        requested = requested.absolute()
        if requested.name == "profiles":
            self.store_root = requested
            self.user_data_root = requested.parent
        else:
            self.user_data_root = requested
            self.store_root = requested / "profiles"
        if self.store_root.is_symlink():
            raise ProfileDefinitionStoreIntegrityError(
                "Profile store directory must not be a symlink"
            )
        self.index_path = self.store_root / PROFILE_STORE_FILENAME
        self.storage_path = self.index_path
        self._directory = SecureDirectory(self.store_root, create=True)
        self._lock_timeout_seconds = lock_timeout_seconds
        self._monotonic_clock = monotonic_clock
        self._retry_sleep = retry_sleep
        self._clock = clock

    @property
    def path(self) -> Path:
        """Return the canonical registry file path."""

        return self.index_path

    def exists(self) -> bool:
        """Return whether the authoritative registry has been initialized."""

        try:
            return self._directory.exists(PROFILE_STORE_FILENAME)
        except FileNotFoundError:
            return False

    def snapshot(self) -> dict[str, Any]:
        """Read the complete verified registry envelope."""

        with self._locked():
            return self._read_state()

    def bootstrap_state(self) -> Mapping[str, Any]:
        """Return generic Host metadata for the install bootstrap template."""

        return dict(self.snapshot()["bootstrap"])

    def list_profiles(
        self, *, include_tombstones: bool = False
    ) -> tuple[StoredProfile, ...]:
        """Return current Profiles in their persisted user order."""

        state = self.snapshot()
        result = [
            self._stored_from_entry(entry)
            for entry in state["profiles"]
            if include_tombstones or not entry["tombstone"]
        ]
        result.sort(key=lambda item: (item.order, item.profile_id))
        return tuple(result)

    def list_profile_payloads(
        self, *, include_tombstones: bool = False
    ) -> list[dict[str, Any]]:
        """Return detached mapping payloads for API/UI adapters."""

        return [
            item.to_dict()
            for item in self.list_profiles(include_tombstones=include_tombstones)
        ]

    def search(
        self,
        query: str = "",
        *,
        include_tombstones: bool = False,
    ) -> tuple[StoredProfile, ...]:
        """Search IDs/names while preserving authoritative ordering."""

        needle = str(query or "").strip().casefold()
        return tuple(
            profile
            for profile in self.list_profiles(include_tombstones=include_tombstones)
            if not needle
            or needle in profile.profile_id.casefold()
            or needle in profile.display_name.casefold()
        )

    def get_profile(
        self,
        profile_id: str,
        *,
        include_tombstone: bool = False,
    ) -> StoredProfile | None:
        """Read one current Profile without accepting workspace authority."""

        safe_id = _safe_profile_id(profile_id)
        state = self.snapshot()
        for entry in state["profiles"]:
            if entry["profile_id"] == safe_id:
                if entry["tombstone"] and not include_tombstone:
                    return None
                return self._stored_from_entry(entry)
        return None

    def create_profile(
        self,
        profile: Mapping[str, Any],
        *,
        profile_id: str | None = None,
        display_name: str | None = None,
        expected_store_generation: int | None = None,
        _bootstrap_template: bool = False,
    ) -> StoredProfile:
        """Create one Profile as its first immutable revision."""

        candidate = _profile_document(
            profile, profile_id=profile_id, display_name=display_name
        )
        safe_id = _safe_profile_id(str(candidate["profile_id"]))
        now = self._now()
        with self._locked():
            state = self._read_state()
            self._check_generation(state, expected_store_generation)
            if any(entry["profile_id"] == safe_id for entry in state["profiles"]):
                raise ProfileDefinitionStoreConflict(
                    f"Profile '{safe_id}' already exists, including tombstones"
                )
            revision = canonical_digest(candidate)
            entry = _new_entry(
                safe_id,
                candidate,
                revision=revision,
                order=len(state["profiles"]),
                now=now,
            )
            state["profiles"].append(entry)
            state["bootstrap"] = {
                "state": (
                    "template_available" if _bootstrap_template else "not_required"
                ),
                "template_profile_revision": revision if _bootstrap_template else None,
            }
            state["generation"] += 1
            state["updated_at"] = now
            self._write_state(state)
            return self._stored_from_entry(entry)

    def bootstrap_defaults(
        self,
        template: Mapping[str, Any],
        *,
        expected_store_generation: int | None = None,
    ) -> StoredProfile:
        """Copy the packaged Defaults template once into the normal registry."""

        template_id = _safe_profile_id(str(template.get("profile_id") or ""))
        existing = self.get_profile(template_id, include_tombstone=True)
        if existing is not None:
            return existing
        return self.create_profile(
            template,
            profile_id=template_id,
            expected_store_generation=expected_store_generation,
            _bootstrap_template=True,
        )

    def repair_legacy_display_names(self) -> int:
        """Append canonical successors for legacy localized display names.

        Historical revisions remain byte-for-byte represented in the immutable
        chain.  Each repaired successor keeps the complete localized mapping in
        ``legacy_display_name`` while exposing one deterministic string in
        ``display_name``.  All successors are published in one atomic registry
        transaction, and a second call is a no-op.

        Returns:
            Number of live Profile entries repaired by the transaction.
        """

        with self._locked():
            state = self._read_state()
            repaired: list[tuple[dict[str, Any], dict[str, Any], str]] = []
            for entry in state["profiles"]:
                if entry["tombstone"]:
                    continue
                current = entry["revisions"][-1]
                profile = current.get("profile")
                if not isinstance(profile, Mapping):
                    raise ProfileDefinitionStoreIntegrityError(
                        "live Profile revision document is invalid"
                    )
                normalized = _normalize_legacy_display_name(profile)
                if normalized == profile:
                    continue
                repaired.append(
                    (
                        entry,
                        normalized,
                        str(current["profile_revision"]),
                    )
                )
            if not repaired:
                return 0

            now = self._now()
            for entry, normalized, parent_revision in repaired:
                revision = canonical_digest(normalized)
                entry["current_revision"] = revision
                entry["revisions"].append(
                    _revision_record(
                        normalized,
                        revision=revision,
                        parent_revision=parent_revision,
                        now=now,
                    )
                )
                entry["updated_at"] = now
            state["bootstrap"] = {
                "state": "not_required",
                "template_profile_revision": None,
            }
            state["generation"] += len(repaired)
            state["updated_at"] = now
            self._write_state(state)
            return len(repaired)

    def migrate_retired_pack_projections(self) -> int:
        """Atomically append neutral projection successors for retired Packs."""

        from .profile_projection_migration import (
            COMPATIBILITY_RELEASE,
            MIGRATION_ID,
            REMOVE_NO_EARLIER_THAN_RELEASE,
            SUNSET_AT,
            migrate_profile_document,
        )

        with self._locked():
            state = self._read_state()
            prepared: list[
                tuple[dict[str, Any], dict[str, Any], tuple[str, ...], str]
            ] = []
            for entry in state["profiles"]:
                if entry["tombstone"]:
                    continue
                current = entry["revisions"][-1]
                profile = current.get("profile")
                if not isinstance(profile, Mapping):
                    raise ProfileDefinitionStoreIntegrityError(
                        "live Profile revision document is invalid"
                    )
                migrated, legacy_ids = migrate_profile_document(profile)
                if legacy_ids:
                    prepared.append(
                        (
                            entry,
                            migrated,
                            legacy_ids,
                            str(current["profile_revision"]),
                        )
                    )
            if not prepared:
                return 0

            now = self._now()
            receipts: list[dict[str, Any]] = []
            for entry, migrated, legacy_ids, prior_revision in prepared:
                pre_digest = canonical_digest(entry["revisions"][-1]["profile"])
                _append_profile_successor(entry, migrated, now=now)
                receipts.append(
                    {
                        "migration_id": MIGRATION_ID,
                        "profile_id": entry["profile_id"],
                        "profile_revision": entry["current_revision"],
                        "prior_profile_revision": prior_revision,
                        "migrated_pack_ids": list(legacy_ids),
                        "migrated_count": len(legacy_ids),
                        "pre_state_digest": pre_digest,
                        "post_state_digest": canonical_digest(migrated),
                        "rollback_digest": pre_digest,
                    }
                )
            legacy = copy.deepcopy(dict(state["legacy"]))
            migrations = copy.deepcopy(dict(legacy.get("projection_migrations") or {}))
            migrations[MIGRATION_ID] = {
                "migration_id": MIGRATION_ID,
                "compatibility_release": COMPATIBILITY_RELEASE,
                "remove_no_earlier_than_release": REMOVE_NO_EARLIER_THAN_RELEASE,
                "sunset_at": SUNSET_AT,
                "profile_count": len(receipts),
                "migrated_count": sum(item["migrated_count"] for item in receipts),
                "receipts": receipts,
            }
            legacy["projection_migrations"] = migrations
            state["legacy"] = legacy
            state["generation"] += len(prepared)
            state["updated_at"] = now
            self._write_state(state)
            return len(prepared)

    def update_profile(
        self,
        profile_id: str,
        profile: Mapping[str, Any] | None = None,
        *,
        patch: Mapping[str, Any] | None = None,
        expected_profile_revision: str | None = None,
        expected_store_generation: int | None = None,
        display_name: str | None = None,
    ) -> StoredProfile:
        """Append an immutable successor revision for one live Profile."""

        safe_id = _safe_profile_id(profile_id)
        if profile is not None and patch is not None:
            raise ProfileDefinitionStoreError(
                "profile and patch cannot both be supplied"
            )
        with self._locked():
            state = self._read_state()
            self._check_generation(state, expected_store_generation)
            entry = self._entry_for_id(state, safe_id)
            if entry is None or entry["tombstone"]:
                raise ProfileDefinitionNotFound(safe_id)
            current = self._stored_from_entry(entry)
            self._check_profile_revision(current, expected_profile_revision)
            if profile is None:
                candidate = copy.deepcopy(dict(current.profile))
                if patch:
                    candidate.update(copy.deepcopy(dict(patch)))
            else:
                candidate = copy.deepcopy(dict(profile))
            candidate["profile_id"] = safe_id
            if display_name is not None:
                candidate["display_name"] = str(display_name)
            candidate = _profile_document(candidate, profile_id=safe_id)
            revision = canonical_digest(candidate)
            now = self._now()
            successor = _revision_record(
                candidate,
                revision=revision,
                parent_revision=current.profile_revision,
                now=now,
            )
            entry["current_revision"] = revision
            entry["revisions"].append(successor)
            entry["updated_at"] = now
            state["bootstrap"] = {
                "state": "not_required",
                "template_profile_revision": None,
            }
            state["generation"] += 1
            state["updated_at"] = now
            self._write_state(state)
            return self._stored_from_entry(entry)

    def duplicate_profile(
        self,
        profile_id: str,
        *,
        new_profile_id: str | None = None,
        display_name: str | None = None,
        expected_profile_revision: str | None = None,
        expected_store_generation: int | None = None,
    ) -> StoredProfile:
        """Create a distinct Profile from an immutable source revision."""

        safe_id = _safe_profile_id(profile_id)
        with self._locked():
            state = self._read_state()
            self._check_generation(state, expected_store_generation)
            entry = self._entry_for_id(state, safe_id)
            if entry is None or entry["tombstone"]:
                raise ProfileDefinitionNotFound(safe_id)
            current = self._stored_from_entry(entry)
            self._check_profile_revision(current, expected_profile_revision)
            requested_id = new_profile_id or _next_copy_id(state, safe_id)
            destination_id = _safe_profile_id(requested_id)
            if self._entry_for_id(state, destination_id) is not None:
                raise ProfileDefinitionStoreConflict(
                    f"Profile '{destination_id}' already exists, including tombstones"
                )
            candidate = copy.deepcopy(dict(current.profile))
            candidate["profile_id"] = destination_id
            if display_name is not None:
                candidate["display_name"] = str(display_name)
            elif candidate.get("display_name"):
                candidate["display_name"] = f"{candidate['display_name']} Copy"
            candidate = _profile_document(candidate, profile_id=destination_id)
            now = self._now()
            revision = canonical_digest(candidate)
            new_entry = _new_entry(
                destination_id,
                candidate,
                revision=revision,
                order=len(state["profiles"]),
                now=now,
                legacy_ids=tuple(current.legacy_ids),
            )
            state["profiles"].append(new_entry)
            state["bootstrap"] = {
                "state": "not_required",
                "template_profile_revision": None,
            }
            state["generation"] += 1
            state["updated_at"] = now
            self._write_state(state)
            return self._stored_from_entry(new_entry)

    def delete_profile(
        self,
        profile_id: str,
        *,
        expected_profile_revision: str | None = None,
        expected_store_generation: int | None = None,
    ) -> StoredProfile:
        """Append a tombstone successor while retaining every prior revision."""

        safe_id = _safe_profile_id(profile_id)
        with self._locked():
            state = self._read_state()
            self._check_generation(state, expected_store_generation)
            entry = self._entry_for_id(state, safe_id)
            if entry is None or entry["tombstone"]:
                raise ProfileDefinitionNotFound(safe_id)
            current = self._stored_from_entry(entry)
            self._check_profile_revision(current, expected_profile_revision)
            now = self._now()
            tombstone_revision = canonical_digest(
                {
                    "schema": "io.tobkiri.profile-tombstone.v1",
                    "profile_id": safe_id,
                    "parent_revision": current.profile_revision,
                }
            )
            entry["current_revision"] = tombstone_revision
            entry["tombstone"] = True
            entry["updated_at"] = now
            entry["revisions"].append(
                {
                    "profile_revision": tombstone_revision,
                    "parent_revision": current.profile_revision,
                    "profile": None,
                    "created_at": now,
                    "updated_at": now,
                    "tombstone": True,
                }
            )
            state["generation"] += 1
            state["updated_at"] = now
            state["legacy"]["tombstones"] = sorted(
                {*state["legacy"].get("tombstones", []), safe_id}
            )
            if state["bootstrap"].get("state") == "template_available":
                state["bootstrap"] = {
                    "state": "empty",
                    "template_profile_revision": None,
                }
            self._write_state(state)
            return self._stored_from_entry(entry)

    def import_legacy_collection(
        self,
        source: Mapping[str, Any] | Path,
        *,
        legacy_workspace_root: Path | None = None,
        copy_workspaces: bool = True,
        migration_catalog: object | None = None,
        expected_store_generation: int | None = None,
    ) -> LegacyMigrationResult:
        """Import every legacy startup Profile without collapsing the collection.

        The legacy document remains traceable through the store's migration
        receipt.  Unsupported legacy IDs are mapped deterministically to a
        canonical ID while their original values are preserved in
        ``legacy_ids``.  Workspace copying is conservative: symlinks and
        existing destinations are rejected rather than overwritten.
        """

        source_path: Path | None = None
        if isinstance(source, Path):
            source_path = source
            try:
                raw = source.read_bytes()
                legacy = strict_loads(raw)
            except Exception as error:
                raise ProfileDefinitionStoreIntegrityError(
                    "legacy Profile collection is unreadable"
                ) from error
        else:
            legacy = copy.deepcopy(dict(source))
        if not isinstance(legacy, Mapping):
            raise ProfileDefinitionStoreIntegrityError(
                "legacy Profile collection is invalid"
            )
        profiles_value = legacy.get("profiles")
        if isinstance(profiles_value, Mapping):
            profiles = []
            for legacy_key, value in profiles_value.items():
                if not isinstance(value, Mapping):
                    raise ProfileDefinitionStoreIntegrityError(
                        "legacy Profile collection contains a non-object entry"
                    )
                _safe_legacy_workspace_id(str(legacy_key))
                profile = dict(value)
                if not profile.get("profile_id") and not profile.get("id"):
                    profile["profile_id"] = str(legacy_key)
                profiles.append(profile)
        elif isinstance(profiles_value, list):
            profiles = [
                dict(value) for value in profiles_value if isinstance(value, Mapping)
            ]
            if len(profiles) != len(profiles_value):
                raise ProfileDefinitionStoreIntegrityError(
                    "legacy Profile collection contains a non-object entry"
                )
        else:
            raise ProfileDefinitionStoreIntegrityError(
                "legacy Profile collection has no profiles array"
            )
        if not profiles:
            raise ProfileDefinitionStoreIntegrityError(
                "legacy Profile collection is empty"
            )

        # Validate the source identity before any canonicalization, ID mapping,
        # workspace-root lookup, or early return can discard path semantics.
        _validate_legacy_workspace_ids(profiles)

        source_digest = canonical_digest(legacy)
        prepared: list[tuple[str, str, dict[str, Any], int, int]] = []
        id_map: dict[str, str] = {}
        used: set[str] = set()
        for index, item in enumerate(profiles):
            legacy_id = str(item.get("profile_id") or item.get("id") or "").strip()
            if not legacy_id:
                legacy_id = f"legacy-profile-{index + 1}"
            candidate_id = _canonical_migration_id(legacy_id, used)
            used.add(candidate_id)
            id_map[legacy_id] = candidate_id
            document = copy.deepcopy(item)
            document["profile_id"] = candidate_id
            if "display_name" not in document and document.get("name"):
                document["display_name"] = document["name"]
            document = _normalize_legacy_display_name(document)
            now = self._legacy_timestamp(item)
            updated = self._legacy_timestamp(item, key="updated_at", fallback=now)
            prepared.append((legacy_id, candidate_id, document, now, updated))

        legacy_active = _legacy_ref(legacy.get("active_profile_id"), id_map)
        legacy_last = _legacy_ref(legacy.get("last_launched_profile_id"), id_map)
        source_root = (
            Path(legacy_workspace_root)
            if legacy_workspace_root
            else _guess_workspace_root(source_path)
        )
        stage = self._stage_legacy_workspaces(
            prepared,
            source_root=source_root,
            source_digest=source_digest,
            copy_workspaces=copy_workspaces,
        )
        copied: dict[str, str] = {}
        with self._migration_lock(stage):
            previous_exists = self._directory.exists(PROFILE_STORE_FILENAME)
            previous_raw = (
                self._directory.read_bytes(PROFILE_STORE_FILENAME)
                if previous_exists
                else None
            )
            state = self._read_state()
            self._check_generation(state, expected_store_generation)
            existing_ids = {entry["profile_id"] for entry in state["profiles"]}
            prepared_ids = {item[1] for item in prepared}
            if existing_ids.intersection(prepared_ids):
                raise ProfileDefinitionStoreConflict(
                    "legacy import would overwrite an existing Profile"
                )
            order_offset = (
                max(
                    (int(entry["order"]) for entry in state["profiles"]),
                    default=-1,
                )
                + 1
            )
            publication_time = self._now()
            for offset, (
                legacy_id,
                candidate_id,
                document,
                created,
                updated,
            ) in enumerate(prepared):
                document["legacy_migration"] = {
                    "source_digest": source_digest,
                    "legacy_version": str(legacy.get("version") or "3"),
                    "classification": "legacy_profile",
                    "requires_review": True,
                    "dropped_fields": [],
                    "legacy_ids": [legacy_id],
                    "command_digest": None,
                }
                revision = canonical_digest(document)
                entry = _new_entry(
                    candidate_id,
                    document,
                    revision=revision,
                    order=order_offset + offset,
                    now=created,
                    updated_at=updated,
                    legacy_ids=(legacy_id,),
                )
                if migration_catalog is not None:
                    successor = self._legacy_v4_successor(
                        item=profiles[offset],
                        profile_id=candidate_id,
                        catalog=migration_catalog,
                        source_path=(
                            str(source_path)
                            if source_path
                            else "legacy/startup_profiles.json"
                        ),
                    )
                    _append_profile_successor(
                        entry,
                        successor,
                        now=publication_time,
                    )
                state["profiles"].append(entry)
            state["bootstrap"] = {
                "state": "not_required",
                "template_profile_revision": None,
            }
            state["generation"] += len(prepared)
            state["updated_at"] = publication_time
            state["legacy"].update(
                {
                    "source_digest": source_digest,
                    "source_path": str(source_path) if source_path else None,
                    "active_profile_id": legacy_active,
                    "last_launched_profile_id": legacy_last,
                    "legacy_id_map": dict(id_map),
                    "tombstones": state["legacy"].get("tombstones", []),
                    "source_document": copy.deepcopy(dict(legacy)),
                }
            )
            published: list[_StagedLegacyWorkspace] = []
            try:
                if stage is not None:
                    self._assert_legacy_stage_paths(stage)
                if stage is not None:
                    for workspace in stage.workspaces:
                        self._publish_staged_workspace(stage, workspace)
                        published.append(workspace)
                        copied[workspace.candidate_id] = str(workspace.destination_path)
                    self._assert_legacy_stage_paths(stage)
                    self._remove_staging_root(stage)
                self._write_state(state)
            except Exception as error:
                rollback_error: Exception | None = None
                for workspace in reversed(published):
                    try:
                        if stage is None:
                            raise ProfileDefinitionStoreError(
                                "legacy workspace stage disappeared during rollback"
                            )
                        _remove_tree_at(
                            stage.workspaces_descriptor,
                            workspace.candidate_id,
                            missing_ok=True,
                        )
                    except Exception as cleanup_error:
                        rollback_error = cleanup_error
                    copied.pop(workspace.candidate_id, None)
                if rollback_error is not None:
                    raise ProfileDefinitionStoreError(
                        "legacy migration failed and workspace rollback failed"
                    ) from rollback_error
                try:
                    if previous_exists and previous_raw is not None:
                        self._directory.write_bytes_atomic(
                            PROFILE_STORE_FILENAME,
                            previous_raw,
                        )
                    else:
                        self._directory.unlink(PROFILE_STORE_FILENAME, missing_ok=True)
                except (OSError, SecurePersistenceError) as rollback_registry_error:
                    raise ProfileDefinitionStoreError(
                        "legacy migration failed and registry rollback failed"
                    ) from rollback_registry_error
                raise ProfileDefinitionStoreError(
                    "legacy workspace publication failed; migration was rolled back"
                ) from error

        return LegacyMigrationResult(
            source_digest=source_digest,
            profile_ids=tuple(item[1] for item in prepared),
            legacy_id_map=id_map,
            active_profile_id=legacy_active,
            last_launched_profile_id=legacy_last,
            copied_workspaces=copied,
        )

    def migrate_legacy_successors(self, catalog: object) -> int:
        """Append v4 successors to previously imported opaque legacy entries."""

        with self._locked():
            state = self._read_state()
            legacy = state.get("legacy")
            source = (
                legacy.get("source_document") if isinstance(legacy, Mapping) else None
            )
            repaired: list[tuple[dict[str, Any], dict[str, Any]]] = []
            for entry in state["profiles"]:
                if entry["tombstone"] or not entry.get("legacy_ids"):
                    continue
                current = entry["revisions"][-1].get("profile")
                if not isinstance(current, Mapping) or current.get(
                    "profile_api_version"
                ) in {
                    "io.tobkiri.profile.v4",
                    "io.tobkiri.profile.v5",
                }:
                    continue
                legacy_id = str(entry["legacy_ids"][0])
                item = _legacy_source_profile(source, legacy_id) or current
                source_path = (
                    legacy.get("source_path") if isinstance(legacy, Mapping) else None
                )
                successor = self._legacy_v4_successor(
                    item=item,
                    profile_id=str(entry["profile_id"]),
                    catalog=catalog,
                    source_path=(
                        str(source_path)
                        if source_path
                        else "legacy/startup_profiles.json"
                    ),
                )
                repaired.append((entry, successor))
            if not repaired:
                return 0
            now = self._now()
            for entry, successor in repaired:
                _append_profile_successor(entry, successor, now=now)
            state["generation"] += len(repaired)
            state["updated_at"] = now
            self._write_state(state)
            return len(repaired)

    @staticmethod
    def _legacy_v4_successor(
        *,
        item: Mapping[str, Any],
        profile_id: str,
        catalog: object,
        source_path: str,
    ) -> dict[str, Any]:
        """Build a successor without expanding the registry module."""

        from .legacy_profile_successor_v4 import build_legacy_profile_successor

        return build_legacy_profile_successor(
            item,
            profile_id=profile_id,
            catalog=catalog,
            source_path=source_path,
        )

    def _stage_legacy_workspaces(
        self,
        prepared: list[tuple[str, str, dict[str, Any], int, int]],
        *,
        source_root: Path | None,
        source_digest: str,
        copy_workspaces: bool,
    ) -> _LegacyWorkspaceStage | None:
        """Copy and validate legacy workspaces before changing the registry."""

        if not copy_workspaces or source_root is None:
            return None

        source_root = _canonical_absolute_path(Path(source_root).expanduser())
        try:
            source_root_descriptor = _open_directory_chain(source_root)
        except ProfileDefinitionStoreIntegrityError as error:
            raise ProfileDefinitionStoreIntegrityError(
                "legacy workspace root could not be opened safely"
            ) from error
        if source_root_descriptor is None:
            return None

        profiles_root = source_root / "profiles"
        try:
            try:
                source_profiles_descriptor = _open_directory_at(
                    source_root_descriptor,
                    "profiles",
                    required=False,
                )
            except ProfileDefinitionStoreIntegrityError as error:
                raise ProfileDefinitionStoreIntegrityError(
                    "legacy workspace profiles root could not be opened safely"
                ) from error
        finally:
            os.close(source_root_descriptor)
        if source_profiles_descriptor is None:
            return None

        source_profiles_snapshot = os.fstat(source_profiles_descriptor)
        source_profiles_identity = _directory_identity(source_profiles_snapshot)
        source_candidates: list[tuple[str, str, os.stat_result]] = []
        try:
            for legacy_id, candidate_id, _document, _created, _updated in prepared:
                safe_legacy_id = _safe_legacy_workspace_id(legacy_id)
                metadata = _stat_directory_entry(
                    source_profiles_descriptor,
                    safe_legacy_id,
                )
                if metadata is None:
                    continue
                if stat.S_ISLNK(metadata.st_mode):
                    raise ProfileDefinitionStoreIntegrityError(
                        "legacy workspace copy is unsafe"
                    )
                if not stat.S_ISDIR(metadata.st_mode):
                    raise ProfileDefinitionStoreIntegrityError(
                        "legacy workspace is not a directory"
                    )
                source_candidates.append((legacy_id, candidate_id, metadata))
        except Exception:
            _close_descriptor(source_profiles_descriptor)
            raise
        if not source_candidates:
            os.close(source_profiles_descriptor)
            return None

        user_data_path = _canonical_absolute_path(self._directory.root.parent)
        user_data_descriptor: int | None = None
        workspaces_descriptor: int | None = None
        workspaces_created = False
        staging_descriptor: int | None = None
        stage: _LegacyWorkspaceStage | None = None
        try:
            user_data_descriptor = _open_directory_chain(user_data_path)
            if user_data_descriptor is None:
                raise ProfileDefinitionStoreIntegrityError(
                    "Profile store user-data root is unavailable"
                )
            workspaces_descriptor, workspaces_created = _ensure_directory_at(
                user_data_descriptor,
                "workspaces",
            )
            workspaces_path = user_data_path / "workspaces"
            for _legacy_id, candidate_id, _metadata in source_candidates:
                destination_metadata = _stat_directory_entry(
                    workspaces_descriptor,
                    candidate_id,
                )
                if destination_metadata is not None:
                    raise ProfileDefinitionStoreIntegrityError(
                        "legacy workspace destination already exists"
                    )

            staging_name = (
                ".profile-migration-staging-"
                + source_digest.removeprefix("sha256:")[:24]
            )
            if _stat_directory_entry(user_data_descriptor, staging_name) is not None:
                raise ProfileDefinitionStoreIntegrityError(
                    "legacy workspace staging path already exists"
                )
            try:
                staging_descriptor = _create_directory_at(
                    user_data_descriptor,
                    staging_name,
                )
            except ProfileDefinitionStoreIntegrityError:
                raise
            staging_path = user_data_path / staging_name
            stage = _LegacyWorkspaceStage(
                source_profiles_descriptor=source_profiles_descriptor,
                source_profiles_path=profiles_root,
                source_profiles_identity=source_profiles_identity,
                user_data_descriptor=user_data_descriptor,
                user_data_path=user_data_path,
                workspaces_descriptor=workspaces_descriptor,
                workspaces_path=workspaces_path,
                workspaces_identity=_directory_identity(
                    os.fstat(workspaces_descriptor)
                ),
                workspaces_created=workspaces_created,
                staging_descriptor=staging_descriptor,
                staging_name=staging_name,
                staging_path=staging_path,
                staging_identity=_directory_identity(os.fstat(staging_descriptor)),
                workspaces=[],
            )

            for legacy_id, candidate_id, expected in source_candidates:
                safe_legacy_id = _safe_legacy_workspace_id(legacy_id)
                try:
                    source_descriptor = _open_directory_at(
                        source_profiles_descriptor,
                        safe_legacy_id,
                        required=True,
                    )
                except ProfileDefinitionStoreIntegrityError as error:
                    raise ProfileDefinitionStoreIntegrityError(
                        "legacy workspace source changed during validation"
                    ) from error
                assert source_descriptor is not None
                try:
                    opened = os.fstat(source_descriptor)
                    if not _same_directory_identity(expected, opened):
                        raise ProfileDefinitionStoreIntegrityError(
                            "legacy workspace source changed during validation"
                        )
                    staged_descriptor = _create_directory_at(
                        staging_descriptor,
                        candidate_id,
                    )
                    try:
                        _copy_directory_from_descriptor(
                            source_descriptor,
                            staged_descriptor,
                        )
                        final_source = os.fstat(source_descriptor)
                        if not _same_directory_snapshot(opened, final_source):
                            raise ProfileDefinitionStoreIntegrityError(
                                "legacy workspace source changed during copy"
                            )
                        current_source = _stat_directory_entry(
                            source_profiles_descriptor,
                            safe_legacy_id,
                        )
                        if current_source is None or not _same_directory_identity(
                            expected,
                            current_source,
                        ):
                            raise ProfileDefinitionStoreIntegrityError(
                                "legacy workspace source path changed during copy"
                            )
                        staged_identity = _directory_identity(
                            os.fstat(staged_descriptor)
                        )
                    finally:
                        os.close(staged_descriptor)
                finally:
                    os.close(source_descriptor)
                stage.workspaces.append(
                    _StagedLegacyWorkspace(
                        candidate_id=candidate_id,
                        staged_identity=staged_identity,
                        destination_path=workspaces_path / candidate_id,
                    )
                )

            _assert_pinned_directory(
                profiles_root,
                source_profiles_descriptor,
                "legacy workspace profiles root changed during copy",
            )
            if not _same_directory_snapshot(
                source_profiles_snapshot,
                os.fstat(source_profiles_descriptor),
            ):
                raise ProfileDefinitionStoreIntegrityError(
                    "legacy workspace profiles root changed during copy"
                )
            return stage
        except Exception:
            if stage is not None:
                self._cleanup_staging(stage)
            else:
                if staging_descriptor is not None:
                    _close_descriptor(staging_descriptor)
                if workspaces_descriptor is not None:
                    _close_descriptor(workspaces_descriptor)
                if workspaces_created and user_data_descriptor is not None:
                    _remove_empty_directory_at(user_data_descriptor, "workspaces")
                if user_data_descriptor is not None:
                    _close_descriptor(user_data_descriptor)
                _close_descriptor(source_profiles_descriptor)
            raise

    @staticmethod
    def _assert_legacy_stage_paths(stage: _LegacyWorkspaceStage) -> None:
        """Require every retained pathname to still name its pinned directory."""

        _assert_pinned_directory(
            stage.source_profiles_path,
            stage.source_profiles_descriptor,
            "legacy workspace source path changed",
        )
        _assert_pinned_directory(
            stage.user_data_path,
            stage.user_data_descriptor,
            "Profile store user-data path changed",
        )
        _assert_pinned_directory(
            stage.workspaces_path,
            stage.workspaces_descriptor,
            "legacy workspace destination path changed",
        )
        _assert_pinned_directory(
            stage.staging_path,
            stage.staging_descriptor,
            "legacy workspace staging path changed",
        )

    @staticmethod
    def _publish_staged_workspace(
        stage: _LegacyWorkspaceStage,
        workspace: _StagedLegacyWorkspace,
    ) -> None:
        """Publish one staged tree with descriptor-relative rename operations."""

        staged_descriptor = _open_directory_at(
            stage.staging_descriptor,
            workspace.candidate_id,
            required=True,
        )
        assert staged_descriptor is not None
        try:
            if _directory_identity(os.fstat(staged_descriptor)) != (
                workspace.staged_identity
            ):
                raise ProfileDefinitionStoreIntegrityError(
                    "legacy workspace staging entry changed before publication"
                )
        finally:
            os.close(staged_descriptor)

        if (
            _stat_directory_entry(
                stage.workspaces_descriptor,
                workspace.candidate_id,
            )
            is not None
        ):
            raise ProfileDefinitionStoreIntegrityError(
                "legacy workspace destination appeared during publication"
            )
        renamed = False
        try:
            os.rename(
                workspace.candidate_id,
                workspace.candidate_id,
                src_dir_fd=stage.staging_descriptor,
                dst_dir_fd=stage.workspaces_descriptor,
            )
            renamed = True
            published_descriptor = _open_directory_at(
                stage.workspaces_descriptor,
                workspace.candidate_id,
                required=True,
            )
            assert published_descriptor is not None
            try:
                if _directory_identity(os.fstat(published_descriptor)) != (
                    workspace.staged_identity
                ):
                    raise ProfileDefinitionStoreIntegrityError(
                        "legacy workspace publication target changed"
                    )
            finally:
                os.close(published_descriptor)
        except OSError as error:
            if renamed:
                _remove_tree_at(
                    stage.workspaces_descriptor,
                    workspace.candidate_id,
                    missing_ok=True,
                )
                raise ProfileDefinitionStoreIntegrityError(
                    "legacy workspace publication failed"
                ) from error
            raise ProfileDefinitionStoreIntegrityError(
                "legacy workspace publication failed"
            ) from error
        except Exception:
            if renamed:
                _remove_tree_at(
                    stage.workspaces_descriptor,
                    workspace.candidate_id,
                    missing_ok=True,
                )
            raise

    @staticmethod
    def _remove_staging_root(stage: _LegacyWorkspaceStage) -> None:
        """Remove the empty staging root through its retained parent descriptor."""

        current = _stat_directory_entry(
            stage.user_data_descriptor,
            stage.staging_name,
        )
        if current is None or not _same_directory_identities(
            _directory_identity(current),
            stage.staging_identity,
        ):
            raise ProfileDefinitionStoreIntegrityError(
                "legacy workspace staging path changed before cleanup"
            )
        if not stat.S_ISDIR(current.st_mode):
            raise ProfileDefinitionStoreIntegrityError(
                "legacy workspace staging path is not a directory"
            )
        try:
            os.rmdir(stage.staging_name, dir_fd=stage.user_data_descriptor)
        except OSError as error:
            raise ProfileDefinitionStoreIntegrityError(
                "legacy workspace staging cleanup failed"
            ) from error

    @staticmethod
    def _cleanup_staging(
        stage: _LegacyWorkspaceStage | None,
    ) -> None:
        """Remove unpublished staged workspace trees after a failed import."""

        if stage is None or stage.closed:
            return
        try:
            try:
                _remove_directory_contents(stage.staging_descriptor)
            except Exception:
                pass
            try:
                current_staging = _stat_directory_entry(
                    stage.user_data_descriptor,
                    stage.staging_name,
                )
                if current_staging is not None and _same_directory_identities(
                    stage.staging_identity,
                    _directory_identity(current_staging),
                ):
                    os.rmdir(
                        stage.staging_name,
                        dir_fd=stage.user_data_descriptor,
                    )
            except Exception:
                pass
            if stage.workspaces_created:
                _remove_empty_directory_at(
                    stage.user_data_descriptor,
                    "workspaces",
                    expected_identity=stage.workspaces_identity,
                )
        finally:
            for descriptor in (
                stage.staging_descriptor,
                stage.workspaces_descriptor,
                stage.user_data_descriptor,
                stage.source_profiles_descriptor,
            ):
                _close_descriptor(descriptor)
            stage.closed = True

    migrate_legacy_collection = import_legacy_collection

    def legacy_state(self) -> Mapping[str, Any]:
        """Return preserved legacy selection metadata, if an import occurred."""

        return copy.deepcopy(self.snapshot().get("legacy", {}))

    def _workspace_destination(self, profile_id: str) -> Path:
        return self.user_data_root / "workspaces" / profile_id

    @contextmanager
    def _locked(self) -> Iterator[None]:
        with exclusive_profile_lock(
            self._directory,
            ".profile-definition.lock",
            timeout_seconds=self._lock_timeout_seconds,
            monotonic_clock=self._monotonic_clock,
            retry_sleep=self._retry_sleep,
        ):
            yield

    @contextmanager
    def _migration_lock(
        self,
        stage: _LegacyWorkspaceStage | None,
    ) -> Iterator[None]:
        """Hold the registry lock and always clean an unfinished staging tree."""

        try:
            with self._locked():
                yield
        finally:
            self._cleanup_staging(stage)

    def _read_state(self) -> dict[str, Any]:
        try:
            if not self._directory.exists(PROFILE_STORE_FILENAME):
                return _empty_state()
            value = strict_loads(self._directory.read_bytes(PROFILE_STORE_FILENAME))
        except FileNotFoundError:
            return _empty_state()
        except (OSError, SecurePersistenceError, ValueError) as error:
            raise ProfileDefinitionStoreIntegrityError(
                "Profile definition store is unreadable"
            ) from error
        if not isinstance(value, Mapping):
            raise ProfileDefinitionStoreIntegrityError(
                "Profile definition store is not an object"
            )
        state = copy.deepcopy(dict(value))
        expected = canonical_digest(
            {key: state[key] for key in state if key != "store_digest"}
        )
        if state.get("store_digest") != expected:
            raise ProfileDefinitionStoreIntegrityError(
                "Profile definition store digest changed"
            )
        if "bootstrap" not in state:
            # v1 stores created before bootstrap metadata always represent an
            # already-established collection.  Never infer a template role
            # from a Profile ID, name, or provenance bytes.
            state["bootstrap"] = {
                "state": "not_required" if state.get("profiles") else "empty",
                "template_profile_revision": None,
            }
            state["store_digest"] = canonical_digest(
                {key: state[key] for key in state if key != "store_digest"}
            )
        self._validate_state(state)
        return state

    def _write_state(self, state: Mapping[str, Any]) -> None:
        payload = copy.deepcopy(dict(state))
        payload["store_digest"] = canonical_digest(
            {key: payload[key] for key in payload if key != "store_digest"}
        )
        try:
            self._directory.write_bytes_atomic(
                PROFILE_STORE_FILENAME,
                canonical_json(payload) + b"\n",
            )
        except (OSError, SecurePersistenceError, ValueError) as error:
            raise ProfileDefinitionStoreError(
                "Profile definition store could not be committed"
            ) from error

    def _validate_state(self, state: Mapping[str, Any]) -> None:
        expected = {
            "schema",
            "generation",
            "updated_at",
            "profiles",
            "legacy",
            "bootstrap",
            "store_digest",
        }
        if set(state) != expected or state.get("schema") != PROFILE_STORE_SCHEMA:
            raise ProfileDefinitionStoreIntegrityError(
                "Profile definition store fields are invalid"
            )
        if not _non_negative_int(state.get("generation")) or not _non_negative_int(
            state.get("updated_at")
        ):
            raise ProfileDefinitionStoreIntegrityError(
                "Profile definition store counters are invalid"
            )
        profiles = state.get("profiles")
        if not isinstance(profiles, list):
            raise ProfileDefinitionStoreIntegrityError(
                "Profile definition store profiles are invalid"
            )
        seen: set[str] = set()
        for entry in profiles:
            if not isinstance(entry, Mapping):
                raise ProfileDefinitionStoreIntegrityError(
                    "Profile store entry is invalid"
                )
            self._validate_entry(entry)
            if entry["profile_id"] in seen:
                raise ProfileDefinitionStoreIntegrityError(
                    "Profile store ID is duplicated"
                )
            seen.add(entry["profile_id"])
        legacy = state.get("legacy")
        if not isinstance(legacy, Mapping):
            raise ProfileDefinitionStoreIntegrityError(
                "Profile migration metadata is invalid"
            )
        bootstrap = state.get("bootstrap")
        if (
            not isinstance(bootstrap, Mapping)
            or set(bootstrap) != {"state", "template_profile_revision"}
            or bootstrap.get("state")
            not in {"empty", "template_available", "not_required"}
            or (
                bootstrap.get("template_profile_revision") is not None
                and not isinstance(bootstrap.get("template_profile_revision"), str)
            )
        ):
            raise ProfileDefinitionStoreIntegrityError(
                "Profile bootstrap metadata is invalid"
            )
        live = [entry for entry in profiles if not entry["tombstone"]]
        bootstrap_state = bootstrap["state"]
        template_revision = bootstrap["template_profile_revision"]
        if bootstrap_state == "empty" and (live or template_revision is not None):
            raise ProfileDefinitionStoreIntegrityError(
                "empty Profile bootstrap metadata is inconsistent"
            )
        if bootstrap_state == "not_required" and template_revision is not None:
            raise ProfileDefinitionStoreIntegrityError(
                "established Profile bootstrap metadata is inconsistent"
            )
        if bootstrap_state == "template_available" and (
            not isinstance(template_revision, str)
            or len(live) != 1
            or live[0]["current_revision"] != template_revision
            or sum(
                1
                for revision in live[0]["revisions"]
                if revision["profile_revision"] == template_revision
                and not revision["tombstone"]
            )
            != 1
        ):
            raise ProfileDefinitionStoreIntegrityError(
                "Profile bootstrap template binding is inconsistent"
            )

    @staticmethod
    def _validate_entry(entry: Mapping[str, Any]) -> None:
        expected = {
            "profile_id",
            "order",
            "tombstone",
            "current_revision",
            "revisions",
            "created_at",
            "updated_at",
            "legacy_ids",
        }
        if set(entry) != expected:
            raise ProfileDefinitionStoreIntegrityError(
                "Profile store entry fields are invalid"
            )
        if not isinstance(entry["profile_id"], str):
            raise ProfileDefinitionStoreIntegrityError("Profile ID is invalid")
        profile_id = _safe_profile_id(entry["profile_id"])
        try:
            validate_artifact_digest(
                entry["current_revision"], field="current_revision"
            )
        except Exception as error:
            raise ProfileDefinitionStoreIntegrityError(str(error)) from error
        if not _non_negative_int(entry["order"]):
            raise ProfileDefinitionStoreIntegrityError("Profile order is invalid")
        if not isinstance(entry["tombstone"], bool):
            raise ProfileDefinitionStoreIntegrityError("Profile tombstone is invalid")
        if not isinstance(entry["revisions"], list) or not entry["revisions"]:
            raise ProfileDefinitionStoreIntegrityError(
                "Profile revision history is empty"
            )
        if not isinstance(entry["legacy_ids"], list) or any(
            not isinstance(item, str) or not item for item in entry["legacy_ids"]
        ):
            raise ProfileDefinitionStoreIntegrityError("Profile legacy IDs are invalid")
        if not _non_negative_int(entry["created_at"]) or not _non_negative_int(
            entry["updated_at"]
        ):
            raise ProfileDefinitionStoreIntegrityError("Profile timestamps are invalid")
        latest = entry["revisions"][-1]
        if (
            not isinstance(latest, Mapping)
            or latest.get("profile_revision") != entry["current_revision"]
        ):
            raise ProfileDefinitionStoreIntegrityError(
                "Profile current revision is inconsistent"
            )
        previous_revision: str | None = None
        for index, revision in enumerate(entry["revisions"]):
            if not isinstance(revision, Mapping):
                raise ProfileDefinitionStoreIntegrityError(
                    "Profile revision is invalid"
                )
            fields = {
                "profile_revision",
                "parent_revision",
                "profile",
                "created_at",
                "updated_at",
                "tombstone",
            }
            if set(revision) != fields:
                raise ProfileDefinitionStoreIntegrityError(
                    "Profile revision fields are invalid"
                )
            if not _non_negative_int(revision["created_at"]) or not _non_negative_int(
                revision["updated_at"]
            ):
                raise ProfileDefinitionStoreIntegrityError(
                    "Profile revision timestamp is invalid"
                )
            if not isinstance(revision["tombstone"], bool):
                raise ProfileDefinitionStoreIntegrityError(
                    "Profile revision tombstone is invalid"
                )
            try:
                validate_artifact_digest(
                    revision["profile_revision"],
                    field="profile_revision",
                )
            except Exception as error:
                raise ProfileDefinitionStoreIntegrityError(str(error)) from error
            parent_revision = revision["parent_revision"]
            if index == 0:
                if parent_revision is not None:
                    raise ProfileDefinitionStoreIntegrityError(
                        "first Profile revision cannot have a parent"
                    )
            elif parent_revision != previous_revision:
                raise ProfileDefinitionStoreIntegrityError(
                    "Profile revision successor chain is invalid"
                )
            if parent_revision is not None:
                try:
                    validate_artifact_digest(parent_revision, field="parent_revision")
                except Exception as error:
                    raise ProfileDefinitionStoreIntegrityError(str(error)) from error
            profile = revision["profile"]
            if revision["tombstone"]:
                if profile is not None:
                    raise ProfileDefinitionStoreIntegrityError(
                        "Profile tombstone contains a document"
                    )
                expected_tombstone = canonical_digest(
                    {
                        "schema": "io.tobkiri.profile-tombstone.v1",
                        "profile_id": profile_id,
                        "parent_revision": parent_revision,
                    }
                )
                if revision["profile_revision"] != expected_tombstone:
                    raise ProfileDefinitionStoreIntegrityError(
                        "Profile tombstone revision is invalid"
                    )
            else:
                if not isinstance(profile, Mapping):
                    raise ProfileDefinitionStoreIntegrityError(
                        "Profile revision document is invalid"
                    )
                if profile.get("profile_id") != profile_id:
                    raise ProfileDefinitionStoreIntegrityError(
                        "Profile revision ID does not match its entry"
                    )
                if canonical_digest(profile) != revision["profile_revision"]:
                    raise ProfileDefinitionStoreIntegrityError(
                        "Profile revision digest changed"
                    )
            previous_revision = revision["profile_revision"]
        if bool(latest["tombstone"]) != bool(entry["tombstone"]):
            raise ProfileDefinitionStoreIntegrityError(
                "Profile entry tombstone is inconsistent"
            )

    @staticmethod
    def _stored_from_entry(entry: Mapping[str, Any]) -> StoredProfile:
        revision = entry["revisions"][-1]
        if revision["profile"] is None:
            prior = next(
                (
                    item
                    for item in reversed(entry["revisions"][:-1])
                    if isinstance(item.get("profile"), Mapping)
                ),
                None,
            )
            profile = (
                dict(prior["profile"]) if prior else {"profile_id": entry["profile_id"]}
            )
        else:
            profile = dict(revision["profile"])
        return StoredProfile(
            profile_id=str(entry["profile_id"]),
            profile_revision=str(entry["current_revision"]),
            profile=copy.deepcopy(profile),
            order=int(entry["order"]),
            parent_revision=revision.get("parent_revision"),
            tombstone=bool(entry["tombstone"]),
            created_at=int(entry["created_at"]),
            updated_at=int(entry["updated_at"]),
            legacy_ids=tuple(str(item) for item in entry.get("legacy_ids", [])),
        )

    @staticmethod
    def _entry_for_id(
        state: Mapping[str, Any], profile_id: str
    ) -> dict[str, Any] | None:
        return next(
            (entry for entry in state["profiles"] if entry["profile_id"] == profile_id),
            None,
        )

    @staticmethod
    def _check_generation(state: Mapping[str, Any], expected: int | None) -> None:
        if expected is not None and state["generation"] != expected:
            raise ProfileDefinitionStoreConflict("Profile store generation is stale")

    @staticmethod
    def _check_profile_revision(current: StoredProfile, expected: str | None) -> None:
        if expected is not None and current.profile_revision != expected:
            raise ProfileDefinitionStoreConflict("Profile revision is stale")

    def _now(self) -> int:
        value = int(self._clock())
        if value < 0:
            raise ProfileDefinitionStoreError("clock returned a negative timestamp")
        return value

    @staticmethod
    def _legacy_timestamp(
        item: Mapping[str, Any],
        *,
        key: str = "created_at",
        fallback: int | None = None,
    ) -> int:
        value = item.get(key, fallback if fallback is not None else int(time.time()))
        if isinstance(value, bool) or not isinstance(value, (int, str)):
            return int(fallback or 0)
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            return int(fallback or 0)
        return max(parsed, 0)


def _empty_state() -> dict[str, Any]:
    return {
        "schema": PROFILE_STORE_SCHEMA,
        "generation": 0,
        "updated_at": 0,
        "profiles": [],
        "bootstrap": {"state": "empty", "template_profile_revision": None},
        "legacy": {
            "source_digest": None,
            "source_path": None,
            "active_profile_id": None,
            "last_launched_profile_id": None,
            "legacy_id_map": {},
            "tombstones": [],
            "source_document": None,
        },
        "store_digest": canonical_digest(
            {
                "schema": PROFILE_STORE_SCHEMA,
                "generation": 0,
                "updated_at": 0,
                "profiles": [],
                "bootstrap": {"state": "empty", "template_profile_revision": None},
                "legacy": {
                    "source_digest": None,
                    "source_path": None,
                    "active_profile_id": None,
                    "last_launched_profile_id": None,
                    "legacy_id_map": {},
                    "tombstones": [],
                    "source_document": None,
                },
            }
        ),
    }


def _new_entry(
    profile_id: str,
    profile: Mapping[str, Any],
    *,
    revision: str,
    order: int,
    now: int,
    updated_at: int | None = None,
    legacy_ids: tuple[str, ...] = (),
) -> dict[str, Any]:
    timestamp = now if updated_at is None else updated_at
    return {
        "profile_id": profile_id,
        "order": order,
        "tombstone": False,
        "current_revision": revision,
        "revisions": [
            _revision_record(
                profile,
                revision=revision,
                parent_revision=None,
                now=now,
                updated_at=timestamp,
            )
        ],
        "created_at": now,
        "updated_at": timestamp,
        "legacy_ids": list(legacy_ids),
    }


def _revision_record(
    profile: Mapping[str, Any] | None,
    *,
    revision: str,
    parent_revision: str | None,
    now: int,
    updated_at: int | None = None,
    tombstone: bool = False,
) -> dict[str, Any]:
    return {
        "profile_revision": revision,
        "parent_revision": parent_revision,
        "profile": copy.deepcopy(dict(profile)) if profile is not None else None,
        "created_at": now,
        "updated_at": now if updated_at is None else updated_at,
        "tombstone": tombstone,
    }


def _append_profile_successor(
    entry: dict[str, Any],
    profile: Mapping[str, Any],
    *,
    now: int,
) -> None:
    """Append one immutable successor inside a caller-owned transaction."""

    parent_revision = str(entry["current_revision"])
    revision = canonical_digest(profile)
    entry["current_revision"] = revision
    entry["revisions"].append(
        _revision_record(
            profile,
            revision=revision,
            parent_revision=parent_revision,
            now=now,
        )
    )
    entry["updated_at"] = now


def _profile_document(
    profile: Mapping[str, Any],
    *,
    profile_id: str | None = None,
    display_name: str | None = None,
) -> dict[str, Any]:
    if not isinstance(profile, Mapping):
        raise ProfileDefinitionStoreIntegrityError(
            "Profile definition must be an object"
        )
    document = copy.deepcopy(dict(profile))
    candidate_id = profile_id or document.get("profile_id")
    if not isinstance(candidate_id, str) or not candidate_id:
        raise ProfileDefinitionStoreIntegrityError(
            "Profile definition lacks profile_id"
        )
    safe_id = _safe_profile_id(candidate_id)
    document["profile_id"] = safe_id
    if display_name is not None:
        document["display_name"] = str(display_name)
    if "display_name" in document and (
        not isinstance(document["display_name"], str)
        or not document["display_name"].strip()
    ):
        raise ProfileDefinitionStoreIntegrityError(
            "Profile display_name must be a non-empty string"
        )
    canonical_json(document)
    return document


def _normalize_legacy_display_name(
    profile: Mapping[str, Any],
) -> dict[str, Any]:
    """Return a lossless runtime-facing legacy name projection.

    The declared Profile locale wins when it names an available localized
    value.  Japanese then English match the established control-panel legacy
    fallback, followed by the scalar legacy ``name``.  Unknown or structurally
    ambiguous localized mappings are rejected rather than stringified.
    """

    document = copy.deepcopy(dict(profile))
    if "display_name" not in document:
        return document
    display_name = document.get("display_name")
    preserved = document.get("legacy_display_name")
    if isinstance(display_name, str):
        if not display_name.strip():
            raise ProfileDefinitionStoreIntegrityError(
                "legacy Profile display_name is empty"
            )
        if preserved is not None and not isinstance(preserved, Mapping):
            raise ProfileDefinitionStoreIntegrityError(
                "legacy_display_name must preserve an object"
            )
        return document
    if not isinstance(display_name, Mapping) or not display_name:
        raise ProfileDefinitionStoreIntegrityError(
            "legacy Profile display_name is invalid"
        )
    if preserved is not None and preserved != display_name:
        raise ProfileDefinitionStoreIntegrityError(
            "legacy Profile display_name preservation is ambiguous"
        )

    localized: dict[str, str] = {}
    for key, value in display_name.items():
        if (
            not isinstance(key, str)
            or _LEGACY_LOCALE_RE.fullmatch(key) is None
            or not isinstance(value, str)
            or not value.strip()
        ):
            raise ProfileDefinitionStoreIntegrityError(
                "legacy Profile localized display_name is invalid"
            )
        normalized_key = key.replace("_", "-").casefold()
        if normalized_key in localized:
            raise ProfileDefinitionStoreIntegrityError(
                "legacy Profile localized display_name is ambiguous"
            )
        localized[normalized_key] = value.strip()

    candidates: list[str] = []
    locale = document.get("locale")
    if locale is not None:
        if not isinstance(locale, str) or _LEGACY_LOCALE_RE.fullmatch(locale) is None:
            raise ProfileDefinitionStoreIntegrityError(
                "legacy Profile locale is invalid"
            )
        normalized_locale = locale.replace("_", "-").casefold()
        candidates.append(normalized_locale)
        language = normalized_locale.split("-", 1)[0]
        if language != normalized_locale:
            candidates.append(language)
    candidates.extend(("ja", "en"))
    canonical_name = next(
        (localized[candidate] for candidate in candidates if candidate in localized),
        None,
    )
    if canonical_name is None:
        legacy_name = document.get("name")
        if isinstance(legacy_name, str) and legacy_name.strip():
            canonical_name = legacy_name.strip()
    if canonical_name is None:
        raise ProfileDefinitionStoreIntegrityError(
            "legacy Profile localized display_name has no deterministic fallback"
        )

    document["legacy_display_name"] = copy.deepcopy(dict(display_name))
    document["display_name"] = canonical_name
    return document


def _safe_profile_id(value: str) -> str:
    try:
        return validate_canonical_id(str(value), field="profile_id")
    except Exception as error:
        raise ProfileDefinitionStoreIntegrityError(str(error)) from error


def _non_negative_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _next_copy_id(state: Mapping[str, Any], profile_id: str) -> str:
    existing = {str(entry["profile_id"]) for entry in state["profiles"]}
    base = f"{profile_id}-copy"
    candidate = base
    suffix = 2
    while candidate in existing:
        candidate = f"{base}-{suffix}"
        suffix += 1
    return candidate


def _canonical_migration_id(value: str, used: set[str]) -> str:
    lowered = value.strip().casefold()
    lowered = re.sub(r"[^a-z0-9]+", "-", lowered).strip("-") or "profile"
    if not lowered[0].isalpha():
        lowered = "profile-" + lowered
    try:
        candidate = _safe_profile_id(lowered)
    except ProfileDefinitionStoreIntegrityError:
        candidate = "profile"
    suffix = 2
    base = candidate
    while candidate in used:
        candidate = f"{base}-{suffix}"
        suffix += 1
    return candidate


def _legacy_ref(value: object, id_map: Mapping[str, str]) -> str | None:
    if value is None:
        return None
    return id_map.get(str(value), str(value) if str(value) in id_map.values() else None)


def _legacy_source_profile(
    source: object,
    legacy_id: str,
) -> Mapping[str, Any] | None:
    """Find the exact audited source object for one imported legacy identity."""

    if not isinstance(source, Mapping):
        return None
    profiles = source.get("profiles")
    if isinstance(profiles, Mapping):
        value = profiles.get(legacy_id)
        return value if isinstance(value, Mapping) else None
    if not isinstance(profiles, list):
        return None
    return next(
        (
            item
            for item in profiles
            if isinstance(item, Mapping)
            and str(item.get("profile_id") or item.get("id") or "") == legacy_id
        ),
        None,
    )


def _guess_workspace_root(source_path: Path | None) -> Path | None:
    if source_path is None:
        return None
    # settings/startup_profiles.json -> the old user_data root.
    return source_path.parent.parent


def _validate_legacy_workspace_ids(
    profiles: Sequence[Mapping[str, Any]],
) -> None:
    """Validate every selected raw legacy ID before migration normalization."""

    for item in profiles:
        for key in ("profile_id", "id"):
            raw_id = item.get(key)
            if raw_id:
                _safe_legacy_workspace_id(str(raw_id))


def _safe_legacy_workspace_id(value: str) -> str:
    """Accept only one legacy ID component for workspace path lookup."""

    raw_value = str(value)
    lookup_value = raw_value.strip()
    if (
        not lookup_value
        or lookup_value in {".", ".."}
        or "\x00" in raw_value
        or "/" in lookup_value
        or "\\" in lookup_value
        or Path(lookup_value).is_absolute()
        or re.match(r"^[A-Za-z]:", lookup_value) is not None
    ):
        raise ProfileDefinitionStoreIntegrityError(
            "legacy Profile ID is unsafe for workspace lookup"
        )
    return raw_value


def _canonical_absolute_path(path: Path) -> Path:
    """Normalize only lexical path syntax and approved platform aliases."""

    requested = canonical_platform_path(Path(path).expanduser())
    if not requested.is_absolute():
        requested = requested.absolute()
    return Path(os.path.normpath(os.fspath(requested)))


def _require_directory_operations() -> None:
    """Require the descriptor-relative primitives used by migration."""

    if not _DESCRIPTOR_OPERATIONS_AVAILABLE:
        raise ProfileDefinitionStoreIntegrityError(
            "legacy workspace descriptor operations are unavailable"
        )


def _open_directory_chain(path: Path) -> int | None:
    """Open every path component from the filesystem root without following links."""

    _require_directory_operations()
    requested = _canonical_absolute_path(path)
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | os.O_NOFOLLOW
    descriptor: int | None = None
    try:
        descriptor = os.open(requested.anchor or "/", flags)
        for component in requested.parts[1:]:
            try:
                next_descriptor = os.open(
                    component,
                    flags,
                    dir_fd=descriptor,
                )
            except FileNotFoundError:
                return None
            os.close(descriptor)
            descriptor = next_descriptor
        result = descriptor
        descriptor = None
        return result
    except FileNotFoundError:
        return None
    except OSError as error:
        raise ProfileDefinitionStoreIntegrityError(
            "legacy workspace directory chain could not be opened safely"
        ) from error
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _open_directory_at(
    parent_descriptor: int,
    name: str,
    *,
    required: bool,
) -> int | None:
    """Open one directory relative to a pinned parent without following links."""

    _require_directory_operations()
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | os.O_NOFOLLOW
    try:
        descriptor = os.open(name, flags, dir_fd=parent_descriptor)
    except FileNotFoundError:
        if required:
            raise ProfileDefinitionStoreIntegrityError(
                "legacy workspace directory is unavailable"
            ) from None
        return None
    except OSError as error:
        raise ProfileDefinitionStoreIntegrityError(
            "legacy workspace directory could not be opened safely"
        ) from error
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISDIR(metadata.st_mode):
            raise ProfileDefinitionStoreIntegrityError(
                "legacy workspace directory is not a directory"
            )
        return descriptor
    except Exception:
        os.close(descriptor)
        raise


def _stat_directory_entry(
    parent_descriptor: int,
    name: str,
) -> os.stat_result | None:
    """Inspect one child without following a final symlink."""

    _require_directory_operations()
    try:
        return os.stat(
            name,
            dir_fd=parent_descriptor,
            follow_symlinks=False,
        )
    except FileNotFoundError:
        return None
    except OSError as error:
        raise ProfileDefinitionStoreIntegrityError(
            "legacy workspace entry could not be inspected safely"
        ) from error


def _ensure_directory_at(parent_descriptor: int, name: str) -> tuple[int, bool]:
    """Open or create one private directory below a pinned parent."""

    metadata = _stat_directory_entry(parent_descriptor, name)
    if metadata is not None:
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
            raise ProfileDefinitionStoreIntegrityError(
                "legacy workspace directory is unsafe"
            )
        descriptor = _open_directory_at(parent_descriptor, name, required=True)
        assert descriptor is not None
        return descriptor, False

    created = False
    try:
        os.mkdir(name, mode=0o700, dir_fd=parent_descriptor)
        created = True
    except FileExistsError:
        pass
    except OSError as error:
        raise ProfileDefinitionStoreIntegrityError(
            "legacy workspace directory could not be created safely"
        ) from error
    try:
        descriptor = _open_directory_at(parent_descriptor, name, required=True)
    except Exception:
        if created:
            _remove_empty_directory_at(parent_descriptor, name)
        raise
    assert descriptor is not None
    return descriptor, created


def _create_directory_at(
    parent_descriptor: int,
    name: str,
    *,
    mode: int = 0o700,
) -> int:
    """Create and reopen one directory using only its pinned parent descriptor."""

    _require_directory_operations()
    try:
        os.mkdir(name, mode=mode, dir_fd=parent_descriptor)
    except FileExistsError as error:
        raise ProfileDefinitionStoreIntegrityError(
            "legacy workspace directory already exists"
        ) from error
    except OSError as error:
        raise ProfileDefinitionStoreIntegrityError(
            "legacy workspace directory could not be created safely"
        ) from error
    try:
        descriptor = _open_directory_at(parent_descriptor, name, required=True)
    except Exception:
        _remove_empty_directory_at(parent_descriptor, name)
        raise
    assert descriptor is not None
    return descriptor


def _close_descriptor(descriptor: int | None) -> None:
    """Close a descriptor while keeping best-effort cleanup non-throwing."""

    if descriptor is None:
        return
    try:
        os.close(descriptor)
    except OSError:
        pass


def _directory_identity(metadata: os.stat_result) -> tuple[int, int]:
    """Return the stable identity of one opened directory."""

    return int(metadata.st_dev), int(metadata.st_ino)


def _same_directory_identities(
    first: tuple[int, int],
    second: tuple[int, int],
) -> bool:
    """Compare two captured directory identities."""

    return first == second


def _same_file_identity(
    first: os.stat_result,
    second: os.stat_result,
) -> bool:
    """Compare the metadata that matters for a pinned source file."""

    return (
        first.st_dev,
        first.st_ino,
        first.st_mode,
        first.st_nlink,
        first.st_size,
        first.st_mtime_ns,
        first.st_ctime_ns,
    ) == (
        second.st_dev,
        second.st_ino,
        second.st_mode,
        second.st_nlink,
        second.st_size,
        second.st_mtime_ns,
        second.st_ctime_ns,
    )


def _same_directory_identity(
    first: os.stat_result,
    second: os.stat_result,
) -> bool:
    """Compare the identity and type of a pinned source directory."""

    return (
        first.st_dev,
        first.st_ino,
        first.st_mode,
    ) == (
        second.st_dev,
        second.st_ino,
        second.st_mode,
    )


def _same_directory_snapshot(
    first: os.stat_result,
    second: os.stat_result,
) -> bool:
    """Compare a directory identity and mutation-sensitive metadata."""

    return _same_directory_identity(first, second) and (
        first.st_nlink,
        first.st_size,
        first.st_mtime_ns,
        first.st_ctime_ns,
    ) == (
        second.st_nlink,
        second.st_size,
        second.st_mtime_ns,
        second.st_ctime_ns,
    )


def _assert_pinned_directory(
    path: Path,
    descriptor: int,
    message: str,
) -> None:
    """Require a pathname to still resolve to an already-open directory."""

    try:
        current_descriptor = _open_directory_chain(path)
        if current_descriptor is None:
            raise ProfileDefinitionStoreIntegrityError(message)
        try:
            if _directory_identity(os.fstat(current_descriptor)) != (
                _directory_identity(os.fstat(descriptor))
            ):
                raise ProfileDefinitionStoreIntegrityError(message)
        finally:
            os.close(current_descriptor)
    except ProfileDefinitionStoreIntegrityError as error:
        if str(error) == message:
            raise
        raise ProfileDefinitionStoreIntegrityError(message) from error


def _remove_empty_directory_at(
    parent_descriptor: int,
    name: str,
    *,
    expected_identity: tuple[int, int] | None = None,
) -> None:
    """Remove an empty child directory without following a replacement link."""

    try:
        metadata = _stat_directory_entry(parent_descriptor, name)
        if metadata is None or not stat.S_ISDIR(metadata.st_mode):
            return
        if expected_identity is not None and not _same_directory_identities(
            _directory_identity(metadata),
            expected_identity,
        ):
            return
        os.rmdir(name, dir_fd=parent_descriptor)
    except Exception:
        # This helper is used while unwinding an already failed transaction.
        # It never follows a replacement path and must not mask the original
        # integrity failure.
        return


def _remove_tree_at(
    parent_descriptor: int,
    name: str,
    *,
    missing_ok: bool,
) -> None:
    """Remove one tree using only descriptor-relative, no-follow operations."""

    metadata = _stat_directory_entry(parent_descriptor, name)
    if metadata is None:
        if missing_ok:
            return
        raise ProfileDefinitionStoreIntegrityError(
            "legacy workspace cleanup target is unavailable"
        )
    if stat.S_ISDIR(metadata.st_mode):
        child_descriptor = _open_directory_at(
            parent_descriptor,
            name,
            required=True,
        )
        assert child_descriptor is not None
        try:
            opened = os.fstat(child_descriptor)
            if not _same_directory_identity(metadata, opened):
                raise ProfileDefinitionStoreIntegrityError(
                    "legacy workspace cleanup target changed"
                )
            _remove_directory_contents(child_descriptor)
        finally:
            os.close(child_descriptor)
        try:
            os.rmdir(name, dir_fd=parent_descriptor)
        except FileNotFoundError:
            if not missing_ok:
                raise
    else:
        try:
            os.unlink(name, dir_fd=parent_descriptor)
        except FileNotFoundError:
            if not missing_ok:
                raise


def _remove_directory_contents(directory_descriptor: int) -> None:
    """Remove every direct child of an opened directory without following links."""

    try:
        with os.scandir(directory_descriptor) as entries:
            names = [entry.name for entry in entries]
    except OSError as error:
        raise ProfileDefinitionStoreIntegrityError(
            "legacy workspace cleanup enumeration failed"
        ) from error
    for name in names:
        _remove_tree_at(directory_descriptor, name, missing_ok=True)


def _write_all(descriptor: int, content: bytes) -> None:
    """Write all bytes to a descriptor without reopening its path."""

    pending = memoryview(content)
    while pending:
        written = os.write(descriptor, pending)
        if written <= 0:
            raise ProfileDefinitionStoreIntegrityError(
                "legacy workspace file write made no progress"
            )
        pending = pending[written:]


def _copy_regular_file_from_directory(
    source_directory: int,
    filename: str,
    destination_directory: int,
    expected: os.stat_result,
) -> None:
    """Copy one regular file through pinned descriptors and recheck its bytes."""

    flags = os.O_RDONLY | os.O_NOFOLLOW
    try:
        source_descriptor = os.open(filename, flags, dir_fd=source_directory)
    except OSError as error:
        raise ProfileDefinitionStoreIntegrityError(
            "legacy workspace file could not be opened safely"
        ) from error
    destination_descriptor: int | None = None
    try:
        opened = os.fstat(source_descriptor)
        if not stat.S_ISREG(opened.st_mode):
            raise ProfileDefinitionStoreIntegrityError(
                "legacy workspace file changed during validation"
            )
        if not _same_file_identity(expected, opened):
            raise ProfileDefinitionStoreIntegrityError(
                "legacy workspace file changed during validation"
            )
        if opened.st_nlink != 1:
            raise ProfileDefinitionStoreIntegrityError(
                "legacy workspace file has multiple links"
            )
        destination_flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
        try:
            destination_descriptor = os.open(
                filename,
                destination_flags,
                stat.S_IMODE(opened.st_mode) & 0o777,
                dir_fd=destination_directory,
            )
        except OSError as error:
            raise ProfileDefinitionStoreIntegrityError(
                "legacy workspace destination could not be created safely"
            ) from error
        copied_digest = hashlib.sha256()
        while True:
            chunk = os.read(source_descriptor, 1024 * 1024)
            if not chunk:
                break
            copied_digest.update(chunk)
            _write_all(destination_descriptor, chunk)
        final = os.fstat(source_descriptor)
        if not _same_file_identity(opened, final):
            raise ProfileDefinitionStoreIntegrityError(
                "legacy workspace file changed during copy"
            )
        current = _stat_directory_entry(source_directory, filename)
        if current is None or not _same_file_identity(opened, current):
            raise ProfileDefinitionStoreIntegrityError(
                "legacy workspace file changed during copy"
            )
        try:
            os.lseek(source_descriptor, 0, os.SEEK_SET)
        except OSError as error:
            raise ProfileDefinitionStoreIntegrityError(
                "legacy workspace file could not be verified"
            ) from error
        verify_digest = hashlib.sha256()
        while True:
            chunk = os.read(source_descriptor, 1024 * 1024)
            if not chunk:
                break
            verify_digest.update(chunk)
        if verify_digest.digest() != copied_digest.digest():
            raise ProfileDefinitionStoreIntegrityError(
                "legacy workspace file changed during copy"
            )
        final = os.fstat(source_descriptor)
        current = _stat_directory_entry(source_directory, filename)
        if (
            not _same_file_identity(opened, final)
            or current is None
            or not _same_file_identity(opened, current)
        ):
            raise ProfileDefinitionStoreIntegrityError(
                "legacy workspace file changed during copy"
            )
        copied = os.fstat(destination_descriptor)
        if not stat.S_ISREG(copied.st_mode) or copied.st_nlink != 1:
            raise ProfileDefinitionStoreIntegrityError(
                "legacy workspace copy is not a regular file"
            )
    finally:
        if destination_descriptor is not None:
            os.close(destination_descriptor)
        os.close(source_descriptor)


def _copy_directory_from_descriptor(
    source_descriptor: int,
    destination_descriptor: int,
) -> None:
    """Recursively copy a directory while pinning every source descriptor."""

    source_before = os.fstat(source_descriptor)
    try:
        with os.scandir(source_descriptor) as entries:
            children = list(entries)
    except OSError as error:
        raise ProfileDefinitionStoreIntegrityError(
            "legacy workspace could not be enumerated safely"
        ) from error
    for entry in children:
        try:
            expected = entry.stat(follow_symlinks=False)
        except OSError as error:
            raise ProfileDefinitionStoreIntegrityError(
                "legacy workspace entry could not be inspected safely"
            ) from error
        if stat.S_ISLNK(expected.st_mode):
            raise ProfileDefinitionStoreIntegrityError(
                "legacy workspace contains a symlink"
            )
        if stat.S_ISDIR(expected.st_mode):
            child_descriptor = _open_directory_at(
                source_descriptor,
                entry.name,
                required=True,
            )
            assert child_descriptor is not None
            try:
                opened = os.fstat(child_descriptor)
                if not _same_directory_identity(expected, opened):
                    raise ProfileDefinitionStoreIntegrityError(
                        "legacy workspace directory changed during validation"
                    )
                target_descriptor = _create_directory_at(
                    destination_descriptor,
                    entry.name,
                    mode=stat.S_IMODE(opened.st_mode) & 0o777,
                )
                try:
                    _copy_directory_from_descriptor(
                        child_descriptor,
                        target_descriptor,
                    )
                finally:
                    os.close(target_descriptor)
                final = os.fstat(child_descriptor)
                if not _same_directory_snapshot(opened, final):
                    raise ProfileDefinitionStoreIntegrityError(
                        "legacy workspace directory changed during copy"
                    )
            finally:
                os.close(child_descriptor)
        elif stat.S_ISREG(expected.st_mode):
            _copy_regular_file_from_directory(
                source_descriptor,
                entry.name,
                destination_descriptor,
                expected,
            )
        else:
            raise ProfileDefinitionStoreIntegrityError(
                "legacy workspace contains an unsupported entry"
            )
    source_after = os.fstat(source_descriptor)
    if not _same_directory_snapshot(source_before, source_after):
        raise ProfileDefinitionStoreIntegrityError(
            "legacy workspace directory changed during copy"
        )


__all__ = [
    "LegacyMigrationResult",
    "PROFILE_STORE_FILENAME",
    "PROFILE_STORE_SCHEMA",
    "ProfileDefinitionNotFound",
    "ProfileDefinitionStore",
    "ProfileDefinitionStoreConflict",
    "ProfileDefinitionStoreError",
    "ProfileDefinitionStoreIntegrityError",
    "StoredProfile",
]
