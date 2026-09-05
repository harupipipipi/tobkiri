"""Finite, restart-safe capture of Host-owned Profile v4 activations."""

from __future__ import annotations

import hashlib
import json
import os
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Mapping

from tobkiri_protocol.canonical import canonical_digest, strict_loads
from tobkiri_protocol.ids import validate_canonical_id
from tobkiri_protocol.secure_persistence import (
    SecureDirectory,
    SecurePersistenceError,
)

from ..authority.v4 import AuthorityStore
from ..authority.v4_models import authority_digest
from ..env_compat import read_migrated_env
from ..active_profile_store_v4 import ActiveProfileStore, ActiveProfileStoreError
from ..profile_definition_store_v4 import (
    ProfileDefinitionStore,
    ProfileDefinitionStoreConflict,
)
from ..profile_runtime_port import require_profile_runtime


class ProfileResolutionDenied(Exception):
    """Construct the configured application's canonical Profile denial.

    The concrete exception remains application-owned; this proxy exists only
    so the Host can raise a fail-closed denial without importing that class.
    """

    def __new__(cls, message: str):
        return require_profile_runtime().denied(message)


_ProfilePointerSignature = tuple[int, int, int, int]
_ProfileCaptureCacheEntry = tuple[Any, _ProfilePointerSignature]
_PROFILE_CAPTURE_SCOPE: ContextVar[dict[Path, _ProfileCaptureCacheEntry] | None] = ContextVar(
    "tobkiri_profile_capture_scope", default=None
)


@contextmanager
def profile_capture_scope() -> Iterator[None]:
    """Bound repeated capture reads to one explicit operation scope.

    The scope is intentionally opt-in and context-local.  It never becomes a
    process-wide cache: callers open a new scope for each operation, and
    mutation code invalidates it before recapturing state.
    """

    existing = _PROFILE_CAPTURE_SCOPE.get()
    if existing is not None:
        yield
        return
    token = _PROFILE_CAPTURE_SCOPE.set({})
    try:
        yield
    finally:
        _PROFILE_CAPTURE_SCOPE.reset(token)


def invalidate_profile_capture_scope() -> None:
    """Discard the current operation's cached activation snapshot."""

    cache = _PROFILE_CAPTURE_SCOPE.get()
    if cache is not None:
        cache.clear()


def _activation_pointer_signature(path: Path) -> _ProfilePointerSignature | None:
    """Return a cheap identity/version marker for the canonical active pointer."""

    try:
        stat_result = path.lstat()
    except OSError:
        return None
    if not path.is_file() or path.is_symlink():
        return None
    return (
        int(stat_result.st_dev),
        int(stat_result.st_ino),
        int(stat_result.st_size),
        int(stat_result.st_mtime_ns),
    )


def cache_active_profile(
    active: Any,
    *,
    user_data: Path | None = None,
) -> None:
    """Seed the current operation scope after an atomic Profile activation."""

    cache = _PROFILE_CAPTURE_SCOPE.get()
    if cache is None:
        return
    root = user_data or runtime_user_data_root()
    pointer_path = ActiveProfileStore(root).path
    signature = _activation_pointer_signature(pointer_path)
    if signature is not None:
        cache[pointer_path] = (active, signature)


def runtime_user_data_root(base_dir: Path | None = None) -> Path:
    """Return the configured Host state root without an authority fallback."""
    configured = read_migrated_env("TOBKIRI_USER_DATA", "RUMI_USER_DATA")
    if configured:
        return Path(configured).resolve()
    runtime_root = base_dir or Path(__file__).resolve().parents[2]
    return (runtime_root / "user_data").resolve()


def _development_bundle_root(runtime_root: Path) -> Path | None:
    """Return the verified generated bundle only for an explicit source launch."""

    if os.environ.get("RUMI_ENVIRONMENT") != "development":
        return None
    configured_app = os.environ.get("RUMI_APP_DIR")
    try:
        configured_root = Path(configured_app).resolve(strict=True) if configured_app else None
    except OSError:
        return None
    bundled_development_root = runtime_root / "bundled" / "dev-defaults"
    bundled_development_bundle = bundled_development_root / "v4"
    bundled_development_artifacts = bundled_development_root / "platform-artifacts"
    if (
        configured_root == runtime_root.resolve()
        and bundled_development_bundle.is_dir()
        and not bundled_development_bundle.is_symlink()
        and bundled_development_artifacts.is_dir()
        and not bundled_development_artifacts.is_symlink()
    ):
        return bundled_development_bundle
    development_root = (
        runtime_root.parent
        / "tobkiri_launcher"
        / "src-tauri"
        / "target"
        / "dev-defaults"
    )
    development_bundle = development_root / "v4"
    development_artifacts = development_root / "platform-artifacts"
    if (
        configured_root == runtime_root.resolve()
        and development_bundle.is_dir()
        and not development_bundle.is_symlink()
        and development_artifacts.is_dir()
        and not development_artifacts.is_symlink()
    ):
        return development_bundle
    return None


def _user_data_root(base_dir: Path | None = None) -> Path:
    """Normalize optional roots without breaking zero-argument adapters."""

    if base_dir is None:
        return runtime_user_data_root()
    return runtime_user_data_root(base_dir)


def _bootstrap_profile_id() -> str:
    """Return the Pack-owned bootstrap identity after Host path validation."""

    value = require_profile_runtime().bootstrap_profile_id()
    try:
        return validate_canonical_id(value, field="bootstrap_profile_id")
    except Exception as error:
        raise ProfileResolutionDenied(
            "application bootstrap Profile identity is not canonical"
        ) from error


def _bundle_root(base_dir: Path | None = None) -> Path:
    """Return the application-composed sealed Profile bundle location.

    A source-development launch may use the exact generated development
    bundle after its runtime and artifact roots have been checked.  All other
    launches use the application-composed packaged root and verify its Host
    resource binding.  Tests that need a packaged fixture replace this
    dependency in their own process.
    """

    del base_dir
    runtime = require_profile_runtime()
    runtime_root = runtime.host_resource_root()
    development_bundle = _development_bundle_root(runtime_root)
    if development_bundle is not None:
        return development_bundle
    bundle_root = runtime.bundled_profile_root()
    _verify_installed_bundle_binding(runtime_root, bundle_root)
    return bundle_root


def host_profile_catalog(
    base_dir: Path | None = None,
    *,
    bundle_root: Path | None = None,
    user_data_root: Path | None = None,
) -> Any:
    """Return artifacts plus Host-owned Profile definitions.

    ``user_data_root`` is reserved for callers that already bound the Host
    state to a canonical Authority path.  It takes precedence over the
    ambient environment so a recapture cannot mix an Authority store from one
    Host with Profile definitions from another.
    """

    runtime = require_profile_runtime()
    bundled = runtime.load_catalog(_bundle_root(base_dir) if bundle_root is None else bundle_root)
    user_data = (
        Path(user_data_root).resolve() if user_data_root is not None else _user_data_root(base_dir)
    )
    definitions = ProfileDefinitionStore(user_data)
    legacy_collection = user_data / "settings" / "startup_profiles.json"
    if not definitions.list_profiles(include_tombstones=True) and legacy_collection.is_file():
        definitions.import_legacy_collection(
            legacy_collection,
            migration_catalog=bundled,
        )
    definitions.migrate_legacy_successors(bundled)
    definitions.repair_legacy_display_names()
    definitions.migrate_retired_pack_projections()
    # Defaults is an install/bootstrap template, not a prerequisite Profile.
    # Once any live definition exists, preserve that collection exactly and
    # never inject a special execution identity into it.
    if definitions.bootstrap_state().get("state") == "empty":
        try:
            definitions.bootstrap_defaults(bundled.profiles[_bootstrap_profile_id()])
        except KeyError as error:
            raise ProfileResolutionDenied(
                "application bootstrap Profile is unavailable from its catalog"
            ) from error
        except ProfileDefinitionStoreConflict:
            if definitions.bootstrap_state().get("state") == "empty":
                raise
    profiles = {
        item.profile_id: dict(item.profile)
        for item in definitions.list_profiles()
        if _is_resolvable_profile_definition(item.profile)
    }
    return runtime.catalog_with_profiles(bundled, profiles)


def _is_resolvable_profile_definition(profile: Mapping[str, Any]) -> bool:
    """Keep opaque legacy imports visible in the store but out of resolution."""

    return all(
        key in profile
        for key in (
            "profile_api_version",
            "profile_id",
            "mode",
            "base",
            "shell",
            "packs",
            "requested_edges",
            "authority_references",
            "profile_authority_snapshot_digest",
            "provenance",
        )
    )


def _verify_installed_bundle_binding(runtime_root: Path, bundle_root: Path) -> None:
    """Bind a packaged Profile bundle to the launcher's resource manifest."""

    artifact_root = bundle_root.parent / "platform-artifacts"
    if not artifact_root.exists():
        # A source checkout contains no executable Profile artifact and will
        # fail closed during resolution.  It is not a production override.
        return
    manifest_path = runtime_root / "runtime-resource-manifest.v1.json"
    if manifest_path.is_symlink() or not manifest_path.is_file():
        raise ProfileResolutionDenied("packaged runtime resource manifest is unavailable")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ProfileResolutionDenied("packaged runtime resource manifest is invalid") from exc
    if manifest.get("schema") != "io.tobkiri.runtime-resource-manifest.v1":
        raise ProfileResolutionDenied("packaged runtime resource manifest is unsupported")
    entries = manifest.get("entries")
    if not isinstance(entries, list):
        raise ProfileResolutionDenied("packaged runtime resource inventory is invalid")
    expected: dict[str, tuple[int, str]] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            raise ProfileResolutionDenied("packaged runtime resource entry is invalid")
        path = entry.get("path")
        size = entry.get("size")
        digest = entry.get("sha256")
        if (
            not isinstance(path, str)
            or Path(path).is_absolute()
            or ".." in Path(path).parts
            or not isinstance(size, int)
            or isinstance(size, bool)
            or size < 0
            or not isinstance(digest, str)
            or len(digest) != 64
            or any(character not in "0123456789abcdef" for character in digest)
            or path in expected
        ):
            raise ProfileResolutionDenied("packaged runtime resource entry is unsafe")
        expected[path] = (size, digest)
    roots = (bundle_root, artifact_root)
    prefixes = tuple(root.relative_to(runtime_root).as_posix() + "/" for root in roots)
    actual_paths: set[str] = set()
    for root in roots:
        if root.is_symlink() or not root.is_dir():
            raise ProfileResolutionDenied("packaged Profile resource root is unsafe")
        for current, directories, filenames in os.walk(root, followlinks=False):
            current_path = Path(current)
            children = tuple(directories) + tuple(filenames)
            if current_path.is_symlink() or any(
                (current_path / child).is_symlink() for child in children
            ):
                raise ProfileResolutionDenied("packaged Profile resource contains a symlink")
            for filename in filenames:
                path = current_path / filename
                if not path.is_file():
                    raise ProfileResolutionDenied(
                        "packaged Profile resource contains a non-file entry"
                    )
                relative = path.relative_to(runtime_root).as_posix()
                actual_paths.add(relative)
                binding = expected.get(relative)
                if binding is None:
                    raise ProfileResolutionDenied(
                        f"packaged Profile resource is unlisted: {relative}"
                    )
                payload = path.read_bytes()
                if binding != (len(payload), hashlib.sha256(payload).hexdigest()):
                    raise ProfileResolutionDenied(
                        f"packaged Profile resource is not launcher-bound: {relative}"
                    )
    expected_paths = {
        path for path in expected if any(path.startswith(prefix) for prefix in prefixes)
    }
    if actual_paths != expected_paths:
        raise ProfileResolutionDenied("packaged Profile resource inventory is incomplete")


def _edge_key(edge: Mapping[str, Any]) -> str:
    return "|".join(
        str(edge[field])
        for field in (
            "caller_function_id",
            "target_provider_id",
            "contract_id",
            "operation_id",
        )
    )


def _authority_reference(edge: Mapping[str, Any], snapshot_digest: str) -> str:
    digest = canonical_digest(
        {
            "schema": "io.tobkiri.profile-authority-edge.v1",
            "edge": _edge_key(edge),
            "profile_authority_snapshot_digest": snapshot_digest,
        }
    )
    return f"authority-ref:{digest.removeprefix('sha256:')}"


def _authority_snapshot_digest(store: AuthorityStore, bundle_lock_digest: str) -> str:
    epoch = store.security_epoch_record
    return canonical_digest(
        {
            "schema": "io.tobkiri.profile-authority-snapshot.v1",
            "security_epoch": epoch.value,
            "security_epoch_reason_digest": epoch.reason_digest,
            "bundle_lock_digest": bundle_lock_digest,
            "grant_import": "none",
        }
    )


def _genesis_authority_snapshot_digest(bundle_lock_digest: str) -> str:
    """Return the read-only snapshot a new Authority store will initialize."""

    return canonical_digest(
        {
            "schema": "io.tobkiri.profile-authority-snapshot.v1",
            "security_epoch": 1,
            "security_epoch_reason_digest": authority_digest({"reason": "genesis"}),
            "bundle_lock_digest": bundle_lock_digest,
            "grant_import": "none",
        }
    )


def _resolve_bootstrap_candidate(*, base_dir: Path | None = None) -> tuple[Any, dict[str, Any]]:
    """Resolve the finite Pack-selected bootstrap candidate without writing."""

    user_data = _user_data_root(base_dir)
    bundle_root = _bundle_root(base_dir)
    runtime = require_profile_runtime()
    catalog = runtime.load_catalog(bundle_root)
    bundle_lock_digest = (
        "sha256:" + hashlib.sha256((bundle_root / "bundle.lock.json").read_bytes()).hexdigest()
    )
    authority_path = user_data / "authority" / "v4.sqlite3"
    if authority_path.is_file():
        with AuthorityStore(authority_path) as authority:
            security_epoch = authority.security_epoch
            snapshot_digest = _authority_snapshot_digest(authority, bundle_lock_digest)
    elif authority_path.exists():
        raise ProfileResolutionDenied("Authority store path is not a regular file")
    else:
        security_epoch = 1
        snapshot_digest = _genesis_authority_snapshot_digest(bundle_lock_digest)
    profile_id = _bootstrap_profile_id()
    source_profile = catalog.profiles.get(profile_id)
    if source_profile is None:
        raise ProfileResolutionDenied("bundled setup Profile is missing")
    authority_bindings = {
        _edge_key(edge): _authority_reference(edge, snapshot_digest)
        for edge in source_profile["requested_edges"]
    }
    verified_artifacts = {
        str(manifest["pack"]["artifact_digest"]) for manifest in catalog.packs.values()
    }
    resolved = runtime.resolve_profile(
        catalog,
        profile_id,
        approved_artifact_digests=verified_artifacts,
        authority_snapshot_digest=snapshot_digest,
        authority_bindings=authority_bindings,
        security_epoch=security_epoch,
    )
    return resolved, dict(
        runtime.bootstrap_confirmation(
            resolved=resolved,
            profile_id=profile_id,
            authority_snapshot_digest=snapshot_digest,
            security_epoch=security_epoch,
        )
    )


def _resolve_profile_candidate(
    profile_id: str,
    *,
    base_dir: Path | None = None,
) -> tuple[Any, dict[str, Any]]:
    """Resolve one registry Profile through the bootstrap authority ceremony."""

    user_data = _user_data_root(base_dir)
    catalog = host_profile_catalog(base_dir)
    source_profile = catalog.profiles.get(profile_id)
    if source_profile is None:
        raise ProfileResolutionDenied("Profile is unavailable from the Host registry")
    bundle_root = Path(catalog.root)
    bundle_lock_path = bundle_root / "bundle.lock.json"
    if bundle_lock_path.is_symlink() or not bundle_lock_path.is_file():
        raise ProfileResolutionDenied("Profile bundle lock is unavailable")
    bundle_lock_digest = "sha256:" + hashlib.sha256(bundle_lock_path.read_bytes()).hexdigest()
    authority_path = user_data / "authority" / "v4.sqlite3"
    if authority_path.is_file():
        with AuthorityStore(authority_path) as authority:
            security_epoch = authority.security_epoch
            snapshot_digest = _authority_snapshot_digest(authority, bundle_lock_digest)
    elif authority_path.exists():
        raise ProfileResolutionDenied("Authority store path is not a regular file")
    else:
        security_epoch = 1
        snapshot_digest = _genesis_authority_snapshot_digest(bundle_lock_digest)
    authority_bindings = {
        _edge_key(edge): _authority_reference(edge, snapshot_digest)
        for edge in source_profile.get("requested_edges", [])
    }
    verified_artifacts = {
        str(manifest["pack"]["artifact_digest"]) for manifest in catalog.packs.values()
    }
    resolved = require_profile_runtime().resolve_profile(
        catalog,
        profile_id,
        approved_artifact_digests=verified_artifacts,
        authority_snapshot_digest=snapshot_digest,
        authority_bindings=authority_bindings,
        security_epoch=security_epoch,
    )
    return resolved, dict(
        require_profile_runtime().profile_confirmation(
            resolved=resolved,
            profile_id=profile_id,
            authority_snapshot_digest=snapshot_digest,
            security_epoch=security_epoch,
        )
    )


def _ensure_profile_workspace(user_data: Path, profile_id: str) -> Path:
    """Create the non-authoritative, Profile-scoped workspace layout."""

    workspace = user_data / "workspaces" / profile_id
    if workspace.is_symlink():
        raise ProfileResolutionDenied("Profile workspace must not be symlinked")
    workspace.mkdir(mode=0o700, parents=True, exist_ok=True)
    for name in (
        "activation",
        "state",
        "packs",
        "conversation",
        "settings",
        "credentials",
        "handoff",
        "audit",
        "artifacts",
        "snapshots",
    ):
        directory = workspace / name
        if directory.is_symlink() or directory.exists() and not directory.is_dir():
            raise ProfileResolutionDenied("Profile workspace contains an unsafe state directory")
        directory.mkdir(mode=0o700, exist_ok=True)
    return workspace


def prepare_bootstrap_profile_confirmation(*, base_dir: Path | None = None) -> dict[str, Any]:
    """Return an exact, read-only bootstrap confirmation bound to the catalog."""

    _resolved, confirmation = _resolve_bootstrap_candidate(base_dir=base_dir)
    return confirmation


def prepare_default_profile_confirmation(*, base_dir: Path | None = None) -> dict[str, Any]:
    """Compatibility alias for the application bootstrap confirmation."""

    return prepare_bootstrap_profile_confirmation(base_dir=base_dir)


def prepare_profile_confirmation(
    profile_id: str,
    *,
    base_dir: Path | None = None,
) -> dict[str, Any]:
    """Return a read-only confirmation for any live Named Profile."""

    _resolved, confirmation = _resolve_profile_candidate(
        profile_id,
        base_dir=base_dir,
    )
    return confirmation


def capture_profile(
    profile_id: str,
    *,
    base_dir: Path | None = None,
    confirmation: Mapping[str, Any] | None = None,
) -> Any:
    """Capture or activate any Named Profile through the canonical v4 ceremony.

    The Profile workspace owns only that Profile's activation history and
    state.  The committed active selection is always the Host-global pointer,
    so changing from one Profile to another never writes selection state into
    the previous Profile workspace.
    """

    try:
        safe_profile_id = validate_canonical_id(profile_id, field="profile_id")
    except Exception as error:
        raise ProfileResolutionDenied("Profile ID is not canonical") from error
    user_data = _user_data_root(base_dir)
    workspace = user_data / "workspaces" / safe_profile_id
    state_root = workspace / "activation"
    active_pointer = state_root / "active.json"
    if workspace.is_symlink() or state_root.is_symlink() or active_pointer.is_symlink():
        raise ProfileResolutionDenied("Profile activation state must not be symlinked")

    pointers = ActiveProfileStore(user_data)
    current = pointers.load(verify_snapshot=True)
    target_exists = active_pointer.is_file()
    if active_pointer.exists() and not target_exists:
        raise ProfileResolutionDenied("Profile activation pointer is not a regular file")

    if target_exists and confirmation is None and current is not None:
        if current.profile_id != safe_profile_id:
            raise ProfileResolutionDenied("explicit Profile activation confirmation is required")
        _ensure_profile_workspace(user_data, safe_profile_id)
        catalog = host_profile_catalog(base_dir)
        with AuthorityStore(user_data / "authority" / "v4.sqlite3") as authority:
            active = (
                require_profile_runtime()
                .activation_store(
                    root=state_root,
                    workspace=workspace,
                    profile_id=safe_profile_id,
                    authority=authority,
                    catalog=catalog,
                )
                .load_active_snapshot()
            )
        _publish_host_active_pointer(
            active,
            user_data=user_data,
            replace_existing=False,
        )
        cache_active_profile(active, user_data=user_data)
        return active

    if confirmation is None:
        raise ProfileResolutionDenied("explicit Profile activation confirmation is required")
    resolved, expected_confirmation = _resolve_profile_candidate(
        safe_profile_id,
        base_dir=base_dir,
    )
    if dict(confirmation) != expected_confirmation:
        raise ProfileResolutionDenied("Profile activation confirmation is stale or tampered")
    workspace = _ensure_profile_workspace(user_data, safe_profile_id)
    with AuthorityStore(user_data / "authority" / "v4.sqlite3") as authority:
        store = require_profile_runtime().activation_store(
            root=state_root,
            workspace=workspace,
            profile_id=safe_profile_id,
            authority=authority,
            catalog=host_profile_catalog(base_dir),
        )
        store.recover()
        predecessor = store.load_active_snapshot() if target_exists else None
        activation_id = (
            f"activation:{safe_profile_id}-"
            + resolved.plan["plan_digest"].removeprefix("sha256:")[:16]
        )
        created_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        predecessor_bindings = (
            {}
            if predecessor is None
            else {
                "expected_predecessor_profile_revision": str(
                    predecessor.resolved.plan["profile_revision"]
                ),
                "expected_predecessor_plan_digest": str(predecessor.resolved.plan["plan_digest"]),
                "expected_predecessor_activation_id": str(predecessor.activation["activation_id"]),
            }
        )
        store.activate(
            resolved,
            activation_id=activation_id,
            created_at=created_at,
            **predecessor_bindings,
        )
        active = store.load_active_snapshot()
    _publish_host_active_pointer(
        active,
        user_data=user_data,
        replace_existing=True,
    )
    cache_active_profile(active, user_data=user_data)
    return active


def active_bootstrap_profile_exists(*, base_dir: Path | None = None) -> bool:
    """Return whether the Pack-selected bootstrap activation exists."""

    user_data = _user_data_root(base_dir)
    profile_id = _bootstrap_profile_id()
    pointer = user_data / "workspaces" / profile_id / "activation" / "active.json"
    return pointer.is_file()


def active_default_profile_exists(*, base_dir: Path | None = None) -> bool:
    """Compatibility alias for the bootstrap activation existence check."""

    return active_bootstrap_profile_exists(base_dir=base_dir)


def active_profile_exists(*, base_dir: Path | None = None) -> bool:
    """Return whether the Host-global Profile selection physically exists."""

    user_data = _user_data_root(base_dir)
    return ActiveProfileStore(user_data).path.is_file()


def repair_legacy_active_profile_pointer(*, base_dir: Path | None = None) -> Any | None:
    """Validate legacy selection without manufacturing execution authority.

    The v1 ``profiles/active_profile.json`` marker contains only a legacy ID;
    it cannot authorize execution or supply a Profile revision, ProfileLock,
    ResolvedPlan, approval, activation, fence, or SecurityEpoch.  A matching
    imported Named Profile is kept visible, but no Host-global execution
    pointer is published until that same Profile identity completes the normal
    activation ceremony. In particular, a legacy Profile is never silently
    replaced with the Pack-selected bootstrap Profile identity.
    """

    user_data = _user_data_root(base_dir)
    pointers = ActiveProfileStore(user_data)
    if pointers.path.is_file():
        pointers.require(verify_snapshot=True)
        return None

    try:
        profiles_directory = SecureDirectory(user_data / "profiles", create=True)
        if not profiles_directory.exists("active_profile.json"):
            return None
        legacy_pointer = strict_loads(
            profiles_directory.read_bytes_bounded(
                "active_profile.json",
                max_bytes=16 * 1024,
            )
        )
    except (OSError, SecurePersistenceError, ValueError) as error:
        raise ProfileResolutionDenied(
            "legacy active Profile marker is unsafe or unreadable"
        ) from error
    if (
        not isinstance(legacy_pointer, Mapping)
        or set(legacy_pointer) != {"version", "active_profile_id"}
        or legacy_pointer.get("version") != 1
    ):
        raise ProfileResolutionDenied("legacy active Profile marker is invalid")
    legacy_id_value = legacy_pointer.get("active_profile_id")
    if not isinstance(legacy_id_value, str):
        raise ProfileResolutionDenied("legacy active Profile marker identity is invalid")
    try:
        legacy_id = validate_canonical_id(
            legacy_id_value,
            field="active_profile_id",
        )
    except Exception as error:
        raise ProfileResolutionDenied("legacy active Profile marker identity is invalid") from error

    definitions = ProfileDefinitionStore(user_data)
    legacy_state = definitions.legacy_state()
    source_document = legacy_state.get("source_document")
    if (
        legacy_state.get("active_profile_id") != legacy_id
        or not isinstance(source_document, Mapping)
        or source_document.get("active_profile_id") != legacy_id
    ):
        raise ProfileResolutionDenied(
            "legacy active Profile marker does not match the imported registry"
        )
    selected = definitions.get_profile(legacy_id)
    if selected is None:
        raise ProfileResolutionDenied("legacy active Profile is unavailable")
    return None


def activation_audit_receipt(active: Any, *, base_dir: Path | None = None) -> dict[str, Any]:
    """Return the committed Authority reservation bound to an activation."""

    user_data = _user_data_root(base_dir)
    with AuthorityStore(user_data / "authority" / "v4.sqlite3") as authority:
        reservation = authority.active_activation_reservation(
            str(active.activation["activation_id"])
        )
    if reservation is None or (
        reservation.get("state") != "active"
        or reservation.get("plan_digest") != active.activation["plan_digest"]
        or reservation.get("fencing_token") != active.activation["fencing_token"]
    ):
        raise ProfileResolutionDenied("activation audit commit is unavailable")
    return {
        "reservation_id": str(reservation["reservation_id"]),
        "state": "committed",
        "activation_id": str(reservation["activation_id"]),
        "fencing_token": int(reservation["fencing_token"]),
    }


def capture_active_profile(*, base_dir: Path | None = None) -> Any:
    """Capture the exact Host-selected Profile without a bootstrap fallback."""

    user_data = _user_data_root(base_dir)
    pointers = ActiveProfileStore(user_data)
    cache = _PROFILE_CAPTURE_SCOPE.get()
    if cache is not None:
        signature = _activation_pointer_signature(pointers.path)
        cached = cache.get(pointers.path)
        if cached is not None and signature == cached[1]:
            return cached[0]
    try:
        pointer = pointers.require(verify_snapshot=True)
    except ActiveProfileStoreError:
        # Compatibility migration only: an existing pre-registry bootstrap
        # activation is promoted once. Absence of both pointers remains
        # fail-closed and never creates application state here.
        has_legacy_bootstrap = (
            active_bootstrap_profile_exists()
            if base_dir is None
            else active_bootstrap_profile_exists(base_dir=base_dir)
        )
        if pointers.path.is_file() or not has_legacy_bootstrap:
            raise
        if base_dir is None:
            capture_bootstrap_profile()
        else:
            capture_bootstrap_profile(base_dir=base_dir)
        pointer = pointers.require(verify_snapshot=True)
    workspace = user_data / "workspaces" / pointer.profile_id
    if workspace.is_symlink() or not workspace.is_dir():
        raise ProfileResolutionDenied("active Profile workspace is unavailable")
    catalog = host_profile_catalog(base_dir)
    with AuthorityStore(user_data / "authority" / "v4.sqlite3") as authority:
        store = require_profile_runtime().activation_store(
            root=workspace / "activation",
            workspace=workspace,
            profile_id=pointer.profile_id,
            authority=authority,
            catalog=catalog,
        )
        active = store.load_active_snapshot()
    identity = (
        str(active.resolved.plan["profile_revision"]),
        str(active.activation["activation_id"]),
        str(active.resolved.plan["plan_digest"]),
        str(active.resolved.lock["lock_digest"]),
    )
    if identity != (
        pointer.profile_revision,
        pointer.activation_id,
        pointer.plan_digest,
        pointer.lock_digest,
    ):
        raise ProfileResolutionDenied("Host active pointer does not match the Profile activation")
    cache_active_profile(active, user_data=user_data)
    return active


def _publish_host_active_pointer(
    active: Any,
    *,
    user_data: Path,
    replace_existing: bool,
) -> None:
    """Publish one verified workspace activation through the Host-global CAS."""

    activation_id = str(active.activation["activation_id"])
    profile_id = str(active.resolved.profile["profile_id"])
    relative = (
        Path("workspaces")
        / profile_id
        / "activation"
        / "activations"
        / f"{activation_id.removeprefix('activation:')}.json"
    )
    try:
        snapshot = strict_loads((user_data / relative).read_bytes())
    except (OSError, ValueError) as error:
        raise ProfileResolutionDenied(
            "active Profile activation envelope is unavailable"
        ) from error
    if not isinstance(snapshot, Mapping):
        raise ProfileResolutionDenied("active Profile activation envelope is invalid")
    pointers = ActiveProfileStore(user_data)
    current = pointers.load(verify_snapshot=True)
    if current is not None and not replace_existing:
        return
    pointers.commit_activation(
        active.activation,
        activation_snapshot=snapshot,
        activation_snapshot_path=relative.as_posix(),
        expected=current,
        catalog_revision=str(active.resolved.plan.get("catalog_revision") or "") or None,
    )


def capture_bootstrap_profile(
    *,
    base_dir: Path | None = None,
    confirmation: Mapping[str, Any] | None = None,
) -> Any:
    """Load or create the Pack-selected verified bootstrap activation.

    Creation is one finite transaction: verify the locked bundle, capture the
    Authority Kernel epoch, resolve the application-selected Profile, and
    atomically activate it. Restart only reloads the digest-bound envelope.
    """
    user_data = _user_data_root(base_dir)
    runtime = require_profile_runtime()
    profile_id = _bootstrap_profile_id()
    state_root = user_data / "workspaces" / profile_id / "activation"
    active_pointer = state_root / "active.json"
    if state_root.is_symlink() or active_pointer.is_symlink():
        raise ProfileResolutionDenied("bootstrap activation state must not be symlinked")
    cache = _PROFILE_CAPTURE_SCOPE.get()
    if confirmation is None and cache is not None:
        signature = _activation_pointer_signature(active_pointer)
        cached = cache.get(user_data)
        if cached is not None:
            if signature == cached[1]:
                return cached[0]
            cache.pop(user_data, None)
    if active_pointer.is_file():
        workspace = user_data / "workspaces" / profile_id
        catalog = runtime.load_catalog(_bundle_root(base_dir))
        resolved_reconciliation: Any | None = None
        if confirmation is not None:
            resolved_reconciliation, expected_confirmation = _resolve_bootstrap_candidate(
                base_dir=base_dir
            )
            if dict(confirmation) != expected_confirmation:
                raise ProfileResolutionDenied(
                    "bootstrap activation confirmation is stale or tampered"
                )
        with AuthorityStore(user_data / "authority" / "v4.sqlite3") as authority:
            store = runtime.activation_store(
                root=state_root,
                workspace=workspace,
                profile_id=profile_id,
                authority=authority,
                catalog=catalog,
            )
            if resolved_reconciliation is not None:
                activation_id = (
                    f"activation:{profile_id}-reconcile-"
                    + resolved_reconciliation.plan["plan_digest"].removeprefix("sha256:")[:16]
                )
                created_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
                store.reconcile_active(
                    resolved_reconciliation,
                    activation_id=activation_id,
                    created_at=created_at,
                )
            active = store.load_active_snapshot()
            _publish_host_active_pointer(
                active,
                user_data=user_data,
                replace_existing=confirmation is not None,
            )
            if confirmation is None and cache is not None:
                signature = _activation_pointer_signature(active_pointer)
                if signature is not None:
                    cache[user_data] = (active, signature)
            return active
    if active_pointer.exists():
        raise ProfileResolutionDenied("active activation pointer is not a regular file")
    if confirmation is None:
        raise ProfileResolutionDenied("explicit bootstrap activation confirmation is required")

    resolved, expected_confirmation = _resolve_bootstrap_candidate(base_dir=base_dir)
    if dict(confirmation) != expected_confirmation:
        raise ProfileResolutionDenied("bootstrap activation confirmation is stale or tampered")
    workspace = user_data / "workspaces" / profile_id
    workspace.mkdir(parents=True, exist_ok=True)
    with AuthorityStore(user_data / "authority" / "v4.sqlite3") as authority:
        store = runtime.activation_store(
            root=state_root,
            workspace=workspace,
            profile_id=profile_id,
            authority=authority,
            catalog=runtime.load_catalog(_bundle_root(base_dir)),
        )
        store.recover()
        activation_id = (
            f"activation:{profile_id}-" + resolved.plan["plan_digest"].removeprefix("sha256:")[:16]
        )
        created_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        store.activate(
            resolved,
            activation_id=activation_id,
            created_at=created_at,
        )
        active = store.load_active_snapshot()
        _publish_host_active_pointer(
            active,
            user_data=user_data,
            replace_existing=True,
        )
        if cache is not None:
            signature = _activation_pointer_signature(active_pointer)
            if signature is not None:
                cache[user_data] = (active, signature)
        return active


def capture_default_profile(
    *,
    base_dir: Path | None = None,
    confirmation: Mapping[str, Any] | None = None,
) -> Any:
    """Compatibility alias for the Pack-selected bootstrap activation."""

    return capture_bootstrap_profile(
        base_dir=base_dir,
        confirmation=confirmation,
    )


__all__ = [
    "activation_audit_receipt",
    "active_bootstrap_profile_exists",
    "active_profile_exists",
    "active_default_profile_exists",
    "cache_active_profile",
    "capture_active_profile",
    "capture_profile",
    "capture_bootstrap_profile",
    "capture_default_profile",
    "host_profile_catalog",
    "invalidate_profile_capture_scope",
    "prepare_bootstrap_profile_confirmation",
    "prepare_default_profile_confirmation",
    "prepare_profile_confirmation",
    "profile_capture_scope",
    "repair_legacy_active_profile_pointer",
    "runtime_user_data_root",
]
