"""Authoritative immutable runtime-plan resolution for one profile revision."""

from __future__ import annotations

import hashlib
import json
import os
import platform as host_platform
import stat
import threading
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from .dependency_resolver import extract_dependency_specs, version_satisfies
from .global_contracts.canonical import canonical_json, content_identity
from .global_contracts.models import (
    Cardinality,
    ContractDescriptor,
    ContractRequirement,
    ContractStatus,
    FailureSemantics,
    LifecycleMetadata,
    ProviderDescriptor,
    SecurityClassification,
)
from .global_contracts.registry import ContractRegistry
from .paths import PackLocation, resolve_pack_locations

RESOLVED_PROFILE_VERSION = "rumi.resolved-profile.v1"
LOCKFILE_VERSION = "rumi.profile-lock.v1"

PROJECTION_TYPES = (
    "routes",
    "ui",
    "tools",
    "prompts",
    "models",
    "providers",
    "services",
    "resources",
    "graphs",
    "policies",
    "scheduler",
)

_PACK_CONTENT_IGNORED_DIRECTORY_NAMES = frozenset({"__pycache__"})
_PACK_CONTENT_IGNORED_SUFFIXES = frozenset({".pyc", ".pyo"})

_PACK_CONTENT_HASH_CACHE_LOCK = threading.RLock()
_PACK_CONTENT_HASH_CACHE: dict[
    tuple[str, str],
    tuple[tuple[tuple[str, int, int, int, int, int, int], ...], str],
] = {}

_COMPONENT_TYPE_TO_PROJECTION = {
    "frontend": "ui",
    "route": "routes",
    "tool": "tools",
    "prompt": "prompts",
    "model": "models",
    "provider": "providers",
    "ai_client": "providers",
    "service": "services",
    "resource": "resources",
    "graph": "graphs",
    "policy": "policies",
    "scheduler": "scheduler",
}


@dataclass(frozen=True)
class ResolutionDiagnostic:
    """One stable explanation emitted while creating a runtime plan."""

    code: str
    severity: str
    message: str
    subject: str | None = None
    details: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class ResolvedPack:
    """A selected pack identity with host-verified trust metadata."""

    pack_id: str
    version: str
    manifest_hash: str
    content_hash: str
    requested: bool
    available: bool
    selected: bool
    healthy: bool
    authorized: bool
    trust_class: str = "untrusted"


@dataclass(frozen=True)
class ResolvedProvider:
    """A selected global-contract provider without executable/source paths."""

    contract_id: str
    provider_instance_id: str
    source_pack_id: str
    version: str
    content_hash: str
    credential_handle: str | None = None
    credential_scopes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        """Reject credential values masquerading as opaque handles."""
        if self.credential_handle is not None and not self.credential_handle.startswith(
            ("credential:", "opaque:")
        ):
            raise ValueError("credential_handle must be an opaque handle")


@dataclass(frozen=True)
class ResourceProjection:
    """A complete projection contributed by one effective pack."""

    kind: str
    resource_id: str
    source_pack_id: str
    content_hash: str


@dataclass(frozen=True)
class ResolutionInput:
    """All inputs that participate in deterministic plan identity."""

    profile_id: str
    profile_revision: str
    platform: str
    policy_revision: str
    lockfile_revision: str | None
    requested_pack_ids: tuple[str, ...]
    requested_contracts: tuple[ContractRequirement, ...] = ()
    authorized_pack_ids: tuple[str, ...] = ()
    healthy_pack_ids: tuple[str, ...] = ()
    policy_capabilities: tuple[str, ...] = ()
    verified_pack_trust: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class ResolvedProfile:
    """Immutable, complete, revision-bound runtime plan."""

    version: str
    profile_id: str
    profile_revision: str
    platform: str
    policy_revision: str
    requested_pack_ids: tuple[str, ...]
    available_pack_ids: tuple[str, ...]
    selected_pack_ids: tuple[str, ...]
    healthy_pack_ids: tuple[str, ...]
    authorized_pack_ids: tuple[str, ...]
    effective_pack_set: tuple[str, ...]
    packs: tuple[ResolvedPack, ...]
    providers: tuple[ResolvedProvider, ...]
    projections: tuple[ResourceProjection, ...]
    effective_permissions: tuple[str, ...]
    diagnostics: tuple[ResolutionDiagnostic, ...]
    input_hash: str
    plan_hash: str

    def projections_for(self, kind: str) -> tuple[ResourceProjection, ...]:
        """Return one precomputed projection without rediscovering packs."""
        return tuple(item for item in self.projections if item.kind == kind)

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-safe snapshot of this immutable plan."""
        return asdict(self)


@dataclass(frozen=True)
class ProfileLockfile:
    """Secret-free lockfile for every selected pack/provider/resource."""

    version: str
    profile_id: str
    profile_revision: str
    plan_hash: str
    input_hash: str
    packs: tuple[ResolvedPack, ...]
    providers: tuple[ResolvedProvider, ...]
    resources: tuple[ResourceProjection, ...]
    lock_hash: str

    def to_dict(self) -> dict[str, Any]:
        """Return canonical lockfile data suitable for persistence."""
        return asdict(self)


@dataclass(frozen=True)
class LockfileValidation:
    """Result of comparing a lockfile with a newly resolved plan."""

    status: ContractStatus
    diagnostics: tuple[ResolutionDiagnostic, ...] = ()


def resolve_profile(
    resolution_input: ResolutionInput,
    *,
    ecosystem_dir: Path | str | None = None,
    providers: Sequence[ProviderDescriptor] = (),
    expected_lockfile: ProfileLockfile | None = None,
) -> ResolvedProfile:
    """Resolve the single authoritative runtime plan from explicit inputs."""
    requested = _unique(resolution_input.requested_pack_ids)
    manifests, diagnostics = _read_manifest_closure(
        requested,
        ecosystem_dir=(str(ecosystem_dir) if ecosystem_dir is not None else None),
    )
    available = tuple(sorted(manifests))
    selected, dependency_diagnostics = _dependency_closure(requested, manifests)
    diagnostics.extend(dependency_diagnostics)

    authorized = set(resolution_input.authorized_pack_ids)
    healthy = set(resolution_input.healthy_pack_ids)
    verified_pack_trust = _verified_pack_trust(
        resolution_input.verified_pack_trust
    )
    if not resolution_input.healthy_pack_ids:
        healthy = set(available)

    effective = tuple(
        pack_id
        for pack_id in selected
        if pack_id in manifests and pack_id in authorized and pack_id in healthy
    )
    for pack_id in selected:
        if pack_id not in manifests:
            diagnostics.append(
                _diagnostic(
                    "missing_pack",
                    "error",
                    f"Selected pack is not available: {pack_id}",
                    pack_id,
                )
            )
        elif pack_id not in authorized:
            diagnostics.append(
                _diagnostic(
                    "pack_not_authorized",
                    "error",
                    f"Selected pack is not authorized: {pack_id}",
                    pack_id,
                )
            )
        elif pack_id not in healthy:
            diagnostics.append(
                _diagnostic(
                    "pack_unhealthy",
                    "warning",
                    f"Selected pack is unhealthy: {pack_id}",
                    pack_id,
                )
            )

    resolved_packs = tuple(
        _resolved_pack(
            pack_id,
            manifests.get(pack_id),
            requested=pack_id in requested,
            selected=pack_id in selected,
            healthy=pack_id in healthy,
            authorized=pack_id in authorized,
            trust_class=verified_pack_trust.get(pack_id, "untrusted"),
        )
        for pack_id in selected
    )
    projections = _project_resources(effective, manifests)
    manifest_providers, manifest_requirements, manifest_diagnostics = (
        _manifest_contract_metadata(
            effective,
            manifests,
            verified_pack_trust=verified_pack_trust,
        )
    )
    diagnostics.extend(manifest_diagnostics)
    requirement_map = {
        item.contract_id: item for item in manifest_requirements
    }
    requirement_map.update(
        {
            item.contract_id: item
            for item in resolution_input.requested_contracts
        }
    )
    requirements = tuple(
        requirement_map[key] for key in sorted(requirement_map)
    )
    resolved_providers, provider_diagnostics = _resolve_contracts(
        requirements,
        (*providers, *manifest_providers),
        effective,
    )
    diagnostics.extend(provider_diagnostics)
    effective_permissions = _effective_permissions(
        effective,
        manifests,
        resolution_input.policy_capabilities,
    )

    input_payload = _input_payload(resolution_input, manifests)
    input_hash = content_identity(input_payload)
    plan_payload = {
        "version": RESOLVED_PROFILE_VERSION,
        "profile_id": resolution_input.profile_id,
        "profile_revision": resolution_input.profile_revision,
        "platform": resolution_input.platform,
        "policy_revision": resolution_input.policy_revision,
        "requested_pack_ids": requested,
        "available_pack_ids": available,
        "selected_pack_ids": selected,
        "healthy_pack_ids": tuple(sorted(healthy)),
        "authorized_pack_ids": tuple(sorted(authorized)),
        "effective_pack_set": effective,
        "packs": tuple(asdict(item) for item in resolved_packs),
        "providers": tuple(asdict(item) for item in resolved_providers),
        "projections": tuple(asdict(item) for item in projections),
        "effective_permissions": effective_permissions,
        "diagnostics": tuple(asdict(item) for item in diagnostics),
        "input_hash": input_hash,
    }
    plan_hash = content_identity(plan_payload)
    plan = ResolvedProfile(
        version=RESOLVED_PROFILE_VERSION,
        profile_id=resolution_input.profile_id,
        profile_revision=resolution_input.profile_revision,
        platform=resolution_input.platform,
        policy_revision=resolution_input.policy_revision,
        requested_pack_ids=requested,
        available_pack_ids=available,
        selected_pack_ids=selected,
        healthy_pack_ids=tuple(sorted(healthy)),
        authorized_pack_ids=tuple(sorted(authorized)),
        effective_pack_set=effective,
        packs=resolved_packs,
        providers=resolved_providers,
        projections=projections,
        effective_permissions=effective_permissions,
        diagnostics=tuple(diagnostics),
        input_hash=input_hash,
        plan_hash=plan_hash,
    )
    if expected_lockfile is not None:
        validation = validate_lockfile(expected_lockfile, plan)
        if validation.status is not ContractStatus.OK:
            return _with_diagnostics(plan, validation.diagnostics)
    return plan


def resolution_input_from_startup_profile(
    profile: Mapping[str, Any],
    *,
    profile_revision: str | None = None,
    policy_revision: str | None = None,
    lockfile_revision: str | None = None,
    legacy_selection: Mapping[str, Any] | None = None,
    verified_pack_trust: Mapping[str, str] | None = None,
) -> ResolutionInput:
    """Normalize startup and legacy setup selection into one explicit input."""
    profile_id = str(profile.get("profile_id") or profile.get("id") or "").strip()
    if not profile_id:
        raise ValueError("profile_id is required")
    requested = _string_items(profile.get("packs"))
    base_pack = str(profile.get("base_pack") or "").strip()
    if base_pack:
        requested = (base_pack, *requested)
    if legacy_selection:
        requested = (*requested, *_legacy_selected_pack_ids(legacy_selection))
    permissions = profile.get("permissions")
    policy = profile.get("policy")
    authorized = _string_items(
        permissions.get("authorized_pack_ids")
        if isinstance(permissions, Mapping)
        else None
    )
    capabilities = _string_items(
        policy.get("capabilities") if isinstance(policy, Mapping) else None
    )
    return ResolutionInput(
        profile_id=profile_id,
        profile_revision=profile_revision or _revision_of(profile),
        platform=host_platform.system().lower(),
        policy_revision=policy_revision or _revision_of(policy or {}),
        lockfile_revision=lockfile_revision,
        requested_pack_ids=_unique(requested),
        requested_contracts=(),
        authorized_pack_ids=_unique(authorized),
        healthy_pack_ids=(),
        policy_capabilities=_unique(capabilities),
        verified_pack_trust=tuple(
            sorted(
                (str(pack_id), str(trust_class))
                for pack_id, trust_class in (verified_pack_trust or {}).items()
            )
        ),
    )


def create_lockfile(plan: ResolvedProfile) -> ProfileLockfile:
    """Create a complete secret-free lockfile from a resolved plan."""
    payload = {
        "version": LOCKFILE_VERSION,
        "profile_id": plan.profile_id,
        "profile_revision": plan.profile_revision,
        "plan_hash": plan.plan_hash,
        "input_hash": plan.input_hash,
        "packs": tuple(asdict(item) for item in plan.packs),
        "providers": tuple(asdict(item) for item in plan.providers),
        "resources": tuple(asdict(item) for item in plan.projections),
    }
    return ProfileLockfile(
        version=LOCKFILE_VERSION,
        profile_id=plan.profile_id,
        profile_revision=plan.profile_revision,
        plan_hash=plan.plan_hash,
        input_hash=plan.input_hash,
        packs=plan.packs,
        providers=plan.providers,
        resources=plan.projections,
        lock_hash=content_identity(payload),
    )


def write_lockfile(path: Path, lockfile: ProfileLockfile) -> None:
    """Atomically persist a canonical lockfile with owner-only permissions."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_bytes(canonical_json(lockfile.to_dict()) + b"\n")
    os.chmod(temporary, 0o600)
    temporary.replace(path)


def read_lockfile(path: Path) -> ProfileLockfile:
    """Read and verify a lockfile without accepting unknown secret fields."""
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or payload.get("version") != LOCKFILE_VERSION:
        raise ValueError("unsupported profile lockfile")
    allowed = {
        "version",
        "profile_id",
        "profile_revision",
        "plan_hash",
        "input_hash",
        "packs",
        "providers",
        "resources",
        "lock_hash",
    }
    unknown = set(payload) - allowed
    if unknown:
        raise ValueError("unknown lockfile fields: " + ", ".join(sorted(unknown)))
    packs = tuple(ResolvedPack(**item) for item in payload.get("packs", ()))
    providers = tuple(
        ResolvedProvider(
            **{
                **item,
                "credential_scopes": tuple(item.get("credential_scopes", ())),
            }
        )
        for item in payload.get("providers", ())
    )
    resources = tuple(
        ResourceProjection(**item) for item in payload.get("resources", ())
    )
    lockfile = ProfileLockfile(
        version=payload["version"],
        profile_id=str(payload["profile_id"]),
        profile_revision=str(payload["profile_revision"]),
        plan_hash=str(payload["plan_hash"]),
        input_hash=str(payload["input_hash"]),
        packs=packs,
        providers=providers,
        resources=resources,
        lock_hash=str(payload["lock_hash"]),
    )
    expected = _lock_identity(lockfile)
    if lockfile.lock_hash != expected:
        raise ValueError("profile lockfile hash mismatch")
    return lockfile


def refresh_lockfile(path: Path, plan: ResolvedProfile) -> ProfileLockfile:
    """Replace a stale lockfile atomically from an explicit new plan revision."""
    lockfile = create_lockfile(plan)
    write_lockfile(path, lockfile)
    return lockfile


def validate_lockfile(
    lockfile: ProfileLockfile, plan: ResolvedProfile
) -> LockfileValidation:
    """Fail closed when any selected pack/provider/resource revision is stale."""
    diagnostics: list[ResolutionDiagnostic] = []
    if lockfile.lock_hash != _lock_identity(lockfile):
        diagnostics.append(
            _diagnostic(
                "invalid_lockfile_hash",
                "error",
                "Lockfile content does not match its identity",
                lockfile.profile_id,
            )
        )
    if lockfile.profile_id != plan.profile_id:
        diagnostics.append(
            _diagnostic(
                "lockfile_profile_mismatch",
                "error",
                "Lockfile belongs to a different profile",
                lockfile.profile_id,
            )
        )
    if lockfile.profile_revision != plan.profile_revision:
        diagnostics.append(
            _diagnostic(
                "stale_profile_revision",
                "error",
                "Profile revision changed since lockfile creation",
                plan.profile_id,
            )
        )
    if lockfile.input_hash != plan.input_hash or lockfile.plan_hash != plan.plan_hash:
        diagnostics.append(
            _diagnostic(
                "stale_resolution",
                "error",
                "Resolved inputs or selected resource hashes changed",
                plan.profile_id,
            )
        )
    return LockfileValidation(
        status=(
            ContractStatus.STALE_RESOLUTION
            if diagnostics
            else ContractStatus.OK
        ),
        diagnostics=tuple(diagnostics),
    )


def _read_manifests(
    locations: Iterable[PackLocation],
) -> tuple[dict[str, dict[str, Any]], list[ResolutionDiagnostic]]:
    """Reject legacy projection loading from every runtime resolution path."""

    locations = tuple(locations)
    return {}, [
        _diagnostic(
            "offline_projection_not_authority",
            "error",
            "ecosystem.json and rumi.pack.v3.json are offline projections; use Pack v4",
            location.pack_id,
        )
        for location in locations
    ]


def _read_manifest_closure(
    requested: tuple[str, ...],
    *,
    ecosystem_dir: str | None,
) -> tuple[dict[str, dict[str, Any]], list[ResolutionDiagnostic]]:
    """Read only requested manifests and their explicit dependency closure."""
    manifests: dict[str, dict[str, Any]] = {}
    diagnostics: list[ResolutionDiagnostic] = []
    pending = set(requested)
    visited: set[str] = set()
    while pending:
        batch = tuple(sorted(pending - visited))
        if not batch:
            break
        visited.update(batch)
        discovered, batch_diagnostics = _read_manifests(
            resolve_pack_locations(batch, ecosystem_dir)
        )
        manifests.update(discovered)
        diagnostics.extend(batch_diagnostics)
        for manifest in discovered.values():
            pending.update(
                spec["pack_id"]
                for spec in extract_dependency_specs(manifest)
            )
    return manifests, diagnostics


def _dependency_closure(
    requested: tuple[str, ...], manifests: Mapping[str, Mapping[str, Any]]
) -> tuple[tuple[str, ...], list[ResolutionDiagnostic]]:
    selected: set[str] = set()
    visiting: list[str] = []
    diagnostics: list[ResolutionDiagnostic] = []

    def visit(pack_id: str) -> None:
        if pack_id in selected:
            return
        if pack_id in visiting:
            cycle = " -> ".join((*visiting, pack_id))
            diagnostics.append(
                _diagnostic(
                    "dependency_cycle",
                    "error",
                    f"Pack dependency cycle: {cycle}",
                    pack_id,
                )
            )
            return
        visiting.append(pack_id)
        manifest = manifests.get(pack_id)
        if manifest is not None:
            for spec in sorted(
                extract_dependency_specs(dict(manifest)),
                key=lambda item: item["pack_id"],
            ):
                target = spec["pack_id"]
                target_manifest = manifests.get(target)
                constraint = spec.get("version")
                if target_manifest is None:
                    diagnostics.append(
                        _diagnostic(
                            "missing_dependency",
                            "error",
                            f"Required pack is unavailable: {target}",
                            pack_id,
                            target=target,
                        )
                    )
                elif constraint and not version_satisfies(
                    target_manifest.get("version"), constraint
                ):
                    diagnostics.append(
                        _diagnostic(
                            "incompatible_dependency",
                            "error",
                            f"Pack {target} does not satisfy {constraint}",
                            pack_id,
                            target=target,
                        )
                    )
                visit(target)
        visiting.pop()
        selected.add(pack_id)

    for pack_id in requested:
        visit(pack_id)
    return tuple(sorted(selected)), diagnostics


def _resolved_pack(
    pack_id: str,
    manifest: Mapping[str, Any] | None,
    *,
    requested: bool,
    selected: bool,
    healthy: bool,
    authorized: bool,
    trust_class: str,
) -> ResolvedPack:
    return ResolvedPack(
        pack_id=pack_id,
        version=str((manifest or {}).get("version") or "unknown"),
        manifest_hash=str((manifest or {}).get("_manifest_hash") or "missing"),
        content_hash=str((manifest or {}).get("_content_hash") or "missing"),
        requested=requested,
        available=manifest is not None,
        selected=selected,
        healthy=healthy,
        authorized=authorized,
        trust_class=trust_class,
    )


def _verified_pack_trust(
    values: tuple[tuple[str, str], ...],
) -> dict[str, str]:
    """Validate host-supplied trust records without consulting pack manifests."""
    result: dict[str, str] = {}
    for pack_id, trust_class in values:
        normalized_pack_id = str(pack_id or "").strip()
        normalized_trust_class = str(trust_class or "").strip()
        if not normalized_pack_id:
            raise ValueError("verified pack trust requires a pack ID")
        if normalized_trust_class not in {
            "untrusted",
            "local",
            "verified",
            "system",
        }:
            raise ValueError(
                f"invalid verified trust class: {normalized_trust_class!r}"
            )
        if normalized_pack_id in result:
            raise ValueError(
                f"duplicate verified pack trust record: {normalized_pack_id}"
            )
        result[normalized_pack_id] = normalized_trust_class
    return result


def _project_resources(
    effective: tuple[str, ...], manifests: Mapping[str, Mapping[str, Any]]
) -> tuple[ResourceProjection, ...]:
    projections: list[ResourceProjection] = []
    for pack_id in effective:
        manifest = manifests[pack_id]
        content_hash = str(manifest["_content_hash"])
        components = manifest.get("components")
        entries: Iterable[tuple[Any, Any]]
        if isinstance(components, Mapping):
            entries = components.items()
        elif isinstance(components, list):
            entries = (
                (str(item.get("id") or index), item)
                for index, item in enumerate(components)
                if isinstance(item, Mapping)
            )
        else:
            entries = ()
        for component_id, component in entries:
            if not isinstance(component, Mapping):
                continue
            component_type = str(component.get("type") or "").lower()
            kind = _COMPONENT_TYPE_TO_PROJECTION.get(component_type)
            if kind is None:
                continue
            resource_id = str(component.get("id") or component_id).strip()
            if resource_id:
                projections.append(
                    ResourceProjection(kind, resource_id, pack_id, content_hash)
                )
        for kind in PROJECTION_TYPES:
            values = manifest.get(kind)
            resource_ids: Iterable[Any]
            if isinstance(values, Mapping):
                resource_ids = values.keys()
            elif isinstance(values, list):
                resource_ids = (
                    item.get("id") if isinstance(item, Mapping) else item
                    for item in values
                )
            else:
                continue
            for resource_id in resource_ids:
                value = str(resource_id or "").strip()
                if value:
                    projections.append(
                        ResourceProjection(kind, value, pack_id, content_hash)
                    )
    return tuple(
        sorted(
            set(projections),
            key=lambda item: (item.kind, item.resource_id, item.source_pack_id),
        )
    )


def _manifest_contract_metadata(
    effective: tuple[str, ...],
    manifests: Mapping[str, Mapping[str, Any]],
    *,
    verified_pack_trust: Mapping[str, str],
) -> tuple[
    tuple[ProviderDescriptor, ...],
    tuple[ContractRequirement, ...],
    list[ResolutionDiagnostic],
]:
    """Project validated v3 descriptors without importing pack code."""
    providers: list[ProviderDescriptor] = []
    requirements: dict[str, ContractRequirement] = {}
    diagnostics: list[ResolutionDiagnostic] = []
    for pack_id in effective:
        ecosystem_manifest = manifests[pack_id]
        manifest = ecosystem_manifest.get("_v3_manifest")
        if not isinstance(manifest, Mapping):
            continue
        provenance = manifest.get("provenance")
        provenance = provenance if isinstance(provenance, Mapping) else {}
        contracts = manifest.get("contracts")
        contracts = contracts if isinstance(contracts, Mapping) else {}
        for item in contracts.get("provides", []):
            if not isinstance(item, Mapping):
                continue
            try:
                cardinality = Cardinality(str(item.get("cardinality")))
                lifecycle_data = item.get("lifecycle")
                lifecycle_data = (
                    lifecycle_data if isinstance(lifecycle_data, Mapping) else {}
                )
                schemas = item.get("schemas")
                schemas = schemas if isinstance(schemas, Mapping) else {}
                contract = ContractDescriptor(
                    contract_id=str(item.get("id") or ""),
                    version=str(item.get("version") or ""),
                    cardinality=cardinality,
                    security=SecurityClassification(str(item.get("security"))),
                    failure=FailureSemantics(str(item.get("failure"))),
                    lifecycle=LifecycleMetadata(**dict(lifecycle_data)),
                    input_schema=schemas.get("input"),
                    output_schema=schemas.get("output"),
                    event_schema=schemas.get("event"),
                )
                provider = ProviderDescriptor(
                    contract=contract,
                    provider_instance_id=str(
                        item.get("provider_instance_id") or ""
                    ),
                    source_pack_id=pack_id,
                    source_pack_version=str(
                        ecosystem_manifest.get("version") or "0.0.0"
                    ),
                    content_hash=str(provenance.get("content_hash") or ""),
                    build_identity=str(provenance.get("build_identity") or ""),
                    # Provider authority is Host-attested. Pack provenance is
                    # descriptive input and cannot elevate dispatch trust.
                    trust_class=verified_pack_trust.get(pack_id, "untrusted"),
                    # Placement/Pack declarations are not runtime attestation.
                    isolation="host_unattested",
                    required_capabilities=tuple(
                        str(value)
                        for value in item.get("required_capabilities", [])
                    ),
                    instance_key=(
                        str(item.get("instance_key"))
                        if item.get("instance_key") is not None
                        else None
                    ),
                    priority=int(item.get("priority") or 0),
                    before=tuple(str(value) for value in item.get("before", [])),
                    after=tuple(str(value) for value in item.get("after", [])),
                )
            except (TypeError, ValueError) as exc:
                diagnostics.append(
                    _diagnostic(
                        "invalid_manifest",
                        "error",
                        f"Invalid global provider descriptor: {exc}",
                        pack_id,
                    )
                )
                continue
            providers.append(provider)
            requirements.setdefault(
                contract.contract_id,
                ContractRequirement(
                    contract_id=contract.contract_id,
                    version_range=f">={contract.version} <{int(contract.version.split('.')[0]) + 1}.0.0",
                    cardinality=contract.cardinality,
                    optional=contract.cardinality is Cardinality.OPTIONAL,
                    instance_key=provider.instance_key,
                ),
            )
        for item in contracts.get("requires", []):
            if not isinstance(item, Mapping):
                continue
            try:
                requirement = ContractRequirement(
                    contract_id=str(item.get("id") or ""),
                    version_range=str(item.get("version_range") or ""),
                    cardinality=Cardinality(str(item.get("cardinality"))),
                    optional=bool(item.get("optional", False)),
                    instance_key=(
                        str(item.get("instance_key"))
                        if item.get("instance_key") is not None
                        else None
                    ),
                )
                requirements.setdefault(requirement.contract_id, requirement)
            except (TypeError, ValueError) as exc:
                diagnostics.append(
                    _diagnostic(
                        "invalid_manifest",
                        "error",
                        f"Invalid global consumer requirement: {exc}",
                        pack_id,
                    )
                )
    return (
        tuple(providers),
        tuple(requirements[key] for key in sorted(requirements)),
        diagnostics,
    )


def _resolve_contracts(
    requirements: Sequence[ContractRequirement],
    providers: Sequence[ProviderDescriptor],
    effective: tuple[str, ...],
) -> tuple[tuple[ResolvedProvider, ...], list[ResolutionDiagnostic]]:
    registry = ContractRegistry()
    effective_set = set(effective)
    for provider in providers:
        if provider.source_pack_id in effective_set:
            registry.register(provider)
    selected: list[ResolvedProvider] = []
    diagnostics: list[ResolutionDiagnostic] = []
    for requirement in requirements:
        result = registry.resolve(requirement)
        if result.status is not ContractStatus.OK:
            diagnostics.append(
                _diagnostic(
                    result.status.value,
                    "warning" if requirement.optional else "error",
                    "; ".join(result.diagnostics),
                    requirement.contract_id,
                )
            )
            continue
        for provider in result.value or ():
            selected.append(
                ResolvedProvider(
                    contract_id=requirement.contract_id,
                    provider_instance_id=provider.provider_instance_id,
                    source_pack_id=provider.source_pack_id,
                    version=provider.contract.version,
                    content_hash=provider.content_hash,
                )
            )
    return (
        tuple(
            sorted(
                selected,
                key=lambda item: (item.contract_id, item.provider_instance_id),
            )
        ),
        diagnostics,
    )


def _effective_permissions(
    effective: tuple[str, ...],
    manifests: Mapping[str, Mapping[str, Any]],
    policy_capabilities: tuple[str, ...],
) -> tuple[str, ...]:
    requested: set[str] = set()
    for pack_id in effective:
        manifest = manifests[pack_id]
        capabilities = manifest.get("required_capabilities")
        if isinstance(capabilities, list):
            requested.update(str(item) for item in capabilities if str(item))
        v3_manifest = manifest.get("_v3_manifest")
        permissions = (
            v3_manifest.get("permissions")
            if isinstance(v3_manifest, Mapping)
            else None
        )
        if isinstance(permissions, list):
            requested.update(
                str(item.get("capability"))
                for item in permissions
                if isinstance(item, Mapping) and item.get("capability")
            )
    return tuple(sorted(requested & set(policy_capabilities)))


def _input_payload(
    value: ResolutionInput, manifests: Mapping[str, Mapping[str, Any]]
) -> dict[str, Any]:
    return {
        "profile_id": value.profile_id,
        "profile_revision": value.profile_revision,
        "platform": value.platform,
        "policy_revision": value.policy_revision,
        "lockfile_revision": value.lockfile_revision,
        "requested_pack_ids": value.requested_pack_ids,
        "requested_contracts": tuple(asdict(item) for item in value.requested_contracts),
        "authorized_pack_ids": value.authorized_pack_ids,
        "healthy_pack_ids": value.healthy_pack_ids,
        "policy_capabilities": value.policy_capabilities,
        "verified_pack_trust": value.verified_pack_trust,
        "manifest_hashes": tuple(
            sorted(
                (pack_id, str(manifest.get("_manifest_hash") or ""))
                for pack_id, manifest in manifests.items()
            )
        ),
    }


def _lock_identity(lockfile: ProfileLockfile) -> str:
    payload = lockfile.to_dict()
    payload.pop("lock_hash", None)
    return content_identity(payload)


def _pack_content_hash(pack_root: Path, manifest_hash: str) -> str:
    cache_key = (str(pack_root.absolute()), manifest_hash)
    files, revision = _pack_projection_revision(pack_root)
    with _PACK_CONTENT_HASH_CACHE_LOCK:
        cached = _PACK_CONTENT_HASH_CACHE.get(cache_key)
        if cached is not None and cached[0] == revision:
            return cached[1]

    resources: list[tuple[str, str]] = [("ecosystem.json", manifest_hash)]
    resources.extend(
        (path.relative_to(pack_root).as_posix(), _sha256(path))
        for path in files
    )
    content_hash = content_identity(resources)
    _, verified_revision = _pack_projection_revision(pack_root)
    if verified_revision != revision:
        raise RuntimeError("pack projection changed during content hashing")
    with _PACK_CONTENT_HASH_CACHE_LOCK:
        _PACK_CONTENT_HASH_CACHE[cache_key] = (revision, content_hash)
    return content_hash


def _pack_projection_revision(
    pack_root: Path,
) -> tuple[
    tuple[Path, ...],
    tuple[tuple[str, int, int, int, int, int, int], ...],
]:
    """Return projected files and a change-sensitive cache revision.

    Reusing an already computed digest is safe only while every projected
    file keeps the same identity, size, mode, mtime, and ctime. Directory
    additions and removals also change the ordered path set. The digest is
    recomputed whenever any of those Host-observed properties changes.
    """
    declared_directories = set(PROJECTION_TYPES)
    try:
        manifest = json.loads((pack_root / "ecosystem.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        manifest = {}
    components = manifest.get("components") if isinstance(manifest, dict) else None
    if isinstance(components, Mapping):
        for component in components.values():
            if not isinstance(component, Mapping):
                continue
            declared = str(component.get("path") or "").strip()
            declared_path = Path(declared)
            if (
                declared
                and not declared_path.is_absolute()
                and ".." not in declared_path.parts
            ):
                declared_directories.add(declared)
    files: list[Path] = []
    for directory in sorted(declared_directories):
        root = pack_root / directory
        files.extend(_bounded_pack_files(root))
    ordered_files = tuple(sorted(set(files), key=lambda path: path.as_posix()))
    revision: list[tuple[str, int, int, int, int, int, int]] = []
    for path in ordered_files:
        file_stat = path.stat(follow_symlinks=False)
        if not stat.S_ISREG(file_stat.st_mode):
            raise RuntimeError("pack projection contains a non-regular file")
        revision.append(
            (
                path.relative_to(pack_root).as_posix(),
                file_stat.st_dev,
                file_stat.st_ino,
                file_stat.st_mode,
                file_stat.st_size,
                file_stat.st_mtime_ns,
                file_stat.st_ctime_ns,
            )
        )
    return ordered_files, tuple(revision)


def _bounded_pack_files(
    root: Path,
    *,
    max_depth: int = 8,
    max_entries: int = 8192,
) -> tuple[Path, ...]:
    """List regular pack files with finite, symlink-safe traversal.

    Projection roots are manifest-controlled and may point at unexpectedly
    broad trees.  Resolution must therefore never use an unbounded recursive
    glob.  Exceeding either budget fails closed instead of producing a partial
    integrity identity.
    """

    if max_depth < 0 or max_entries < 1:
        raise RuntimeError("pack content scan budget is invalid")
    try:
        if root.is_symlink() or not root.is_dir():
            return ()
    except OSError:
        return ()

    files: list[Path] = []
    visited_entries = 0

    def walk(directory: Path, depth: int) -> None:
        nonlocal visited_entries
        try:
            with os.scandir(directory) as iterator:
                entries = sorted(iterator, key=lambda item: item.name)
        except OSError:
            return
        for entry in entries:
            visited_entries += 1
            if visited_entries > max_entries:
                raise RuntimeError(
                    f"pack content scan exceeded {max_entries} entries: {root}"
                )
            try:
                if entry.is_symlink():
                    continue
                if entry.is_file(follow_symlinks=False):
                    path = Path(entry.path)
                    if path.suffix not in _PACK_CONTENT_IGNORED_SUFFIXES:
                        files.append(path)
                elif entry.is_dir(follow_symlinks=False):
                    if entry.name in _PACK_CONTENT_IGNORED_DIRECTORY_NAMES:
                        continue
                    if depth >= max_depth:
                        raise RuntimeError(
                            f"pack content scan exceeded depth {max_depth}: {root}"
                        )
                    walk(Path(entry.path), depth + 1)
            except OSError:
                continue

    walk(root, 0)
    return tuple(files)


def _with_diagnostics(
    plan: ResolvedProfile, diagnostics: tuple[ResolutionDiagnostic, ...]
) -> ResolvedProfile:
    combined = (*plan.diagnostics, *diagnostics)
    payload = plan.to_dict()
    payload["diagnostics"] = tuple(asdict(item) for item in combined)
    payload.pop("plan_hash")
    return replace(
        plan,
        diagnostics=combined,
        plan_hash=content_identity(payload),
    )


def _legacy_selected_pack_ids(selection: Mapping[str, Any]) -> tuple[str, ...]:
    values = _string_items(selection.get("setup_pack_ids"))
    singular = str(selection.get("setup_pack_id") or "").strip()
    if singular:
        values = (*values, singular)
    return _unique(values)


def _string_items(value: Any) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        return ()
    return tuple(str(item).strip() for item in value if str(item).strip())


def _unique(values: Iterable[str]) -> tuple[str, ...]:
    return tuple(sorted({str(item).strip() for item in values if str(item).strip()}))


def _revision_of(value: Mapping[str, Any]) -> str:
    return content_identity(dict(value))


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def _diagnostic(
    code: str,
    severity: str,
    message: str,
    subject: str | None = None,
    **details: str,
) -> ResolutionDiagnostic:
    return ResolutionDiagnostic(
        code=code,
        severity=severity,
        message=message,
        subject=subject,
        details=tuple(sorted((str(key), str(value)) for key, value in details.items())),
    )
