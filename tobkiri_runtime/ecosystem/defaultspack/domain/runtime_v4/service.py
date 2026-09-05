"""Resolve and persist the bundled defaults through Protocol v4 only.

This module deliberately has no Registry, legacy ecosystem, route, alias, or
environment fallback. The Host supplies an already-approved artifact set and an
Authority Kernel snapshot. The service can only narrow those inputs into exact
Profile, ProfileLock, ResolvedPlan, and Activation records.
"""

from __future__ import annotations

import hashlib
import importlib
import errno
import os
import re
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping, Protocol

from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.version import InvalidVersion, Version

from tobkiri_protocol.canonical import canonical_digest, canonical_json, strict_loads
from tobkiri_protocol.errors import ProtocolError, SchemaValidationError
from tobkiri_protocol.executable_catalog import materialization_catalog_digest
from tobkiri_protocol.profile_scope import normalize_requested_scope_template
from tobkiri_protocol.platform_artifact import verify_platform_artifact
from tobkiri_protocol.secure_persistence import (
    SecureDirectory,
    SecurePersistenceError,
)
from tobkiri_protocol.validation import validate_document

_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_ACTIVATION_RE = re.compile(r"^activation:[a-z0-9][a-z0-9._-]{7,127}$")
_BUNDLE_SCHEMA = "io.tobkiri.defaultspack-bundle-lock.v1"
_ENVELOPE_SCHEMA = "io.tobkiri.defaultspack-activation-envelope.v1"
_POINTER_SCHEMA = "io.tobkiri.defaultspack-active-pointer.v1"
_PENDING_SCHEMA = "io.tobkiri.defaultspack-pending-activation.v1"
_FOUNDATIONAL_CONTRACT = "conversation.turn.v1"
_AUTHORITY_MODES = frozenset({"profile_grant", "interactive_only"})


class DefaultProfileV4Error(RuntimeError):
    """Base error for the default Profile v4 boundary."""


class BundleIntegrityError(DefaultProfileV4Error):
    """Raised when the finite bundled inventory is invalid or has drifted."""


class ProfileResolutionDenied(DefaultProfileV4Error):
    """Raised when an exact, approved v4 composition cannot be produced."""


class ProfileReconfirmationRequired(ProfileResolutionDenied):
    """Raised when a valid predecessor cannot authorize a changed Profile."""


class ActivationLockTimeout(ProfileResolutionDenied):
    """Raised when the activation process lock is unavailable by its deadline."""


class ActivationAuthority(Protocol):
    """Host-owned authority/audit port used by the fenced activation journal."""

    @property
    def security_epoch(self) -> int: ...

    def reserve_activation(
        self,
        *,
        activation_id: str,
        profile_id: str,
        plan_digest: str,
        profile_authority_digest: str,
        security_epoch: int,
    ) -> tuple[str, int]: ...

    def transition_activation(
        self,
        reservation_id: str,
        *,
        expected_state: str,
        new_state: str,
    ) -> Mapping[str, Any]: ...

    def activation_reservation(self, reservation_id: str) -> Mapping[str, Any] | None: ...

    def incomplete_activation_reservations(
        self, profile_id: str
    ) -> tuple[Mapping[str, Any], ...]: ...

    def active_activation_reservation(self, activation_id: str) -> Mapping[str, Any] | None: ...


def _sha256_bytes(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def _bundle_digest(catalog: "BundledCatalog") -> str:
    """Digest the exact lock bytes that admitted the finite bundle."""

    lock_path = catalog.root / "bundle.lock.json"
    if lock_path.is_symlink() or not lock_path.is_file():
        raise ProfileResolutionDenied("bundle lock is unavailable")
    return _sha256_bytes(lock_path.read_bytes())


def _require_digest(value: object, field: str) -> str:
    if not isinstance(value, str) or _DIGEST_RE.fullmatch(value) is None:
        raise ProfileResolutionDenied(f"{field} must be an exact sha256 digest")
    return value


def _require_optional_pin(
    requested: object,
    actual: str,
    field: str,
) -> None:
    """Reject a stale exact source pin while permitting unresolved ``null``."""

    if requested is not None and requested != actual:
        raise ProfileResolutionDenied(f"{field} is stale or mismatched")


@dataclass(frozen=True)
class BundledCatalog:
    """Finite, digest-verified collection of bundled v4 documents."""

    root: Path
    packs: Mapping[str, Mapping[str, Any]]
    bases: Mapping[str, Mapping[str, Any]]
    shells: Mapping[str, Mapping[str, Any]]
    profiles: Mapping[str, Mapping[str, Any]]
    artifact_root: Path | None = None
    executable_catalogs: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)

    @classmethod
    def load(cls, root: Path, *, artifact_root: Path | None = None) -> "BundledCatalog":
        """Load only files named by ``bundle.lock.json`` and verify every byte."""
        requested_root = Path(root)
        if requested_root.is_symlink():
            raise BundleIntegrityError("bundle root must not be a symlink")
        root = requested_root.resolve(strict=True)
        lock_path = root / "bundle.lock.json"
        if lock_path.is_symlink():
            raise BundleIntegrityError("bundle lock must not be a symlink")
        try:
            lock = strict_loads(lock_path.read_bytes())
        except (OSError, ProtocolError) as exc:
            raise BundleIntegrityError(f"cannot read bundle lock: {exc}") from exc
        if not isinstance(lock, dict) or set(lock) != {"schema", "entries"}:
            raise BundleIntegrityError("bundle lock has unknown or missing fields")
        if lock.get("schema") != _BUNDLE_SCHEMA:
            raise BundleIntegrityError("bundle lock schema is not supported")
        entries = lock.get("entries")
        if not isinstance(entries, list) or not entries:
            raise BundleIntegrityError("bundle lock entries must be a non-empty array")

        collections: dict[str, dict[str, Mapping[str, Any]]] = {
            "pack": {},
            "base": {},
            "shell": {},
            "profile": {},
            "executable_catalog": {},
        }
        identity_fields = {
            "pack": ("pack", "id"),
            "base": (None, "pack_id"),
            "shell": (None, "provider_id"),
            "profile": (None, "profile_id"),
            "executable_catalog": (None, "pack_id"),
        }
        seen_paths: set[str] = set()
        executable_lock_entries: dict[str, tuple[str, str]] = {}
        for index, entry in enumerate(entries):
            if not isinstance(entry, dict) or set(entry) != {"path", "kind", "digest"}:
                raise BundleIntegrityError(f"bundle entry {index} has invalid fields")
            relative = entry.get("path")
            kind = entry.get("kind")
            expected_digest = entry.get("digest")
            if not isinstance(relative, str) or not relative or relative in seen_paths:
                raise BundleIntegrityError(f"bundle entry {index} has an invalid path")
            if kind not in collections:
                raise BundleIntegrityError(f"bundle entry {relative} has an invalid kind")
            if (
                not isinstance(expected_digest, str)
                or _DIGEST_RE.fullmatch(expected_digest) is None
            ):
                raise BundleIntegrityError(f"bundle entry {relative} has an invalid digest")
            candidate = (root / relative).resolve(strict=True)
            if candidate == root or root not in candidate.parents:
                raise BundleIntegrityError(f"bundle entry escapes root: {relative}")
            relative_path = Path(relative)
            current = root
            for part in relative_path.parts:
                current /= part
                if current.is_symlink():
                    raise BundleIntegrityError(f"bundle entry contains a symlink: {relative}")
            raw = candidate.read_bytes()
            actual_digest = _sha256_bytes(raw)
            if actual_digest != expected_digest:
                raise BundleIntegrityError(
                    f"bundle artifact digest changed: {relative} "
                    f"({actual_digest} != {expected_digest})"
                )
            try:
                document = validate_document(raw, kind)
            except SchemaValidationError as exc:
                raise BundleIntegrityError(f"invalid {kind} document {relative}: {exc}") from exc
            if kind in {"base", "shell"}:
                expected_revision = canonical_digest(
                    {key: value for key, value in document.items() if key != "definition_revision"}
                )
                if document["definition_revision"] != expected_revision:
                    raise BundleIntegrityError(
                        f"{kind} definition revision is stale or tampered: {relative}"
                    )
            parent_field, identity_field = identity_fields[kind]
            identity_source = document.get(parent_field) if parent_field else document
            identity = (
                identity_source.get(identity_field) if isinstance(identity_source, dict) else None
            )
            if not isinstance(identity, str) or identity in collections[kind]:
                raise BundleIntegrityError(f"duplicate or missing {kind} identity: {identity!r}")
            collections[kind][identity] = document
            if kind == "executable_catalog":
                executable_lock_entries[identity] = (relative, expected_digest)
            seen_paths.add(relative)
        for pack_id, executable in collections["executable_catalog"].items():
            manifest = collections["pack"].get(pack_id)
            if manifest is None:
                raise BundleIntegrityError(
                    f"executable catalog has no bundled Pack manifest: {pack_id}"
                )
            if executable["source_identity"] != manifest["integrity"]["source_identity"]:
                raise BundleIntegrityError(
                    f"executable catalog source identity is stale: {pack_id}"
                )
            expected_catalog_digest = canonical_digest(
                {key: value for key, value in executable.items() if key != "catalog_digest"}
            )
            if executable["catalog_digest"] != expected_catalog_digest:
                raise BundleIntegrityError(f"executable catalog digest is stale: {pack_id}")
            catalog_entries = [
                item for item in manifest["artifacts"] if item["path"] == "executables.v4.json"
            ]
            if len(catalog_entries) != 1:
                raise BundleIntegrityError(
                    f"Pack manifest does not pin executable catalog: {pack_id}"
                )
            catalog_path, catalog_lock_digest = executable_lock_entries[pack_id]
            catalog_raw = (root / catalog_path).read_bytes()
            catalog_raw_digest = _sha256_bytes(catalog_raw)
            if (
                catalog_entries[0]["digest"] != catalog_raw_digest
                or catalog_lock_digest != catalog_raw_digest
            ):
                raise BundleIntegrityError(
                    f"Pack executable catalog artifact pin is stale: {pack_id}"
                )
        return cls(
            root=root,
            packs=collections["pack"],
            bases=collections["base"],
            shells=collections["shell"],
            profiles=collections["profile"],
            artifact_root=(
                artifact_root.resolve(strict=True)
                if artifact_root
                else (
                    (root.parent / "platform-artifacts").resolve(strict=True)
                    if (root.parent / "platform-artifacts").is_dir()
                    else None
                )
            ),
            executable_catalogs=collections["executable_catalog"],
        )


@dataclass(frozen=True)
class ResolvedDefaultProfile:
    """Schema-valid v4 records ready for Host activation ceremony."""

    profile: Mapping[str, Any]
    lock: Mapping[str, Any]
    plan: Mapping[str, Any]


@dataclass(frozen=True)
class ActiveDefaultProfile:
    """Restart-safe v4 records plus their exact ActivationRecord."""

    resolved: ResolvedDefaultProfile
    activation: Mapping[str, Any]


def _application_launch_identity(
    manifest: Mapping[str, Any],
) -> tuple[str, str, str]:
    """Return the one neutral Application contribution or fail closed."""

    pack_id = str(manifest["pack"]["id"])
    operations = [
        item
        for item in manifest["operation_catalog"]
        if item["owner"] == pack_id and item["operation_id"] == "launch"
    ]
    if len(operations) != 1:
        raise ProfileResolutionDenied("Application launch contribution is ambiguous")
    operation = operations[0]
    providers = [
        item
        for item in manifest["provider_catalog"]
        if item["owner"] == pack_id
        and item["provider_id"] == operation["provider_id"]
        and item["contract_reference"] == operation["contract_reference"]
        and operation["operation_id"] in item["operations"]
    ]
    functions = [
        item
        for item in manifest["functions"]
        if item["id"] == operation["provider_id"]
        and operation["operation_id"] in item["operations"]
    ]
    if len(providers) != 1 or len(functions) != 1:
        raise ProfileResolutionDenied("Application launch contribution is ambiguous")
    provider = providers[0]
    function = functions[0]
    contracts = [
        item
        for item in manifest["contracts"]
        if item["contract_id"] == operation["contract_reference"]
        and item["revision_digest"] == function["contract_revision_digest"]
        and operation["operation_id"] in item["operations"]
    ]
    if len(contracts) != 1:
        raise ProfileResolutionDenied("Application launch contribution is stale")
    return (
        str(provider["provider_id"]),
        str(operation["contract_reference"]),
        str(operation["operation_id"]),
    )


def project_runtime_launch_selector(active: ActiveDefaultProfile) -> dict[str, Any]:
    """Project the exact active launch contribution without catalog fallback.

    The contribution is already part of the canonical ResolvedPlan. This
    projection adds only the Activation identity that selects that immutable
    plan; it deliberately creates no second digest or authority record.
    """

    lock = active.resolved.lock
    plan = active.resolved.plan
    activation = active.activation
    plan_digest = canonical_digest(
        {key: value for key, value in plan.items() if key != "plan_digest"}
    )
    if (plan["profile_id"], plan["profile_revision"], plan_digest) != (
        lock["profile_id"],
        lock["profile_revision"],
        lock["plan_digest"],
    ) or plan_digest != plan["plan_digest"]:
        raise ProfileResolutionDenied("runtime launch selector plan is stale")
    if (
        activation.get("state") != "active"
        or activation.get("profile_id") != plan["profile_id"]
        or activation.get("profile_revision") != plan["profile_revision"]
        or activation.get("plan_digest") != plan["plan_digest"]
        or activation.get("lock_digest") != lock["lock_digest"]
    ):
        raise ProfileResolutionDenied("runtime launch selector activation is stale")
    contribution = plan.get("launch_contribution")
    application = plan.get("application")
    shell = lock.get("shell")
    if (
        not isinstance(contribution, Mapping)
        or not isinstance(application, Mapping)
        or not isinstance(shell, Mapping)
        or application.get("executable_artifact_digest")
        != plan["shell"].get("executable_artifact_digest")
        or contribution.get("platform") != shell.get("platform")
        or contribution.get("architecture") != shell.get("architecture")
    ):
        raise ProfileResolutionDenied("runtime launch contribution is unavailable")
    return {
        "selector_api_version": "io.tobkiri.runtime-launch-selector.v1",
        "profile_id": plan["profile_id"],
        "profile_revision": plan["profile_revision"],
        "activation_id": activation["activation_id"],
        "plan_digest": plan["plan_digest"],
        "launch_contribution": dict(contribution),
    }


def _edge_key(edge: Mapping[str, Any]) -> str:
    return "|".join(
        str(edge.get(field) or "")
        for field in (
            "caller_function_id",
            "target_provider_id",
            "contract_id",
            "operation_id",
        )
    )


def _authority_mode(edge: Mapping[str, Any]) -> str:
    """Return the closed activation authority policy for one requested edge."""

    value = edge.get("authority_mode", "profile_grant")
    if value not in _AUTHORITY_MODES:
        raise ProfileResolutionDenied("requested edge authority mode is invalid")
    return str(value)


def _provider_candidates(
    packs: list[Mapping[str, Any]], contract_id: str, operation_id: str
) -> list[tuple[Mapping[str, Any], Mapping[str, Any], Mapping[str, Any]]]:
    candidates: list[tuple[Mapping[str, Any], Mapping[str, Any], Mapping[str, Any]]] = []
    for manifest in packs:
        contracts = [
            contract
            for contract in manifest["contracts"]
            if contract["contract_id"] == contract_id and operation_id in contract["operations"]
        ]
        if len(contracts) > 1:
            raise ProfileResolutionDenied(
                f"duplicate Contract declaration in {manifest['pack']['id']}: {contract_id}"
            )
        if not contracts:
            continue
        contract = contracts[0]
        for function in manifest["functions"]:
            if (
                operation_id in function["operations"]
                and function["contract_revision_digest"] == contract["revision_digest"]
            ):
                candidates.append((manifest, function, contract))
    return candidates


def dynamic_profile_edges(
    catalog: BundledCatalog,
    profile_id: str,
    additional_pack_ids: tuple[str, ...],
) -> tuple[dict[str, Any], ...]:
    """Derive exact operation edges for newly enabled Pack dependencies.

    The bundled Profile owns its static edges.  An approved optional Pack may
    contribute its own operations to the selected Shell and exact operations
    from a direct signed Pack dependency to the contributing Pack Function.
    Transitive dependencies remain selected and verified, but they do not gain
    new caller edges merely by appearing in the dependency closure.  Every edge
    is persisted into the immutable resolved Profile before dispatch.
    """

    source = catalog.profiles.get(profile_id)
    if source is None or not additional_pack_ids:
        return ()
    source_keys = {
        (str(edge["contract_id"]), str(edge["operation_id"])) for edge in source["requested_edges"]
    }
    shell_request = source.get("shell")
    if not isinstance(shell_request, Mapping):
        raise ProfileResolutionDenied("dynamic Pack edges require the selected Shell")
    shell_definition = catalog.shells.get(str(shell_request["provider_id"]))
    if shell_definition is None:
        raise ProfileResolutionDenied("dynamic Pack Shell Provider is unavailable")
    shell_manifest = catalog.packs.get(str(shell_definition["pack_id"]))
    if shell_manifest is None or not shell_manifest["functions"]:
        raise ProfileResolutionDenied("dynamic Pack Shell Function is unavailable")
    # The signed Profile's selected Shell provider is the only caller role
    # allowed to project optional Pack operations.  Never infer authority from
    # lexical Function ordering: a newly added Shell Function must not silently
    # inherit every dynamic Pack capability.
    selected_caller_id = str(shell_request.get("provider_id") or "")
    caller_functions = [
        function
        for function in shell_manifest["functions"]
        if str(function.get("id") or "") == selected_caller_id
        and str(function.get("role") or "") == "brokered"
    ]
    if len(caller_functions) != 1:
        raise ProfileResolutionDenied("dynamic Pack Shell caller role is absent or ambiguous")
    caller_function_id = selected_caller_id
    pending: list[tuple[str, str, int, tuple[str, ...] | None]] = [
        (str(pack_id), caller_function_id, 0, None) for pack_id in additional_pack_ids
    ]
    caller_for_pack: dict[str, str] = {}
    depth_for_pack: dict[str, int] = {}
    contracts_for_pack: dict[str, tuple[str, ...] | None] = {}
    closure: set[str] = set()
    while pending:
        pack_id, caller_id, depth, allowed_contracts = pending.pop(0)
        prior_caller = caller_for_pack.setdefault(pack_id, caller_id)
        prior_depth = depth_for_pack.setdefault(pack_id, depth)
        prior_contracts = contracts_for_pack.setdefault(pack_id, allowed_contracts)
        if (
            prior_caller != caller_id
            or prior_depth != depth
            or prior_contracts != allowed_contracts
        ):
            raise ProfileResolutionDenied(f"dynamic Pack dependency caller is ambiguous: {pack_id}")
        if pack_id in closure:
            continue
        manifest = catalog.packs.get(pack_id)
        if manifest is None:
            raise ProfileResolutionDenied(f"dynamic Pack is not in the exact inventory: {pack_id}")
        closure.add(pack_id)
        if depth >= 1:
            # Only a selected optional Pack and its direct signed dependency
            # receive dynamic caller edges.  The resolver validates the full
            # transitive implementation closure separately; inferring callers
            # beyond this point would both broaden authority and walk valid
            # Host-provider dependency cycles.
            continue
        dependencies = tuple(
            str(dependency) for dependency in manifest["requirements"]["pack_dependencies"]
        )
        if dependencies:
            functions = tuple(manifest["functions"])
            dependency_caller = str(functions[0]["id"]) if len(functions) == 1 else ""
            required_contracts = {
                str(item["contract_id"])
                for item in manifest["requirements"]["contract_dependencies"]
                if not item["optional"]
            }
            for dependency in dependencies:
                dependency_manifest = catalog.packs.get(dependency)
                if dependency_manifest is None:
                    raise ProfileResolutionDenied(
                        f"dynamic Pack is not in the exact inventory: {dependency}"
                    )
                provided_contracts = {
                    str(item["contract_id"]) for item in dependency_manifest["contracts"]
                }
                dependency_contracts = tuple(sorted(required_contracts & provided_contracts))
                if depth == 0 and not dependency_contracts:
                    raise ProfileResolutionDenied(
                        "dynamic Pack dependency does not provide a signed required "
                        f"Contract: {pack_id} -> {dependency}"
                    )
                pending.append(
                    (
                        dependency,
                        dependency_caller,
                        depth + 1,
                        dependency_contracts,
                    )
                )
    result: list[dict[str, Any]] = []
    for pack_id in sorted(closure):
        # Only the optional Pack and its direct signed dependencies contribute
        # dynamic Authority edges.  Deeper dependencies are implementation
        # closure and cannot inherit the ancestor caller.
        if depth_for_pack[pack_id] > 1:
            continue
        manifest = catalog.packs[pack_id]
        contracts = {str(contract["contract_id"]): contract for contract in manifest["contracts"]}
        for function in sorted(manifest["functions"], key=lambda item: str(item["id"])):
            for operation_id in sorted(str(item) for item in function["operations"]):
                contract_id = next(
                    (
                        candidate_id
                        for candidate_id, contract in contracts.items()
                        if operation_id in contract["operations"]
                    ),
                    None,
                )
                if contract_id is None or (contract_id, operation_id) in source_keys:
                    continue
                allowed_contracts = contracts_for_pack[pack_id]
                if allowed_contracts is not None and contract_id not in allowed_contracts:
                    continue
                operation_caller = caller_for_pack[pack_id]
                if not operation_caller:
                    raise ProfileResolutionDenied(
                        f"dynamic Pack dependency caller is ambiguous: {pack_id}"
                    )
                # Dynamic Pack edges are never user-selected interactive
                # authority.  Their absent mode deliberately means the closed
                # ``profile_grant`` default, preserving older resolved Profile
                # bytes while preventing a dynamic source from opting in.
                result.append(
                    {
                        "caller_function_id": operation_caller,
                        "target_provider_id": str(function["id"]),
                        "contract_id": contract_id,
                        "operation_id": operation_id,
                        "requested_scope_template": {
                            "capability": "operation.invoke",
                            "dimensions": {
                                "contract": [contract_id],
                                "operation": [operation_id],
                            },
                        },
                    }
                )
    return tuple(result)


def _exact_executable_variant(
    catalog: BundledCatalog,
    manifest: Mapping[str, Any],
    function: Mapping[str, Any],
    contract_id: str,
    operation_id: str,
) -> Mapping[str, Any]:
    """Resolve one Function through its bundled executable catalog only."""
    pack_id = str(manifest["pack"]["id"])
    executable = catalog.executable_catalogs.get(pack_id)
    if executable is None:
        raise ProfileResolutionDenied(f"executable catalog is not bundled for Pack: {pack_id}")
    if (
        executable["pack_id"] != pack_id
        or executable["source_identity"] != manifest["integrity"]["source_identity"]
    ):
        raise ProfileResolutionDenied("executable catalog identity is stale")
    expected_catalog_digest = canonical_digest(
        {key: value for key, value in executable.items() if key != "catalog_digest"}
    )
    if executable["catalog_digest"] != expected_catalog_digest:
        raise ProfileResolutionDenied("executable catalog digest is stale")
    variants = [item for item in executable["variants"] if item["function_id"] == function["id"]]
    if len(variants) != 1:
        raise ProfileResolutionDenied(
            f"executable variant is not unique: {pack_id}/{function['id']}"
        )
    variant = variants[0]
    if variant["implementation_digest"] != function["implementation_digest"]:
        raise ProfileResolutionDenied("executable variant implementation is stale")
    operations = [
        item
        for item in variant["operations"]
        if item["contract_id"] == contract_id
        and item["operation_id"] == operation_id
        and item["revision_digest"] == function["contract_revision_digest"]
    ]
    if len(operations) != 1:
        raise ProfileResolutionDenied("executable variant Operation is not exact")
    expected_execution_kind = (
        "host_extension"
        if manifest["pack"]["kind"] == "host_extension"
        else (
            "wasm"
            if function.get("isolation") == "wasm_component"
            else "remote"
            if function.get("isolation") == "remote"
            else "pack_vm"
        )
    )
    if variant["execution_kind"] != expected_execution_kind:
        raise ProfileResolutionDenied("executable variant execution kind is stale")
    return variant


def resolve_default_profile(
    catalog: BundledCatalog,
    profile_id: str,
    *,
    approved_artifact_digests: set[str] | frozenset[str],
    authority_snapshot_digest: str,
    authority_bindings: Mapping[str, str],
    security_epoch: int,
    additional_pack_ids: tuple[str, ...] = (),
) -> ResolvedDefaultProfile:
    """Resolve one bundled default Profile without live discovery or fallback.

    ``approved_artifact_digests`` and ``authority_bindings`` are captured Host
    inputs. This function never interprets client approval flags and never mints
    authority.
    """
    snapshot_digest = _require_digest(authority_snapshot_digest, "authority_snapshot_digest")
    if (
        not isinstance(security_epoch, int)
        or isinstance(security_epoch, bool)
        or security_epoch < 0
    ):
        raise ProfileResolutionDenied("security_epoch must be a non-negative integer")
    source = catalog.profiles.get(profile_id)
    if source is None:
        raise ProfileResolutionDenied(f"profile is not in the bundled inventory: {profile_id}")
    if source["state"] != "needs_resolution":
        raise ProfileResolutionDenied("bundled Profile must begin in needs_resolution state")
    if (
        source["profile_authority_snapshot_digest"] is not None
        or source["authority_references"]
        or any(edge.get("authority_reference") is not None for edge in source["requested_edges"])
    ):
        raise ProfileResolutionDenied(
            "bundled Profile source must not contain resolved Authority state"
        )
    profile_definition_digest = canonical_digest(source)
    bundle_digest = _bundle_digest(catalog)

    base_id = source["base"]["pack_id"]
    base_manifest = catalog.packs.get(base_id)
    base_definition = catalog.bases.get(base_id)
    if base_manifest is None or base_definition is None:
        raise ProfileResolutionDenied(f"Base is incomplete: {base_id}")
    if base_manifest["pack"]["kind"] != "base":
        raise ProfileResolutionDenied(f"selected Base manifest is not kind=base: {base_id}")
    if base_definition["artifact_digest"] != base_manifest["pack"]["artifact_digest"]:
        raise ProfileResolutionDenied("Base definition does not pin its exact artifact")
    _require_optional_pin(
        source["base"]["artifact_digest"],
        str(base_manifest["pack"]["artifact_digest"]),
        "Profile Base artifact pin",
    )
    _require_optional_pin(
        source["base"]["definition_revision"],
        str(base_definition["definition_revision"]),
        "Profile Base definition pin",
    )
    if source["base"].get("resolution") not in {
        None,
        "verified_exact_artifact_required",
    }:
        raise ProfileResolutionDenied("bundled Profile Base resolution state is invalid")

    base_dependency_ids: list[str] = []
    for dependency in base_definition["dependencies"]:
        dependency_id = str(dependency["pack_id"])
        if dependency_id in base_dependency_ids:
            raise ProfileResolutionDenied(
                f"Base definition contains a duplicate dependency: {dependency_id}"
            )
        dependency_manifest = catalog.packs.get(dependency_id)
        if dependency_manifest is None:
            raise ProfileResolutionDenied(f"Base dependency is unavailable: {dependency_id}")
        if dependency["artifact_digest"] != dependency_manifest["pack"]["artifact_digest"]:
            raise ProfileResolutionDenied(
                f"Base dependency artifact is stale or mismatched: {dependency_id}"
            )
        base_dependency_ids.append(dependency_id)

    shell_request = source.get("shell")
    if not isinstance(shell_request, dict):
        raise ProfileResolutionDenied("default interactive Profile requires one Shell")
    provider_id = shell_request["provider_id"]
    shell_definition = catalog.shells.get(provider_id)
    if shell_definition is None:
        raise ProfileResolutionDenied(f"Shell Provider is not inventoried: {provider_id}")
    if shell_definition.get("availability") != "verified":
        raise ProfileResolutionDenied(
            f"Shell artifact is unavailable for this source/build: {provider_id}"
        )
    if shell_request["pack_id"] != shell_definition["pack_id"]:
        raise ProfileResolutionDenied("Profile Shell Pack binding is stale or mismatched")
    if shell_request["contract_id"] != shell_definition["contract_id"]:
        raise ProfileResolutionDenied("Profile Shell Contract binding is stale or mismatched")
    variants = [
        variant
        for variant in shell_definition["launch"]["variants"]
        if variant["platform"] == shell_request["platform"]
        and variant["architecture"] == shell_request["architecture"]
    ]
    if len(variants) != 1 or catalog.artifact_root is None:
        raise ProfileResolutionDenied("verified Shell platform artifact is unavailable")
    selected_variant = variants[0]
    try:
        verify_platform_artifact(catalog.artifact_root, selected_variant)
    except ProtocolError as exc:
        raise ProfileResolutionDenied(f"verified Shell artifact rejected: {exc}") from exc
    shell_pack_id = shell_definition["pack_id"]
    shell_manifest = catalog.packs.get(shell_pack_id)
    if shell_manifest is None or shell_manifest["pack"]["kind"] != "shell":
        raise ProfileResolutionDenied(f"Shell Pack is missing or invalid: {shell_pack_id}")
    shell_contracts = [
        contract
        for contract in shell_manifest["contracts"]
        if contract["contract_id"] == shell_definition["contract_id"]
    ]
    shell_functions = [
        function
        for function in shell_manifest["functions"]
        if function["id"] == provider_id
        and any(
            function["contract_revision_digest"] == contract["revision_digest"]
            and set(function["operations"]).issubset(set(contract["operations"]))
            for contract in shell_contracts
        )
    ]
    if len(shell_contracts) != 1 or len(shell_functions) != 1:
        raise ProfileResolutionDenied("Shell Provider does not implement the exact Shell Contract")
    if shell_definition["artifact_digest"] != selected_variant["artifact_digest"]:
        raise ProfileResolutionDenied("Shell definition does not pin its exact artifact")
    _require_optional_pin(
        shell_request["artifact_digest"],
        str(selected_variant["artifact_digest"]),
        "Profile Shell artifact pin",
    )
    _require_optional_pin(
        shell_request.get("executable_artifact_digest"),
        str(selected_variant["entrypoint_digest"]),
        "Profile Shell executable pin",
    )
    _require_optional_pin(
        shell_request["definition_revision"],
        str(shell_definition["definition_revision"]),
        "Profile Shell definition pin",
    )
    shell_requirements = base_definition["shell_requirements"]
    if source["mode"] != shell_requirements["mode"]:
        raise ProfileResolutionDenied("Profile mode does not match the Base definition")
    if (
        shell_definition["presentation"]["family"]
        not in shell_requirements["presentation_families"]
    ):
        raise ProfileResolutionDenied("Shell presentation family is incompatible with Base")
    if not set(shell_requirements["required_capabilities"]).issubset(
        set(shell_definition["presentation"]["capabilities"])
    ):
        raise ProfileResolutionDenied("Shell does not satisfy Base capabilities")

    requested_pack_ids = [item["pack_id"] for item in source["packs"]]
    if len(requested_pack_ids) != len(set(requested_pack_ids)):
        raise ProfileResolutionDenied("Profile composition contains a duplicate Pack")
    requested_pack_roles = {
        item["pack_id"]: item.get("role", "provider") for item in source["packs"]
    }
    application_ids = sorted(
        pack_id for pack_id, role in requested_pack_roles.items() if role == "application"
    )
    if len(application_ids) != 1:
        raise ProfileResolutionDenied(
            "canonical defaults Profile requires exactly one Application Pack"
        )
    application_manifest = catalog.packs.get(application_ids[0])
    if application_manifest is None or application_manifest["pack"]["kind"] != "application":
        raise ProfileResolutionDenied(
            "canonical defaults Application must be a Pack v4 application"
        )
    for request in source["packs"]:
        manifest = catalog.packs.get(str(request["pack_id"]))
        if manifest is None:
            raise ProfileResolutionDenied(
                f"Pack is not in the exact inventory: {request['pack_id']}"
            )
        _require_optional_pin(
            request["artifact_digest"],
            str(manifest["pack"]["artifact_digest"]),
            f"Profile Pack artifact pin for {request['pack_id']}",
        )
    application_artifacts = application_manifest["artifacts"]
    expected_application = (
        selected_variant["relative_path"],
        selected_variant["artifact_digest"],
        selected_variant["entrypoint_digest"],
        f"{selected_variant['platform']}-{selected_variant['architecture']}",
        selected_variant["entrypoint"],
    )
    actual_applications = [
        (
            item.get("path"),
            item.get("digest"),
            item.get("entrypoint_digest"),
            item.get("platform"),
            item.get("entrypoint"),
        )
        for item in application_artifacts
        if item.get("kind") == "executable"
    ]
    if actual_applications != [expected_application] or any(
        function["implementation_digest"] != selected_variant["entrypoint_digest"]
        for function in application_manifest["functions"]
    ):
        raise ProfileResolutionDenied(
            "Application Pack does not pin the selected packaged artifact"
        )
    if len(additional_pack_ids) != len(set(additional_pack_ids)):
        raise ProfileResolutionDenied("additional Pack selection contains a duplicate")
    canonical_additional_ids = tuple(sorted(additional_pack_ids))
    selected_ids = [base_id, shell_pack_id, *requested_pack_ids]
    selected_ids.extend(
        dependency_id
        for dependency_id in sorted(base_dependency_ids)
        if dependency_id not in selected_ids
    )
    selected_ids.extend(
        pack_id for pack_id in canonical_additional_ids if pack_id not in selected_ids
    )
    pending = list(selected_ids)
    while pending:
        current_id = pending.pop(0)
        current = catalog.packs.get(current_id)
        if current is None:
            raise ProfileResolutionDenied(f"Pack is not in the exact inventory: {current_id}")
        for dependency_id, version_range in sorted(
            current["requirements"]["pack_dependencies"].items()
        ):
            dependency = catalog.packs.get(dependency_id)
            if dependency is None:
                raise ProfileResolutionDenied(
                    f"required Pack dependency is unavailable: {dependency_id}"
                )
            try:
                compatible = Version(dependency["pack"]["version"]) in SpecifierSet(
                    version_range.replace(" ", ",")
                )
            except (InvalidSpecifier, InvalidVersion) as exc:
                raise ProfileResolutionDenied(
                    f"invalid Pack dependency constraint: {dependency_id}"
                ) from exc
            if not compatible:
                raise ProfileResolutionDenied(
                    f"Pack dependency version is incompatible: {dependency_id}"
                )
            if dependency_id not in selected_ids:
                selected_ids.append(dependency_id)
                pending.append(dependency_id)
    if len(selected_ids) != len(set(selected_ids)):
        raise ProfileResolutionDenied("Profile composition contains a duplicate Pack")
    selected: list[Mapping[str, Any]] = []
    for pack_id in selected_ids:
        manifest = catalog.packs.get(pack_id)
        if manifest is None:
            raise ProfileResolutionDenied(f"Pack is not in the exact inventory: {pack_id}")
        artifact_digest = manifest["pack"]["artifact_digest"]
        if artifact_digest not in approved_artifact_digests:
            raise ProfileResolutionDenied(f"Pack artifact is not approved: {pack_id}")
        selected.append(manifest)

    provided_contracts = {
        contract["contract_id"] for manifest in selected for contract in manifest["contracts"]
    }
    for manifest in selected:
        for dependency in manifest["requirements"]["contract_dependencies"]:
            if not dependency["optional"] and dependency["contract_id"] not in provided_contracts:
                raise ProfileResolutionDenied(
                    f"required Contract dependency is unavailable: {dependency['contract_id']}"
                )

    foundational = _provider_candidates(selected, _FOUNDATIONAL_CONTRACT, "complete")
    if len(foundational) != 1:
        raise ProfileResolutionDenied(
            "foundational conversation Provider must resolve exactly once; "
            f"found {len(foundational)}"
        )

    available_function_ids = {
        function["id"] for manifest in selected for function in manifest["functions"]
    }
    available_function_ids.update(function["id"] for function in shell_manifest["functions"])
    dynamic_edges = dynamic_profile_edges(catalog, profile_id, canonical_additional_ids)
    all_source_edges = (*source["requested_edges"], *dynamic_edges)
    edge_keys = [_edge_key(edge) for edge in all_source_edges]
    if len(edge_keys) != len(set(edge_keys)):
        raise ProfileResolutionDenied("Profile contains a duplicate requested edge")
    for edge in all_source_edges:
        caller_function_id = edge["caller_function_id"]
        if caller_function_id not in available_function_ids:
            raise ProfileResolutionDenied(
                "requested edge caller is not in the selected Profile closure: "
                f"{caller_function_id}"
            )

    bindings: list[dict[str, Any]] = []
    variant_pins: dict[tuple[str, str], dict[str, Any]] = {}
    resolved_edges: list[dict[str, Any]] = []
    references: list[str] = []
    for source_edge in all_source_edges:
        edge = dict(source_edge)
        authority_mode = _authority_mode(edge)
        candidates = _provider_candidates(selected, edge["contract_id"], edge["operation_id"])
        candidates = [item for item in candidates if item[1]["id"] == edge["target_provider_id"]]
        if len(candidates) != 1:
            raise ProfileResolutionDenied(
                f"requested edge {_edge_key(edge)} must resolve exactly once; "
                f"found {len(candidates)}"
            )
        reference = authority_bindings.get(_edge_key(edge))
        if not isinstance(reference, str) or not reference.startswith("authority-ref:"):
            raise ProfileResolutionDenied(
                f"Authority Kernel reference is missing for edge {_edge_key(edge)}"
            )
        edge["authority_reference"] = reference
        if reference not in references:
            references.append(reference)
        manifest, function, contract = candidates[0]
        try:
            edge["requested_scope_template"] = normalize_requested_scope_template(
                edge["requested_scope_template"],
                contract_id=str(edge["contract_id"]),
                operation_id=str(edge["operation_id"]),
                semantics_digest=str(contract["revision_digest"]),
            )
        except ProtocolError as exc:
            raise ProfileResolutionDenied(
                f"requested scope is invalid for edge {_edge_key(edge)}: {exc}"
            ) from exc
        resolved_edges.append(edge)
        principal = {
            "parent_artifact_digest": manifest["pack"]["artifact_digest"],
            "function_implementation_digest": function["implementation_digest"],
            "function_id": function["id"],
            "contract_revision_digest": contract["revision_digest"],
            "operation_id": edge["operation_id"],
        }
        variant = _exact_executable_variant(
            catalog,
            manifest,
            function,
            str(edge["contract_id"]),
            str(edge["operation_id"]),
        )
        domain_kind = function.get("isolation", "pack_vm")
        try:
            executable_catalog_digest = materialization_catalog_digest(
                manifest,
                catalog.executable_catalogs[manifest["pack"]["id"]],
            )
        except ValueError as exc:
            raise ProfileResolutionDenied(str(exc)) from exc
        pin = {
            "pack_id": manifest["pack"]["id"],
            "artifact_digest": manifest["pack"]["artifact_digest"],
            "executable_catalog_digest": executable_catalog_digest,
            "variant_id": variant["variant_id"],
            "platform": variant["platform"],
            "architecture": variant["architecture"],
            "runtime_abi": variant["runtime_abi"],
            "backend": variant["backend"],
            "execution_kind": variant["execution_kind"],
            "domain_kind": domain_kind,
        }
        pin_key = (str(pin["pack_id"]), str(pin["variant_id"]))
        previous_pin = variant_pins.setdefault(pin_key, pin)
        if previous_pin != pin:
            raise ProfileResolutionDenied("one executable variant has conflicting pins")
        binding = {
            "caller_function_id": edge["caller_function_id"],
            "pack_id": manifest["pack"]["id"],
            "artifact_digest": manifest["pack"]["artifact_digest"],
            "function_principal": principal,
            "contract_id": edge["contract_id"],
            "operation_id": edge["operation_id"],
            "domain_kind": domain_kind,
            "executable_catalog_digest": pin["executable_catalog_digest"],
            "variant_id": pin["variant_id"],
            "platform": pin["platform"],
            "architecture": pin["architecture"],
            "runtime_abi": pin["runtime_abi"],
            "backend": pin["backend"],
            "execution_kind": pin["execution_kind"],
            "authority_reference": reference,
            "requested_scope_digest": canonical_digest(edge["requested_scope_template"]),
            "adapter_digests": [],
        }
        if authority_mode == "interactive_only":
            binding["authority_mode"] = authority_mode
        bindings.append(binding)

    profile = dict(source)
    profile["state"] = "resolved"
    profile["base"] = {
        "pack_id": base_id,
        "artifact_digest": base_manifest["pack"]["artifact_digest"],
        "definition_revision": base_definition["definition_revision"],
        "resolution": "verified",
    }
    resolved_shell = {
        "provider_id": provider_id,
        "pack_id": shell_pack_id,
        "artifact_digest": shell_manifest["pack"]["artifact_digest"],
        "definition_revision": shell_definition["definition_revision"],
        "contract_id": "app.shell.v1",
        "platform": shell_request["platform"],
        "architecture": shell_request["architecture"],
    }
    if source.get("profile_api_version") == "io.tobkiri.profile.v5":
        resolved_shell["executable_artifact_digest"] = selected_variant["entrypoint_digest"]
    profile["shell"] = resolved_shell
    profile["packs"] = [
        {
            "pack_id": manifest["pack"]["id"],
            "artifact_digest": manifest["pack"]["artifact_digest"],
            "role": requested_pack_roles.get(manifest["pack"]["id"], "provider"),
        }
        for manifest in selected
        if manifest["pack"]["id"] not in {base_id, shell_pack_id}
    ]
    profile["requested_edges"] = resolved_edges
    from core_runtime.profile_content_projection import (
        resolve_profile_projection,
        selected_projection_roots,
    )

    profile["content_projections"] = sorted(
        [resolve_profile_projection(item) for item in source.get("content_projections") or []],
        key=lambda item: item["projection_id"],
    )
    selected_projection_roots(profile["content_projections"])
    profile["authority_references"] = references
    profile["profile_authority_snapshot_digest"] = snapshot_digest
    catalog_revision = canonical_digest(
        {manifest["pack"]["id"]: manifest["integrity"]["source_identity"] for manifest in selected}
    )
    _require_optional_pin(
        source["catalog_revision"],
        catalog_revision,
        "Profile catalog revision pin",
    )
    profile["catalog_revision"] = catalog_revision
    profile = validate_document(profile, "profile")
    profile_revision = canonical_digest(profile)
    effective_set = [
        {
            "role": (
                "base"
                if manifest["pack"]["id"] == base_id
                else "shell"
                if manifest["pack"]["id"] == shell_pack_id
                else "pack"
            ),
            "identity": manifest["pack"]["id"],
            "artifact_digest": manifest["pack"]["artifact_digest"],
        }
        for manifest in selected
    ]
    requested_edges_digest = canonical_digest(resolved_edges)
    constraints_digest = canonical_digest(
        {
            "base": {
                "definition_revision": base_definition["definition_revision"],
                "shell_requirements": base_definition["shell_requirements"],
                "dependencies": base_definition["dependencies"],
            },
            "shell": {
                "definition_revision": shell_definition["definition_revision"],
                "presentation": shell_definition["presentation"],
                "launch": shell_definition["launch"],
            },
            "packs": {
                manifest["pack"]["id"]: manifest["requirements"]
                for manifest in sorted(selected, key=lambda item: item["pack"]["id"])
            },
            "requested_scope_templates": [
                edge["requested_scope_template"] for edge in resolved_edges
            ],
        }
    )
    closure_digest = canonical_digest(
        {
            "effective_set": effective_set,
            "content_projections": profile["content_projections"],
        }
    )
    provenance_digest = canonical_digest(profile["provenance"])
    application = {
        "pack_id": application_ids[0],
        "artifact_digest": application_manifest["pack"]["artifact_digest"],
        "executable_artifact_digest": selected_variant["entrypoint_digest"],
        "definition_digest": canonical_digest(application_manifest),
    }
    launch_provider_id, launch_contract_id, launch_operation_id = _application_launch_identity(
        application_manifest
    )
    launch_contribution = {
        "provider_id": launch_provider_id,
        "contract_id": launch_contract_id,
        "operation_id": launch_operation_id,
        "platform": selected_variant["platform"],
        "architecture": selected_variant["architecture"],
        "artifact_digest": selected_variant["artifact_digest"],
        "relative_path": selected_variant["relative_path"],
        "entrypoint": selected_variant["entrypoint"],
    }

    plan: dict[str, Any] = {
        "plan_api_version": "io.tobkiri.resolved-plan.v2",
        "profile_id": profile_id,
        "profile_revision": profile_revision,
        "profile_definition_digest": profile_definition_digest,
        "catalog_revision": profile["catalog_revision"],
        "bundle_digest": bundle_digest,
        "profile_authority_snapshot_digest": snapshot_digest,
        "security_epoch": security_epoch,
        "base": {
            "pack_id": base_id,
            "artifact_digest": base_manifest["pack"]["artifact_digest"],
            "definition_digest": canonical_digest(base_definition),
        },
        "shell": {
            "provider_id": provider_id,
            "pack_id": shell_pack_id,
            "artifact_digest": shell_manifest["pack"]["artifact_digest"],
            "executable_artifact_digest": selected_variant["entrypoint_digest"],
            "contract_id": "app.shell.v1",
            "definition_digest": canonical_digest(shell_definition),
        },
        "application": application,
        "launch_contribution": launch_contribution,
        "effective_set": effective_set,
        "content_projections": profile["content_projections"],
        "requested_edges_digest": requested_edges_digest,
        "constraints_digest": constraints_digest,
        "closure_digest": closure_digest,
        "provenance_digest": provenance_digest,
        "bindings": bindings,
        "plan_digest": "sha256:" + "0" * 64,
    }
    plan["plan_digest"] = canonical_digest(
        {key: value for key, value in plan.items() if key != "plan_digest"}
    )
    plan = validate_document(plan, "resolved_plan")

    lock: dict[str, Any] = {
        "lock_api_version": "io.tobkiri.profile-lock.v5",
        "profile_id": profile_id,
        "profile_revision": profile_revision,
        "profile_definition_digest": profile_definition_digest,
        "catalog_revision": profile["catalog_revision"],
        "bundle_digest": bundle_digest,
        "security_epoch": security_epoch,
        "base": {
            "pack_id": base_id,
            "artifact_digest": base_manifest["pack"]["artifact_digest"],
            "definition_revision": base_definition["definition_revision"],
        },
        "shell": {
            **dict(profile["shell"]),
            "executable_artifact_digest": selected_variant["entrypoint_digest"],
        },
        "application": application,
        "effective_set": effective_set,
        "content_projections": profile["content_projections"],
        "variant_pins": sorted(
            variant_pins.values(),
            key=lambda item: (item["pack_id"], item["variant_id"]),
        ),
        "requested_edges_digest": requested_edges_digest,
        "constraints_digest": constraints_digest,
        "closure_digest": closure_digest,
        "provenance_digest": provenance_digest,
        "plan_digest": plan["plan_digest"],
        "profile_authority_snapshot_digest": snapshot_digest,
        "lock_digest": "sha256:" + "0" * 64,
    }
    lock["lock_digest"] = canonical_digest(
        {key: value for key, value in lock.items() if key != "lock_digest"}
    )
    lock = validate_document(lock, "profile_lock")
    return ResolvedDefaultProfile(profile=profile, lock=lock, plan=plan)


class ActivationStore:
    """Atomic persistence for one workspace-bound default Profile activation."""

    def __init__(
        self,
        state_root: Path,
        workspace_root: Path,
        *,
        profile_id: str,
        authority: ActivationAuthority,
        catalog: BundledCatalog | None = None,
        fault: Callable[[str], None] | None = None,
        lock_timeout_seconds: float = 5.0,
        monotonic_clock: Callable[[], float] = time.monotonic,
        retry_sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        requested_state_root = Path(state_root)
        requested_workspace_root = Path(workspace_root)
        if requested_state_root.is_symlink():
            raise ProfileResolutionDenied("state_root must not be a symlink")
        if requested_workspace_root.is_symlink():
            raise ProfileResolutionDenied("workspace_root must not be a symlink")
        if lock_timeout_seconds <= 0 or lock_timeout_seconds > 30.0:
            raise ValueError("lock_timeout_seconds must be positive and bounded")
        self.state_root = requested_state_root.absolute()
        self.workspace_root = requested_workspace_root.resolve(strict=True)
        self.profile_id = profile_id
        self._authority = authority
        self._catalog = catalog
        self._fault = fault or (lambda _stage: None)
        self._lock_timeout_seconds = lock_timeout_seconds
        self._monotonic_clock = monotonic_clock
        self._retry_sleep = retry_sleep
        self._lock_platform = os.name
        if not self.workspace_root.is_dir():
            raise ProfileResolutionDenied("workspace_root must be a directory")
        try:
            self._state = SecureDirectory(self.state_root, create=True)
        except SecurePersistenceError as exc:
            raise ProfileResolutionDenied("activation state root is unsafe") from exc
        self._workspace_digest = canonical_digest({"workspace_root": str(self.workspace_root)})

    def _write_state(self, relative: str | Path, payload: Mapping[str, Any]) -> None:
        """Write canonical activation state below the pinned state root."""

        try:
            self._state.write_bytes_atomic(
                relative,
                canonical_json(dict(payload)) + b"\n",
            )
        except (OSError, SecurePersistenceError) as exc:
            raise ProfileResolutionDenied("activation persistence is unavailable") from exc

    def _read_state(self, relative: str | Path, label: str) -> Any:
        """Read and decode one pinned activation record."""

        try:
            return strict_loads(self._state.read_bytes(relative))
        except (OSError, ProtocolError, SecurePersistenceError) as exc:
            raise ProfileResolutionDenied(f"{label} is unavailable: {exc}") from exc

    def _unlink_state(self, relative: str | Path, *, missing_ok: bool = False) -> None:
        """Remove one pinned activation record."""

        try:
            self._state.unlink(relative, missing_ok=missing_ok)
        except (OSError, SecurePersistenceError) as exc:
            raise ProfileResolutionDenied("activation persistence is unavailable") from exc

    def resolve_workspace_path(self, relative_path: str) -> Path:
        """Resolve a relative resource and reject traversal or workspace escape."""
        candidate_input = Path(relative_path)
        if not relative_path or candidate_input.is_absolute() or ".." in candidate_input.parts:
            raise ProfileResolutionDenied("workspace path must be relative and traversal-free")
        candidate = (self.workspace_root / candidate_input).resolve()
        if candidate == self.workspace_root or self.workspace_root not in candidate.parents:
            raise ProfileResolutionDenied("workspace path escapes the bound workspace")
        return candidate

    def activate(
        self,
        resolved: ResolvedDefaultProfile,
        *,
        activation_id: str,
        created_at: str,
        expected_predecessor_profile_revision: str | None = None,
        expected_predecessor_plan_digest: str | None = None,
        expected_predecessor_activation_id: str | None = None,
    ) -> Mapping[str, Any]:
        """Commit the complete fenced activation state machine and pointer swap."""
        if _ACTIVATION_RE.fullmatch(activation_id) is None:
            raise ProfileResolutionDenied("activation_id is not canonical")
        profile = validate_document(resolved.profile, "profile")
        lock = validate_document(resolved.lock, "profile_lock")
        plan = validate_document(resolved.plan, "resolved_plan")
        self._validate_record_graph(profile, lock, plan)
        if profile["profile_id"] != self.profile_id:
            raise ProfileResolutionDenied("activation Profile identity does not match store")
        with self._activation_lock():
            return self._activate_locked(
                resolved=ResolvedDefaultProfile(profile=profile, lock=lock, plan=plan),
                activation_id=activation_id,
                created_at=created_at,
                expected_predecessor_profile_revision=(expected_predecessor_profile_revision),
                expected_predecessor_plan_digest=expected_predecessor_plan_digest,
                expected_predecessor_activation_id=expected_predecessor_activation_id,
            )

    def _activate_locked(
        self,
        resolved: ResolvedDefaultProfile,
        *,
        activation_id: str,
        created_at: str,
        expected_predecessor_profile_revision: str | None,
        expected_predecessor_plan_digest: str | None,
        expected_predecessor_activation_id: str | None,
    ) -> Mapping[str, Any]:
        """Run one activation while holding the profile's process lock."""
        profile = resolved.profile
        lock = resolved.lock
        plan = resolved.plan
        self._recover_locked()
        expected_predecessor = (
            expected_predecessor_profile_revision,
            expected_predecessor_plan_digest,
            expected_predecessor_activation_id,
        )
        active: ActiveDefaultProfile | None = None
        if any(value is not None for value in expected_predecessor) and self._state.exists(
            "active.json"
        ):
            active = self._load_active_snapshot_locked()
            if active.activation["activation_id"] == activation_id:
                if active.resolved != resolved:
                    raise ProfileResolutionDenied(
                        "activation identity is bound to another resolved Profile"
                    )
                return dict(active.activation)
        if any(value is not None for value in expected_predecessor):
            if not all(isinstance(value, str) and value for value in expected_predecessor):
                raise ProfileResolutionDenied("activation predecessor binding is incomplete")
            if active is None:
                raise ProfileResolutionDenied("activation predecessor is unavailable")
            actual_predecessor = (
                str(active.resolved.plan["profile_revision"]),
                str(active.resolved.plan["plan_digest"]),
                str(active.activation["activation_id"]),
            )
            if actual_predecessor != expected_predecessor:
                raise ProfileResolutionDenied("activation predecessor is stale")
        self._verify_selected_artifact(profile)
        reservation_id, fencing_token = self._authority.reserve_activation(
            activation_id=activation_id,
            profile_id=self.profile_id,
            plan_digest=plan["plan_digest"],
            profile_authority_digest=profile["profile_authority_snapshot_digest"],
            security_epoch=plan["security_epoch"],
        )
        envelope_path = self.state_root / "activations" / f"{activation_id[11:]}.json"
        state = "prepared"
        try:
            activation = self._activation_record(
                profile,
                lock,
                plan,
                activation_id=activation_id,
                state=state,
                generation=1,
                fencing_token=fencing_token,
                created_at=created_at,
            )
            envelope = self._envelope(profile, lock, plan, activation)
            self._write_state(Path("activations") / envelope_path.name, envelope)
            self._write_state(
                "pending.json",
                self._pending_record(
                    reservation_id,
                    activation_id,
                    envelope_path,
                    canonical_digest(envelope),
                    fencing_token,
                ),
            )
            self._fault("prepared")

            self._authority.transition_activation(
                reservation_id,
                expected_state=state,
                new_state="ready_without_authority",
            )
            state = "ready_without_authority"
            activation = self._activation_record(
                profile,
                lock,
                plan,
                activation_id=activation_id,
                state=state,
                generation=2,
                fencing_token=fencing_token,
                created_at=created_at,
            )
            envelope = self._envelope(profile, lock, plan, activation)
            self._write_state(Path("activations") / envelope_path.name, envelope)
            self._write_state(
                "pending.json",
                self._pending_record(
                    reservation_id,
                    activation_id,
                    envelope_path,
                    canonical_digest(envelope),
                    fencing_token,
                ),
            )
            self._fault("ready_without_authority")

            self._authority.transition_activation(
                reservation_id,
                expected_state=state,
                new_state="committing",
            )
            state = "committing"
            activation = self._activation_record(
                profile,
                lock,
                plan,
                activation_id=activation_id,
                state=state,
                generation=3,
                fencing_token=fencing_token,
                created_at=created_at,
            )
            self._write_state(
                Path("activations") / envelope_path.name,
                self._envelope(profile, lock, plan, activation),
            )
            self._fault("committing")

            activation = self._activation_record(
                profile,
                lock,
                plan,
                activation_id=activation_id,
                state="active",
                generation=4,
                fencing_token=fencing_token,
                created_at=created_at,
                committed_at=created_at,
            )
            envelope = self._envelope(profile, lock, plan, activation)
            envelope_digest = canonical_digest(envelope)
            self._write_state(Path("activations") / envelope_path.name, envelope)
            self._write_state(
                "pending.json",
                self._pending_record(
                    reservation_id,
                    activation_id,
                    envelope_path,
                    envelope_digest,
                    fencing_token,
                ),
            )
            self._fault("before_authority_commit")
            self._verify_selected_artifact(profile)
            self._authority.transition_activation(
                reservation_id,
                expected_state=state,
                new_state="active",
            )
            state = "active"
            self._fault("after_authority_commit")
            self._revalidate_publish_reservation(
                reservation_id=reservation_id,
                activation_id=activation_id,
                plan=plan,
                profile=profile,
                fencing_token=fencing_token,
            )
            self._verify_selected_artifact(profile)
            self._write_state(
                "active.json",
                self._active_pointer(activation_id, envelope_path, envelope_digest),
            )
            self._unlink_state("pending.json", missing_ok=True)
            return activation
        except Exception:
            if state != "active":
                try:
                    reservation = self._authority.activation_reservation(reservation_id)
                    if reservation is not None and reservation.get("state") == state:
                        self._authority.transition_activation(
                            reservation_id,
                            expected_state=state,
                            new_state="aborted",
                        )
                finally:
                    self._unlink_state("pending.json", missing_ok=True)
                    self._unlink_state(
                        Path("activations") / envelope_path.name,
                        missing_ok=True,
                    )
            raise

    def recover(self) -> None:
        """Recover a crash to the complete old or complete committed activation."""

        with self._activation_lock():
            self._recover_locked()

    def _recover_locked(self) -> None:
        """Recover state while the profile's process lock is held."""

        if not self._state.exists("pending.json"):
            for reservation in self._authority.incomplete_activation_reservations(self.profile_id):
                self._authority.transition_activation(
                    str(reservation["reservation_id"]),
                    expected_state=str(reservation["state"]),
                    new_state="aborted",
                )
            return
        pending = self._read_state("pending.json", "pending activation journal")
        expected_keys = {
            "schema",
            "reservation_id",
            "activation_id",
            "envelope_path",
            "envelope_digest",
            "workspace_digest",
            "fencing_token",
        }
        if (
            not isinstance(pending, dict)
            or set(pending) != expected_keys
            or pending.get("schema") != _PENDING_SCHEMA
            or pending.get("workspace_digest") != self._workspace_digest
        ):
            raise ProfileResolutionDenied("pending activation journal is invalid")
        reservation_id = str(pending["reservation_id"])
        loaded_reservation = self._authority.activation_reservation(reservation_id)
        if loaded_reservation is None:
            raise ProfileResolutionDenied("pending authority reservation is unavailable")
        reservation = loaded_reservation
        expected_binding = (
            str(pending["activation_id"]),
            int(pending["fencing_token"]),
            self.profile_id,
        )
        actual_binding = (
            str(reservation["activation_id"]),
            int(reservation["fencing_token"]),
            str(reservation["profile_id"]),
        )
        if actual_binding != expected_binding:
            raise ProfileResolutionDenied("pending activation authority binding changed")
        envelope_name = str(pending["envelope_path"])
        if Path(envelope_name).name != envelope_name:
            raise ProfileResolutionDenied("pending activation envelope path is invalid")
        envelope_path = self.state_root / "activations" / envelope_name
        state = str(reservation["state"])
        if state == "active":
            envelope = self._read_state(
                Path("activations") / envelope_name,
                "committed activation envelope",
            )
            if (
                not isinstance(envelope, dict)
                or canonical_digest(envelope) != pending["envelope_digest"]
                or not isinstance(envelope.get("activation"), dict)
                or envelope["activation"].get("state") != "active"
            ):
                raise ProfileResolutionDenied("committed activation envelope changed")
            self._revalidate_publish_reservation(
                reservation_id=reservation_id,
                activation_id=str(pending["activation_id"]),
                plan=envelope["plan"],
                profile=envelope["profile"],
                fencing_token=int(pending["fencing_token"]),
            )
            if not isinstance(envelope.get("profile"), Mapping):
                raise ProfileResolutionDenied("committed activation profile is invalid")
            self._verify_selected_artifact(envelope["profile"])
            self._write_state(
                "active.json",
                self._active_pointer(
                    str(pending["activation_id"]),
                    envelope_path,
                    str(pending["envelope_digest"]),
                ),
            )
            self._unlink_state("pending.json", missing_ok=True)
            return
        if state in {"prepared", "ready_without_authority", "committing"}:
            self._authority.transition_activation(
                reservation_id,
                expected_state=state,
                new_state="aborted",
            )
        elif state != "aborted":
            raise ProfileResolutionDenied("pending activation has an invalid authority state")
        self._unlink_state("pending.json", missing_ok=True)
        self._unlink_state(Path("activations") / envelope_name, missing_ok=True)

    def _revalidate_publish_reservation(
        self,
        *,
        reservation_id: str,
        activation_id: str,
        plan: Mapping[str, Any],
        profile: Mapping[str, Any],
        fencing_token: int,
    ) -> None:
        """Reject a retired, replaced, or stale reservation before publishing."""
        reservation = self._authority.activation_reservation(reservation_id)
        expected = (
            activation_id,
            self.profile_id,
            plan["plan_digest"],
            profile["profile_authority_snapshot_digest"],
            plan["security_epoch"],
            fencing_token,
            "active",
        )
        actual = (
            (
                reservation.get("activation_id"),
                reservation.get("profile_id"),
                reservation.get("plan_digest"),
                reservation.get("profile_authority_digest"),
                reservation.get("security_epoch"),
                reservation.get("fencing_token"),
                reservation.get("state"),
            )
            if reservation is not None
            else ()
        )
        if actual != expected or self._authority.security_epoch != plan["security_epoch"]:
            raise ProfileResolutionDenied(
                "activation reservation is stale immediately before publish"
            )

    @contextmanager
    def _activation_lock(self) -> Iterator[None]:
        """Serialize activation recovery and publication across Host processes."""
        profile_digest = hashlib.sha256(self.profile_id.encode("utf-8")).hexdigest()[:24]
        lock_name = f".activation-{profile_digest}.lock"
        try:
            descriptor = self._state.open_lock(lock_name)
        except (OSError, SecurePersistenceError) as exc:
            raise ProfileResolutionDenied("activation process lock is unavailable") from exc
        acquired = False
        try:
            if self._lock_platform == "nt":
                if os.fstat(descriptor).st_size == 0:
                    os.write(descriptor, b"0")
                    os.fsync(descriptor)
                backend = importlib.import_module("msvcrt")
                lock_mode = getattr(backend, "LK_NBLCK")
            else:
                backend = importlib.import_module("fcntl")
                lock_mode = getattr(backend, "LOCK_EX") | getattr(backend, "LOCK_NB")
            deadline = self._monotonic_clock() + self._lock_timeout_seconds
            while True:
                try:
                    if self._lock_platform == "nt":
                        os.lseek(descriptor, 0, os.SEEK_SET)
                        getattr(backend, "locking")(descriptor, lock_mode, 1)
                    else:
                        getattr(backend, "flock")(descriptor, lock_mode)
                    acquired = True
                    break
                except OSError as exc:
                    if exc.errno not in {
                        None,
                        errno.EACCES,
                        errno.EAGAIN,
                        errno.EDEADLK,
                    }:
                        raise ProfileResolutionDenied(
                            "activation process lock is unavailable"
                        ) from exc
                    remaining = deadline - self._monotonic_clock()
                    if remaining <= 0:
                        raise ActivationLockTimeout(
                            "activation process lock deadline exceeded"
                        ) from exc
                    self._retry_sleep(min(0.01, remaining))
            self._state.validate_open_file(lock_name, descriptor)
            yield
            self._state.validate_open_file(lock_name, descriptor)
        finally:
            try:
                if acquired:
                    if self._lock_platform == "nt":
                        os.lseek(descriptor, 0, os.SEEK_SET)
                        getattr(backend, "locking")(
                            descriptor,
                            getattr(backend, "LK_UNLCK"),
                            1,
                        )
                    else:
                        getattr(backend, "flock")(
                            descriptor,
                            getattr(backend, "LOCK_UN"),
                        )
            finally:
                os.close(descriptor)

    def _activation_record(
        self,
        profile: Mapping[str, Any],
        lock: Mapping[str, Any],
        plan: Mapping[str, Any],
        *,
        activation_id: str,
        state: str,
        generation: int,
        fencing_token: int,
        created_at: str,
        committed_at: str | None = None,
    ) -> Mapping[str, Any]:
        record: dict[str, Any] = {
            "activation_api_version": "io.tobkiri.activation-record.v2",
            "profile_id": profile["profile_id"],
            "profile_revision": plan["profile_revision"],
            "activation_id": activation_id,
            "state": state,
            "state_generation": generation,
            "catalog_revision": plan["catalog_revision"],
            "bundle_digest": plan["bundle_digest"],
            "lock_digest": lock["lock_digest"],
            "plan_digest": plan["plan_digest"],
            "closure_digest": plan["closure_digest"],
            "profile_authority_snapshot_digest": profile["profile_authority_snapshot_digest"],
            "security_epoch": plan["security_epoch"],
            "fencing_token": fencing_token,
            "created_at": created_at,
        }
        if committed_at is not None:
            record["committed_at"] = committed_at
        return validate_document(record, "activation")

    def _envelope(
        self,
        profile: Mapping[str, Any],
        lock: Mapping[str, Any],
        plan: Mapping[str, Any],
        activation: Mapping[str, Any],
    ) -> dict[str, Any]:
        return {
            "schema": _ENVELOPE_SCHEMA,
            "workspace_digest": self._workspace_digest,
            "profile": dict(profile),
            "lock": dict(lock),
            "plan": dict(plan),
            "activation": dict(activation),
        }

    def _pending_record(
        self,
        reservation_id: str,
        activation_id: str,
        envelope_path: Path,
        envelope_digest: str,
        fencing_token: int,
    ) -> dict[str, Any]:
        return {
            "schema": _PENDING_SCHEMA,
            "reservation_id": reservation_id,
            "activation_id": activation_id,
            "envelope_path": envelope_path.name,
            "envelope_digest": envelope_digest,
            "workspace_digest": self._workspace_digest,
            "fencing_token": fencing_token,
        }

    def _active_pointer(
        self, activation_id: str, envelope_path: Path, envelope_digest: str
    ) -> dict[str, Any]:
        return {
            "schema": _POINTER_SCHEMA,
            "activation_id": activation_id,
            "envelope_path": envelope_path.name,
            "envelope_digest": envelope_digest,
            "workspace_digest": self._workspace_digest,
        }

    def load_active_snapshot(self) -> ActiveDefaultProfile:
        """Load the exact activation snapshot and reject stale restart state."""
        with self._activation_lock():
            return self._load_active_snapshot_locked()

    def reconcile_active(
        self,
        resolved: ResolvedDefaultProfile,
        *,
        activation_id: str,
        created_at: str,
    ) -> Mapping[str, Any]:
        """Replace only a valid predecessor after an explicit confirmation.

        A normal restart is attempted first.  Reconciliation is permitted only
        when that attempt proves the persisted activation and its Authority
        reservation are valid, but the verified bundle requests a changed
        authority surface.  Hard denials (including stale internal revisions,
        digest changes, and missing Authority state) are never converted into a
        migration candidate.
        """

        if _ACTIVATION_RE.fullmatch(activation_id) is None:
            raise ProfileResolutionDenied("activation_id is not canonical")
        profile = validate_document(resolved.profile, "profile")
        lock = validate_document(resolved.lock, "profile_lock")
        plan = validate_document(resolved.plan, "resolved_plan")
        self._validate_record_graph(profile, lock, plan)
        if profile["profile_id"] != self.profile_id:
            raise ProfileResolutionDenied("activation Profile identity does not match store")
        with self._activation_lock():
            try:
                self._load_active_snapshot_locked()
            except ProfileReconfirmationRequired:
                pass
            else:
                raise ProfileResolutionDenied("activation confirmation was replayed")
            return self._activate_locked(
                ResolvedDefaultProfile(profile=profile, lock=lock, plan=plan),
                activation_id=activation_id,
                created_at=created_at,
                expected_predecessor_profile_revision=None,
                expected_predecessor_plan_digest=None,
                expected_predecessor_activation_id=None,
            )

    def _load_active_snapshot_locked(self) -> ActiveDefaultProfile:
        """Load one snapshot while holding the profile's process lock."""
        self._recover_locked()
        pointer = self._read_state("active.json", "active pointer")
        expected_pointer_keys = {
            "schema",
            "activation_id",
            "envelope_path",
            "envelope_digest",
            "workspace_digest",
        }
        if not isinstance(pointer, dict) or set(pointer) != expected_pointer_keys:
            raise ProfileResolutionDenied("active pointer is invalid")
        if pointer["schema"] != _POINTER_SCHEMA:
            raise ProfileResolutionDenied("active pointer schema is unsupported")
        if pointer["workspace_digest"] != self._workspace_digest:
            raise ProfileResolutionDenied("active Profile belongs to another workspace")
        envelope_name = pointer["envelope_path"]
        if not isinstance(envelope_name, str) or Path(envelope_name).name != envelope_name:
            raise ProfileResolutionDenied("active envelope path is invalid")
        envelope = self._read_state(
            Path("activations") / envelope_name,
            "activation envelope",
        )
        if (
            not isinstance(envelope, dict)
            or canonical_digest(envelope) != pointer["envelope_digest"]
        ):
            raise ProfileResolutionDenied("activation envelope digest changed")
        if envelope.get("schema") != _ENVELOPE_SCHEMA:
            raise ProfileResolutionDenied("activation envelope schema is unsupported")
        if envelope.get("workspace_digest") != self._workspace_digest:
            raise ProfileResolutionDenied("activation envelope belongs to another workspace")
        profile_value = envelope.get("profile")
        lock_value = envelope.get("lock")
        plan_value = envelope.get("plan")
        activation_value = envelope.get("activation")
        if not isinstance(profile_value, (dict, str, bytes)):
            raise ProfileResolutionDenied("activation profile record is invalid")
        if not isinstance(lock_value, (dict, str, bytes)):
            raise ProfileResolutionDenied("activation lock record is invalid")
        if not isinstance(plan_value, (dict, str, bytes)):
            raise ProfileResolutionDenied("activation plan record is invalid")
        if not isinstance(activation_value, (dict, str, bytes)):
            raise ProfileResolutionDenied("activation envelope records are invalid")
        if (
            isinstance(lock_value, dict)
            and isinstance(plan_value, dict)
            and isinstance(activation_value, dict)
            and (
                lock_value.get("lock_api_version") == "io.tobkiri.profile-lock.v4"
                or plan_value.get("plan_api_version") == "io.tobkiri.resolved-plan.v1"
                or activation_value.get("activation_api_version")
                == "io.tobkiri.activation-record.v1"
            )
        ):
            self._migrate_legacy_activation_locked(
                pointer=pointer,
                envelope=envelope,
                profile_value=profile_value,
                lock_value=lock_value,
                plan_value=plan_value,
                activation_value=activation_value,
            )
            return self._load_active_snapshot_locked()
        profile = validate_document(profile_value, "profile")
        lock = validate_document(lock_value, "profile_lock")
        plan = validate_document(plan_value, "resolved_plan")
        activation = validate_document(activation_value, "activation")
        self._validate_record_graph(profile, lock, plan)
        if activation["activation_id"] != pointer["activation_id"]:
            raise ProfileResolutionDenied("active pointer selects another activation")
        if (
            activation["state"] != "active"
            or activation["profile_revision"] != plan["profile_revision"]
            or activation["catalog_revision"] != plan["catalog_revision"]
            or activation["bundle_digest"] != plan["bundle_digest"]
            or activation["lock_digest"] != lock["lock_digest"]
            or activation["plan_digest"] != plan["plan_digest"]
            or activation["closure_digest"] != plan["closure_digest"]
        ):
            raise ProfileResolutionDenied("activation is stale or not active")
        authority = self._authority.active_activation_reservation(str(activation["activation_id"]))
        expected_authority = (
            activation["profile_id"],
            activation["plan_digest"],
            activation["profile_authority_snapshot_digest"],
            activation["security_epoch"],
            activation["fencing_token"],
        )
        actual_authority = (
            (
                authority.get("profile_id"),
                authority.get("plan_digest"),
                authority.get("profile_authority_digest"),
                authority.get("security_epoch"),
                authority.get("fencing_token"),
            )
            if authority is not None
            else ()
        )
        if (
            actual_authority != expected_authority
            or activation["security_epoch"] != self._authority.security_epoch
        ):
            raise ProfileResolutionDenied(
                "active activation authority, fence, or SecurityEpoch is stale"
            )
        self._verify_selected_artifact(
            profile,
            allow_verified_successor_reconfirmation=True,
        )
        return ActiveDefaultProfile(
            resolved=ResolvedDefaultProfile(profile=profile, lock=lock, plan=plan),
            activation=activation,
        )

    def _migrate_legacy_activation_locked(
        self,
        *,
        pointer: Mapping[str, Any],
        envelope: Mapping[str, Any],
        profile_value: Mapping[str, Any] | str | bytes,
        lock_value: Mapping[str, Any],
        plan_value: Mapping[str, Any],
        activation_value: Mapping[str, Any],
    ) -> None:
        """Re-resolve one frozen pre-v2 activation and publish its successor."""

        del pointer, envelope
        if self._catalog is None:
            raise ProfileResolutionDenied(
                "legacy activation migration requires the verified bundle catalog"
            )
        if not isinstance(profile_value, Mapping):
            raise ProfileResolutionDenied("legacy activation profile is invalid")
        profile = validate_document(profile_value, "profile")
        lock = validate_document(lock_value, "profile_lock")
        plan = validate_document(plan_value, "resolved_plan")
        activation = validate_document(activation_value, "activation")
        if (
            lock["lock_api_version"] != "io.tobkiri.profile-lock.v4"
            or plan["plan_api_version"] != "io.tobkiri.resolved-plan.v1"
            or activation["activation_api_version"] != "io.tobkiri.activation-record.v1"
        ):
            raise ProfileResolutionDenied("legacy activation versions do not form one record set")
        expected_plan_digest = canonical_digest(
            {key: value for key, value in plan.items() if key != "plan_digest"}
        )
        expected_lock_digest = canonical_digest(
            {key: value for key, value in lock.items() if key != "lock_digest"}
        )
        if (
            canonical_digest(profile) != plan["profile_revision"]
            or lock["profile_revision"] != plan["profile_revision"]
            or expected_plan_digest != plan["plan_digest"]
            or lock["plan_digest"] != plan["plan_digest"]
            or expected_lock_digest != lock["lock_digest"]
            or activation["plan_digest"] != plan["plan_digest"]
            or activation["profile_authority_snapshot_digest"]
            != profile["profile_authority_snapshot_digest"]
            or activation["security_epoch"] != plan["security_epoch"]
            or activation["security_epoch"] != self._authority.security_epoch
            or activation["state"] != "active"
        ):
            raise ProfileResolutionDenied("legacy activation predecessor is stale or tampered")
        legacy_effective = lock["effective_set"]
        profile_effective = {
            (profile["base"]["pack_id"], profile["base"]["artifact_digest"], "base"),
            (profile["shell"]["pack_id"], profile["shell"]["artifact_digest"], "shell"),
            *((item["pack_id"], item["artifact_digest"], "pack") for item in profile["packs"]),
        }
        if (
            profile["profile_id"] != self.profile_id
            or lock["profile_id"] != self.profile_id
            or plan["profile_id"] != self.profile_id
            or activation["profile_id"] != self.profile_id
            or lock["catalog_revision"] != profile["catalog_revision"]
            or lock["base"]["pack_id"] != plan["base"]["pack_id"]
            or lock["base"]["artifact_digest"] != plan["base"]["artifact_digest"]
            or lock["shell"]["pack_id"] != plan["shell"]["pack_id"]
            or lock["shell"]["artifact_digest"] != plan["shell"]["artifact_digest"]
            or {
                (item["identity"], item["artifact_digest"], item["role"])
                for item in legacy_effective
            }
            != profile_effective
        ):
            raise ProfileResolutionDenied("legacy activation record graph is inconsistent")
        authority = self._authority.active_activation_reservation(str(activation["activation_id"]))
        authority_binding = (
            (
                authority.get("profile_id"),
                authority.get("plan_digest"),
                authority.get("profile_authority_digest"),
                authority.get("security_epoch"),
                authority.get("fencing_token"),
            )
            if authority is not None
            else ()
        )
        if authority_binding != (
            activation["profile_id"],
            activation["plan_digest"],
            activation["profile_authority_snapshot_digest"],
            activation["security_epoch"],
            activation["fencing_token"],
        ):
            raise ProfileResolutionDenied("legacy activation Authority record is unavailable")
        edge_keys = {_edge_key(edge) for edge in profile["requested_edges"]}
        if len(edge_keys) != len(profile["requested_edges"]):
            raise ProfileResolutionDenied("legacy activation edge set is ambiguous")
        bindings = {
            _edge_key(edge): str(edge.get("authority_reference") or "")
            for edge in profile["requested_edges"]
        }
        if set(bindings) != edge_keys or any(
            not reference.startswith("authority-ref:") for reference in bindings.values()
        ):
            raise ProfileResolutionDenied("legacy activation Authority bindings are incomplete")
        approved = {
            str(manifest["pack"]["artifact_digest"]) for manifest in self._catalog.packs.values()
        }
        try:
            successor = resolve_default_profile(
                self._catalog,
                self.profile_id,
                approved_artifact_digests=approved,
                authority_snapshot_digest=str(activation["profile_authority_snapshot_digest"]),
                authority_bindings=bindings,
                security_epoch=int(activation["security_epoch"]),
            )
        except ProfileResolutionDenied as exc:
            if str(exc).startswith("Authority Kernel reference is missing for edge "):
                raise ProfileReconfirmationRequired(
                    f"legacy activation requires explicit reconfirmation: {exc}"
                ) from exc
            raise
        if (
            legacy_effective != successor.plan["effective_set"]
            or canonical_digest(
                {
                    "effective_set": legacy_effective,
                    "content_projections": successor.plan["content_projections"],
                }
            )
            != successor.plan["closure_digest"]
            or lock["base"] != successor.lock["base"]
            or {key: plan["base"][key] for key in ("pack_id", "artifact_digest")}
            != {key: successor.plan["base"][key] for key in ("pack_id", "artifact_digest")}
            or lock["shell"] != {key: successor.lock["shell"][key] for key in lock["shell"]}
            or {
                key: plan["shell"][key]
                for key in ("provider_id", "pack_id", "artifact_digest", "contract_id")
            }
            != {
                key: successor.plan["shell"][key]
                for key in ("provider_id", "pack_id", "artifact_digest", "contract_id")
            }
        ):
            raise ProfileResolutionDenied(
                "legacy activation artifact closure changed during migration"
            )
        legacy_bindings = {
            (
                item["function_principal"]["function_id"],
                item["contract_id"],
                item["operation_id"],
            ): {
                key: item[key]
                for key in (
                    "pack_id",
                    "artifact_digest",
                    "function_principal",
                    "contract_id",
                    "operation_id",
                    "domain_kind",
                )
            }
            for item in plan["bindings"]
        }
        successor_bindings = {
            (
                item["function_principal"]["function_id"],
                item["contract_id"],
                item["operation_id"],
            ): {
                key: item[key]
                for key in (
                    "pack_id",
                    "artifact_digest",
                    "function_principal",
                    "contract_id",
                    "operation_id",
                    "domain_kind",
                )
            }
            for item in successor.plan["bindings"]
        }
        if (
            len(legacy_bindings) != len(plan["bindings"])
            or len(successor_bindings) != len(successor.plan["bindings"])
            or legacy_bindings != successor_bindings
        ):
            raise ProfileResolutionDenied(
                "legacy activation principal binding changed during migration"
            )
        if {_edge_key(edge) for edge in successor.profile["requested_edges"]} != edge_keys:
            raise ProfileResolutionDenied("legacy activation edge set changed during migration")
        successor_edges = {_edge_key(edge): edge for edge in successor.profile["requested_edges"]}
        for legacy_edge in profile["requested_edges"]:
            successor_edge = successor_edges[_edge_key(legacy_edge)]
            legacy_template = legacy_edge["requested_scope_template"]
            if legacy_template and "dimensions" not in legacy_template:
                legacy_template = {
                    "dimensions": {str(key): [str(value)] for key, value in legacy_template.items()}
                }
            try:
                normalized_legacy = normalize_requested_scope_template(
                    legacy_template,
                    contract_id=str(legacy_edge["contract_id"]),
                    operation_id=str(legacy_edge["operation_id"]),
                    semantics_digest=str(
                        successor_edge["requested_scope_template"]["semantics_digest"]
                    ),
                )
            except ProtocolError as exc:
                raise ProfileResolutionDenied(
                    "legacy activation requested scope cannot be reconstructed"
                ) from exc
            if normalized_legacy != successor_edge["requested_scope_template"]:
                raise ProfileResolutionDenied(
                    "legacy activation requested scope changed during migration"
                )
        successor_id = (
            f"activation:{self.profile_id}-migration-"
            + successor.plan["plan_digest"].removeprefix("sha256:")[:16]
        )
        self._activate_locked(
            successor,
            activation_id=successor_id,
            created_at=str(activation.get("committed_at") or activation["created_at"]),
            expected_predecessor_profile_revision=None,
            expected_predecessor_plan_digest=None,
            expected_predecessor_activation_id=None,
        )

    def _verified_shell_successor_is_available(
        self,
        profile: Mapping[str, Any],
        definition: Mapping[str, Any],
    ) -> bool:
        """Return whether the catalog has one verified successor for this Shell.

        This classification is intentionally narrower than normal resolution.  It
        never activates or selects the successor.  It only permits the caller to
        surface an explicit reconfirmation transaction after the persisted record
        graph and Authority reservation have already been validated.
        """

        if self._catalog is None or self._catalog.artifact_root is None:
            return False
        shell = profile.get("shell")
        source_profile = self._catalog.profiles.get(self.profile_id)
        source_shell = source_profile.get("shell") if isinstance(source_profile, Mapping) else None
        if not isinstance(shell, Mapping) or not isinstance(source_shell, Mapping):
            return False
        stable_fields = (
            "provider_id",
            "pack_id",
            "contract_id",
            "platform",
            "architecture",
        )
        if any(shell.get(field) != source_shell.get(field) for field in stable_fields):
            return False
        if (
            definition.get("provider_id") != shell.get("provider_id")
            or definition.get("pack_id") != shell.get("pack_id")
            or definition.get("contract_id") != shell.get("contract_id")
            or definition.get("definition_revision") == shell.get("definition_revision")
        ):
            return False
        variants = [
            item
            for item in definition["launch"]["variants"]
            if item["platform"] == shell.get("platform")
            and item["architecture"] == shell.get("architecture")
        ]
        if len(variants) != 1:
            return False
        successor = variants[0]
        shell_manifest = self._catalog.packs.get(str(definition.get("pack_id")))
        if (
            not isinstance(shell_manifest, Mapping)
            or shell_manifest["pack"].get("kind") != "shell"
            or source_shell.get("artifact_digest") != successor["artifact_digest"]
            or source_shell.get("executable_artifact_digest") != successor["entrypoint_digest"]
            or source_shell.get("definition_revision") != definition.get("definition_revision")
            or definition.get("artifact_digest") != successor["artifact_digest"]
        ):
            return False
        try:
            verify_platform_artifact(self._catalog.artifact_root, successor)
        except ProtocolError as exc:
            raise ProfileResolutionDenied(
                f"verified successor Shell artifact rejected: {exc}"
            ) from exc
        return True

    def _verify_selected_artifact(
        self,
        profile: Mapping[str, Any],
        *,
        allow_verified_successor_reconfirmation: bool = False,
    ) -> None:
        """Reverify the exact selected Shell/Application bytes when catalogued."""

        if self._catalog is None:
            return
        shell = profile.get("shell")
        if not isinstance(shell, Mapping):
            raise ProfileResolutionDenied("active Profile Shell binding is unavailable")
        definition = self._catalog.shells.get(str(shell.get("provider_id")))
        if not isinstance(definition, Mapping) or definition.get("availability") != "verified":
            raise ProfileResolutionDenied("active Profile Shell definition is unavailable")
        variants = [
            item
            for item in definition["launch"]["variants"]
            if item["platform"] == shell.get("platform")
            and item["architecture"] == shell.get("architecture")
            and item["entrypoint_digest"] == shell.get("executable_artifact_digest")
        ]
        source_profile = self._catalog.profiles.get(self.profile_id)
        source_shell = source_profile.get("shell") if isinstance(source_profile, Mapping) else None
        shell_manifest = self._catalog.packs.get(str(shell.get("pack_id")))
        stable_fields = (
            "provider_id",
            "pack_id",
            "contract_id",
            "platform",
            "architecture",
        )
        exact_current_binding = (
            len(variants) == 1
            and isinstance(source_shell, Mapping)
            and isinstance(shell_manifest, Mapping)
            and all(shell.get(field) == source_shell.get(field) for field in stable_fields)
            and shell.get("artifact_digest") == shell_manifest["pack"]["artifact_digest"]
            and shell.get("executable_artifact_digest")
            == source_shell.get("executable_artifact_digest")
            and shell.get("definition_revision") == definition.get("definition_revision")
        )
        if (
            not exact_current_binding
            and allow_verified_successor_reconfirmation
            and self._verified_shell_successor_is_available(profile, definition)
        ):
            raise ProfileReconfirmationRequired(
                "active Profile Shell artifact identity was superseded by the "
                "verified packaged release; explicit reconfirmation is required"
            )
        if not exact_current_binding or self._catalog.artifact_root is None:
            raise ProfileResolutionDenied("active Profile Shell artifact is unavailable")
        try:
            verify_platform_artifact(self._catalog.artifact_root, variants[0])
        except ProtocolError as exc:
            raise ProfileResolutionDenied(f"active Profile Shell artifact rejected: {exc}") from exc

    def load_active(self) -> ResolvedDefaultProfile:
        """Load validated Profile/Lock/Plan records for compatibility callers."""
        return self.load_active_snapshot().resolved

    @staticmethod
    def _validate_record_graph(
        profile: Mapping[str, Any],
        lock: Mapping[str, Any],
        plan: Mapping[str, Any],
    ) -> None:
        profile_revision = canonical_digest(profile)
        expected_plan_digest = canonical_digest(
            {key: value for key, value in plan.items() if key != "plan_digest"}
        )
        expected_lock_digest = canonical_digest(
            {key: value for key, value in lock.items() if key != "lock_digest"}
        )
        if (
            lock["profile_revision"] != profile_revision
            or plan["profile_revision"] != profile_revision
        ):
            raise ProfileResolutionDenied("ProfileLock or ResolvedPlan is stale")
        if (
            plan["plan_digest"] != expected_plan_digest
            or lock["plan_digest"] != expected_plan_digest
        ):
            raise ProfileResolutionDenied("ResolvedPlan digest is stale")
        if lock["lock_digest"] != expected_lock_digest:
            raise ProfileResolutionDenied("ProfileLock digest is stale")
        if lock["security_epoch"] != plan["security_epoch"]:
            raise ProfileResolutionDenied("ProfileLock security epoch is stale")
        snapshot = profile["profile_authority_snapshot_digest"]
        shared_fields = (
            "profile_definition_digest",
            "catalog_revision",
            "bundle_digest",
            "application",
            "effective_set",
            "content_projections",
            "requested_edges_digest",
            "constraints_digest",
            "closure_digest",
            "provenance_digest",
        )
        if any(lock[field] != plan[field] for field in shared_fields):
            raise ProfileResolutionDenied("ProfileLock and ResolvedPlan bindings diverge")
        if (
            plan["profile_authority_snapshot_digest"] != snapshot
            or lock["profile_authority_snapshot_digest"] != snapshot
        ):
            raise ProfileResolutionDenied("Profile authority snapshot is stale")
        if plan["catalog_revision"] != profile["catalog_revision"]:
            raise ProfileResolutionDenied("Profile catalog revision is stale")
        if plan["requested_edges_digest"] != canonical_digest(profile["requested_edges"]):
            raise ProfileResolutionDenied("Profile requested edge set is stale")
        if plan["provenance_digest"] != canonical_digest(profile["provenance"]):
            raise ProfileResolutionDenied("Profile provenance binding is stale")
        if plan["closure_digest"] != canonical_digest(
            {
                "effective_set": plan["effective_set"],
                "content_projections": plan["content_projections"],
            }
        ):
            raise ProfileResolutionDenied("Profile closure digest is stale")
        from core_runtime.profile_content_projection import selected_projection_roots

        selected_projection_roots(plan["content_projections"])
        effective_ids = [item["identity"] for item in plan["effective_set"]]
        if len(effective_ids) != len(set(effective_ids)):
            raise ProfileResolutionDenied("Profile closure contains duplicate artifacts")
        base_rows = [item for item in plan["effective_set"] if item["role"] == "base"]
        shell_rows = [item for item in plan["effective_set"] if item["role"] == "shell"]
        if len(base_rows) != 1 or (
            base_rows[0]["identity"],
            base_rows[0]["artifact_digest"],
        ) != (plan["base"]["pack_id"], plan["base"]["artifact_digest"]):
            raise ProfileResolutionDenied("Profile Base closure binding is stale")
        if len(shell_rows) != 1 or (
            shell_rows[0]["identity"],
            shell_rows[0]["artifact_digest"],
        ) != (plan["shell"]["pack_id"], plan["shell"]["artifact_digest"]):
            raise ProfileResolutionDenied("Profile Shell closure binding is stale")
        profile_pack_set = {(item["pack_id"], item["artifact_digest"]) for item in profile["packs"]}
        closure_pack_set = {
            (item["identity"], item["artifact_digest"])
            for item in plan["effective_set"]
            if item["role"] == "pack"
        }
        if profile_pack_set != closure_pack_set:
            raise ProfileResolutionDenied("Profile Pack closure binding is stale")
        application_rows = [item for item in profile["packs"] if item.get("role") == "application"]
        if len(application_rows) != 1 or (
            application_rows[0]["pack_id"],
            application_rows[0]["artifact_digest"],
        ) != (
            plan["application"]["pack_id"],
            plan["application"]["artifact_digest"],
        ):
            raise ProfileResolutionDenied("Profile Application binding is stale")
        edge_bindings = {
            _edge_key(edge): (
                edge["authority_reference"],
                canonical_digest(edge["requested_scope_template"]),
                _authority_mode(edge),
            )
            for edge in profile["requested_edges"]
        }
        plan_bindings = {
            _edge_key(
                {
                    "caller_function_id": binding["caller_function_id"],
                    "target_provider_id": binding["function_principal"]["function_id"],
                    "contract_id": binding["contract_id"],
                    "operation_id": binding["operation_id"],
                }
            ): binding
            for binding in plan["bindings"]
        }
        for edge in profile["requested_edges"]:
            matches = [
                binding
                for binding in plan["bindings"]
                if binding["function_principal"]["function_id"] == edge["target_provider_id"]
                and binding["caller_function_id"] == edge["caller_function_id"]
                and binding["contract_id"] == edge["contract_id"]
                and binding["operation_id"] == edge["operation_id"]
            ]
            if (
                len(matches) != 1
                or (
                    matches[0]["authority_reference"],
                    matches[0]["requested_scope_digest"],
                    _authority_mode(matches[0]),
                )
                != edge_bindings[_edge_key(edge)]
            ):
                raise ProfileResolutionDenied("ResolvedPlan Authority binding is stale")
        if len(plan_bindings) != len(plan["bindings"]):
            raise ProfileResolutionDenied("ResolvedPlan contains duplicate bindings")
