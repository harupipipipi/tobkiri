"""Finite, restart-safe capture of the bundled Defaults Profile v4."""

from __future__ import annotations

import hashlib
import json
import os
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Mapping

from ecosystem.defaultspack.domain.runtime_v4 import (
    ActivationStore,
    ActiveDefaultProfile,
    BundledCatalog,
    ProfileResolutionDenied,
    ResolvedDefaultProfile,
    resolve_default_profile,
)
from tobkiri_protocol.canonical import canonical_digest

from ..authority.v4 import AuthorityStore
from ..authority.v4_models import authority_digest
from ..env_compat import read_migrated_env


_ProfilePointerSignature = tuple[int, int, int, int]
_ProfileCaptureCacheEntry = tuple[ActiveDefaultProfile, _ProfilePointerSignature]
_PROFILE_CAPTURE_SCOPE: ContextVar[
    dict[Path, _ProfileCaptureCacheEntry] | None
] = ContextVar("tobkiri_profile_capture_scope", default=None)


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


def runtime_user_data_root(base_dir: Path | None = None) -> Path:
    """Return the configured Host state root without an authority fallback."""
    configured = read_migrated_env("TOBKIRI_USER_DATA", "RUMI_USER_DATA")
    if configured:
        return Path(configured).resolve()
    runtime_root = base_dir or Path(__file__).resolve().parents[2]
    return (runtime_root / "user_data").resolve()


def _bundle_root(base_dir: Path | None = None) -> Path:
    """Return the compile-time installed Defaults bundle location.

    Environment is intentionally not consulted here.  Tests that need a
    packaged fixture replace this dependency in their own process.
    """

    del base_dir
    runtime_root = Path(__file__).resolve().parents[2]
    bundle_root = runtime_root / "ecosystem" / "defaultspack" / "v4"
    _verify_installed_bundle_binding(runtime_root, bundle_root)
    return bundle_root


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
    prefixes = tuple(
        root.relative_to(runtime_root).as_posix() + "/" for root in roots
    )
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
                raise ProfileResolutionDenied(
                    "packaged Profile resource contains a symlink"
                )
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
                        "packaged Profile resource is not launcher-bound: "
                        f"{relative}"
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


def _authority_snapshot_digest(
    store: AuthorityStore, bundle_lock_digest: str
) -> str:
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


def _resolve_candidate(
    *, base_dir: Path | None = None
) -> tuple[ResolvedDefaultProfile, dict[str, Any]]:
    """Resolve the finite Defaults candidate without writing first-start state."""

    user_data = runtime_user_data_root(base_dir)
    bundle_root = _bundle_root(base_dir)
    catalog = BundledCatalog.load(bundle_root)
    bundle_lock_digest = "sha256:" + hashlib.sha256(
        (bundle_root / "bundle.lock.json").read_bytes()
    ).hexdigest()
    authority_path = user_data / "authority" / "v4.sqlite3"
    if authority_path.is_file():
        with AuthorityStore(authority_path) as authority:
            security_epoch = authority.security_epoch
            snapshot_digest = _authority_snapshot_digest(
                authority, bundle_lock_digest
            )
    elif authority_path.exists():
        raise ProfileResolutionDenied("Authority store path is not a regular file")
    else:
        security_epoch = 1
        snapshot_digest = _genesis_authority_snapshot_digest(bundle_lock_digest)
    source_profile = catalog.profiles.get("defaults")
    if source_profile is None:
        raise ProfileResolutionDenied("bundled defaults Profile is missing")
    authority_bindings = {
        _edge_key(edge): _authority_reference(edge, snapshot_digest)
        for edge in source_profile["requested_edges"]
    }
    verified_artifacts = {
        str(manifest["pack"]["artifact_digest"])
        for manifest in catalog.packs.values()
    }
    resolved = resolve_default_profile(
        catalog,
        "defaults",
        approved_artifact_digests=verified_artifacts,
        authority_snapshot_digest=snapshot_digest,
        authority_bindings=authority_bindings,
        security_epoch=security_epoch,
    )
    confirmation = {
        "confirmation_api_version": "io.tobkiri.defaults-confirmation.v1",
        "operation_id": "defaults.activate",
        "profile_id": "defaults",
        "catalog_revision": resolved.profile["catalog_revision"],
        "profile_revision": resolved.plan["profile_revision"],
        "plan_digest": resolved.plan["plan_digest"],
        "authority_snapshot_digest": snapshot_digest,
        "security_epoch": security_epoch,
        "base": dict(resolved.plan["base"]),
        "shell": dict(resolved.plan["shell"]),
        "bindings": [dict(binding) for binding in resolved.plan["bindings"]],
    }
    confirmation["confirmation_digest"] = canonical_digest(confirmation)
    return resolved, confirmation


def prepare_default_profile_confirmation(
    *, base_dir: Path | None = None
) -> dict[str, Any]:
    """Return an exact, read-only confirmation bound to the current catalog."""

    _resolved, confirmation = _resolve_candidate(base_dir=base_dir)
    return confirmation


def active_default_profile_exists(*, base_dir: Path | None = None) -> bool:
    """Return whether the canonical activation pointer physically exists."""

    pointer = (
        runtime_user_data_root(base_dir)
        / "workspaces"
        / "defaults"
        / "activation"
        / "active.json"
    )
    return pointer.is_file()


def activation_audit_receipt(
    active: ActiveDefaultProfile, *, base_dir: Path | None = None
) -> dict[str, Any]:
    """Return the committed Authority reservation bound to an activation."""

    with AuthorityStore(
        runtime_user_data_root(base_dir) / "authority" / "v4.sqlite3"
    ) as authority:
        reservation = authority.active_activation_reservation(
            str(active.activation["activation_id"])
        )
    if reservation is None or (
        reservation.get("state") != "active"
        or reservation.get("plan_digest") != active.activation["plan_digest"]
        or reservation.get("fencing_token")
        != active.activation["fencing_token"]
    ):
        raise ProfileResolutionDenied("activation audit commit is unavailable")
    return {
        "reservation_id": str(reservation["reservation_id"]),
        "state": "committed",
        "activation_id": str(reservation["activation_id"]),
        "fencing_token": int(reservation["fencing_token"]),
    }


def capture_default_profile(
    *,
    base_dir: Path | None = None,
    confirmation: Mapping[str, Any] | None = None,
) -> ActiveDefaultProfile:
    """Load or create the sole verified bundled Defaults activation.

    Creation is one finite transaction: verify the locked bundle, capture the
    Authority Kernel epoch, resolve the named ``defaults`` Profile, and atomically
    activate it.  Restart only reloads the digest-bound activation envelope.
    """
    user_data = runtime_user_data_root(base_dir)
    state_root = user_data / "workspaces" / "defaults" / "activation"
    active_pointer = state_root / "active.json"
    if state_root.is_symlink() or active_pointer.is_symlink():
        raise ProfileResolutionDenied("Defaults activation state must not be symlinked")
    cache = _PROFILE_CAPTURE_SCOPE.get()
    if confirmation is None and cache is not None:
        signature = _activation_pointer_signature(active_pointer)
        cached = cache.get(user_data)
        if cached is not None:
            if signature == cached[1]:
                return cached[0]
            cache.pop(user_data, None)
    if active_pointer.is_file():
        workspace = user_data / "workspaces" / "defaults"
        catalog = BundledCatalog.load(_bundle_root(base_dir))
        resolved_reconciliation: ResolvedDefaultProfile | None = None
        if confirmation is not None:
            resolved_reconciliation, expected_confirmation = _resolve_candidate(
                base_dir=base_dir
            )
            if dict(confirmation) != expected_confirmation:
                raise ProfileResolutionDenied(
                    "Defaults activation confirmation is stale or tampered"
                )
        with AuthorityStore(user_data / "authority" / "v4.sqlite3") as authority:
            store = ActivationStore(
                state_root,
                workspace,
                profile_id="defaults",
                authority=authority,
                catalog=catalog,
            )
            if resolved_reconciliation is not None:
                activation_id = (
                    "activation:defaults-reconcile-"
                    + resolved_reconciliation.plan["plan_digest"].removeprefix(
                        "sha256:"
                    )[:16]
                )
                created_at = datetime.now(timezone.utc).isoformat().replace(
                    "+00:00", "Z"
                )
                store.reconcile_active(
                    resolved_reconciliation,
                    activation_id=activation_id,
                    created_at=created_at,
                )
            active = store.load_active_snapshot()
            if confirmation is None and cache is not None:
                signature = _activation_pointer_signature(active_pointer)
                if signature is not None:
                    cache[user_data] = (active, signature)
            return active
    if active_pointer.exists():
        raise ProfileResolutionDenied("active activation pointer is not a regular file")
    if confirmation is None:
        raise ProfileResolutionDenied(
            "explicit Defaults activation confirmation is required"
        )

    resolved, expected_confirmation = _resolve_candidate(base_dir=base_dir)
    if dict(confirmation) != expected_confirmation:
        raise ProfileResolutionDenied(
            "Defaults activation confirmation is stale or tampered"
        )
    workspace = user_data / "workspaces" / "defaults"
    workspace.mkdir(parents=True, exist_ok=True)
    with AuthorityStore(user_data / "authority" / "v4.sqlite3") as authority:
        store = ActivationStore(
            state_root,
            workspace,
            profile_id="defaults",
            authority=authority,
            catalog=BundledCatalog.load(_bundle_root(base_dir)),
        )
        store.recover()
        activation_id = (
            "activation:defaults-"
            + resolved.plan["plan_digest"].removeprefix("sha256:")[:16]
        )
        created_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        store.activate(
            resolved,
            activation_id=activation_id,
            created_at=created_at,
        )
        active = store.load_active_snapshot()
        if cache is not None:
            signature = _activation_pointer_signature(active_pointer)
            if signature is not None:
                cache[user_data] = (active, signature)
        return active


__all__ = [
    "activation_audit_receipt",
    "active_default_profile_exists",
    "capture_default_profile",
    "invalidate_profile_capture_scope",
    "prepare_default_profile_confirmation",
    "profile_capture_scope",
    "runtime_user_data_root",
]
