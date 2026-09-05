"""Profile-scoped, pack-agnostic frontend contribution catalog."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping

try:
    from jsonschema import Draft202012Validator
except ImportError:  # Desktop runtime keeps third-party bootstrap minimal.
    Draft202012Validator = None  # type: ignore[assignment,misc]

from core_runtime.global_contracts.canonical import content_identity
from core_runtime.pack_artifact_integrity import verify_declared_artifacts
from core_runtime.paths import PackLocation, resolve_pack_locations
from core_runtime.resolved_profile import ResolvedProfile

CONTRIBUTION_VERSION = "rumi.ui.contribution.v1"
_PACK_QUARANTINE_CODES = {
    "frontend_manifest_invalid",
    "frontend_build_identity_missing",
    "frontend_descriptor_hash_mismatch",
    "frontend_descriptor_outside_pack",
    "frontend_module_hash_mismatch",
    "frontend_module_path_unbound",
    "frontend_pack_hash_mismatch",
    "frontend_pack_artifact_integrity_failed",
}
SCHEMA_PATH = Path(__file__).resolve().parents[4] / "schemas" / "frontend_contribution.schema.json"


@dataclass(frozen=True)
class FrontendDiagnostic:
    """One non-fatal module-resolution or quarantine diagnostic."""

    code: str
    severity: str
    message: str
    owner_pack_id: str | None = None
    contribution_id: str | None = None


@dataclass(frozen=True)
class VerifiedFrontendContribution:
    """Host-safe contribution metadata with backend-verified provenance."""

    contribution_id: str
    kind: str
    mode: str
    label: str
    description: str | None
    priority: int
    owner_pack_id: str
    owner_pack_hash: str
    build_identity: str
    resolved_profile_revision: str
    resolved_plan_hash: str
    descriptor_hash: str
    route: str | None
    region: str | None
    renderer: str | None
    action_contract: str | None
    data_source_contract: str | None
    schema: Mapping[str, Any] | None
    view: Mapping[str, Any] | None
    module: Mapping[str, Any] | None
    isolated: Mapping[str, Any] | None
    localization: Mapping[str, str]
    accessibility: Mapping[str, Any]


@dataclass(frozen=True)
class FrontendCatalog:
    """Atomic catalog consumed by the generic frontend host."""

    version: str
    profile_id: str
    profile_revision: str
    plan_hash: str
    contributions: tuple[VerifiedFrontendContribution, ...]
    diagnostics: tuple[FrontendDiagnostic, ...]
    quarantined_pack_ids: tuple[str, ...]
    catalog_hash: str

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-safe public catalog without local source paths."""
        return asdict(self)


class FrontendHostRegistry:
    """Resolve only frontend descriptors from the active effective pack set."""

    def __init__(
        self,
        plan: ResolvedProfile,
        *,
        ecosystem_dir: Path | str | None = None,
    ) -> None:
        self.plan = plan
        self.ecosystem_dir = (
            str(ecosystem_dir) if ecosystem_dir is not None else None
        )
        schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
        self._validator = (
            Draft202012Validator(schema)
            if Draft202012Validator is not None
            else None
        )

    def build_catalog(self) -> FrontendCatalog:
        """Build a deterministic catalog; one bad pack cannot abort others."""
        diagnostics: list[FrontendDiagnostic] = []
        contributions: list[VerifiedFrontendContribution] = []
        quarantined: set[str] = set()
        pack_records = {item.pack_id: item for item in self.plan.packs}
        locations = resolve_pack_locations(
            self.plan.effective_pack_set,
            self.ecosystem_dir,
        )
        for location in locations:
            pack = pack_records.get(location.pack_id)
            if pack is None:
                continue
            loaded, pack_diagnostics = self._load_pack(
                location,
                pack.content_hash,
                pack.trust_class,
            )
            diagnostics.extend(pack_diagnostics)
            if any(
                item.code in _PACK_QUARANTINE_CODES
                for item in pack_diagnostics
            ):
                quarantined.add(location.pack_id)
            contributions.extend(loaded)

        accepted_list, collision_diagnostics = _reject_collisions(contributions)
        diagnostics.extend(collision_diagnostics)
        sorted_accepted = tuple(
            sorted(
                accepted_list,
                key=lambda item: (
                    item.kind,
                    -item.priority,
                    item.contribution_id,
                    item.owner_pack_id,
                ),
            )
        )
        payload = {
            "version": CONTRIBUTION_VERSION,
            "profile_id": self.plan.profile_id,
            "profile_revision": self.plan.profile_revision,
            "plan_hash": self.plan.plan_hash,
            "contributions": tuple(asdict(item) for item in sorted_accepted),
            "diagnostics": tuple(asdict(item) for item in diagnostics),
            "quarantined_pack_ids": tuple(sorted(quarantined)),
        }
        return FrontendCatalog(
            version=CONTRIBUTION_VERSION,
            profile_id=self.plan.profile_id,
            profile_revision=self.plan.profile_revision,
            plan_hash=self.plan.plan_hash,
            contributions=sorted_accepted,
            diagnostics=tuple(diagnostics),
            quarantined_pack_ids=tuple(sorted(quarantined)),
            catalog_hash=content_identity(payload),
        )

    def _load_pack(
        self,
        location: PackLocation,
        expected_pack_hash: str,
        verified_trust_class: str,
    ) -> tuple[
        list[VerifiedFrontendContribution],
        list[FrontendDiagnostic],
    ]:
        diagnostics: list[FrontendDiagnostic] = []
        manifest = _read_json(location.ecosystem_json_path)
        if not isinstance(manifest, dict):
            return [], [
                _diagnostic(
                    "frontend_manifest_invalid",
                    "error",
                    "Pack manifest is not a JSON object",
                    location.pack_id,
                )
            ]
        provenance = manifest.get("provenance")
        provenance = provenance if isinstance(provenance, dict) else {}
        if str(provenance.get("content_hash") or "") != expected_pack_hash:
            return [], [
                _diagnostic(
                    "frontend_pack_hash_mismatch",
                    "error",
                    "Frontend pack provenance does not match the resolved plan",
                    location.pack_id,
                )
            ]
        integrity_ok, integrity_diagnostics = verify_declared_artifacts(
            location.pack_subdir,
            manifest,
        )
        if not integrity_ok:
            return [], [
                _diagnostic(
                    "frontend_pack_artifact_integrity_failed",
                    "error",
                    "Frontend pack artifacts no longer match the resolved plan: "
                    + "; ".join(integrity_diagnostics),
                    location.pack_id,
                )
            ]
        build_identity = str(provenance.get("build_identity") or "").strip()
        descriptors = _declared_descriptors(manifest, location.pack_subdir)
        if not descriptors:
            return [], []
        if not build_identity:
            diagnostics.append(
                _diagnostic(
                    "frontend_build_identity_missing",
                    "error",
                    "Frontend pack has no backend-verified build identity",
                    location.pack_id,
                )
            )
            return [], diagnostics
        contributions: list[VerifiedFrontendContribution] = []
        for descriptor_path, declared_hash in descriptors:
            contribution, item_diagnostics = self._load_descriptor(
                location,
                descriptor_path,
                declared_hash,
                expected_pack_hash,
                build_identity,
                verified_trust_class,
            )
            diagnostics.extend(item_diagnostics)
            if contribution is not None:
                contributions.append(contribution)
        return contributions, diagnostics

    def _load_descriptor(
        self,
        location: PackLocation,
        descriptor_path: Path,
        declared_hash: str,
        expected_pack_hash: str,
        build_identity: str,
        trust_class: str,
    ) -> tuple[
        VerifiedFrontendContribution | None,
        list[FrontendDiagnostic],
    ]:
        if not _is_within(descriptor_path, location.pack_subdir):
            return None, [
                _diagnostic(
                    "frontend_descriptor_outside_pack",
                    "error",
                    "Frontend descriptor escapes its owner pack",
                    location.pack_id,
                )
            ]
        try:
            raw = descriptor_path.read_bytes()
            payload = json.loads(raw)
        except (OSError, json.JSONDecodeError) as exc:
            return None, [
                _diagnostic(
                    "frontend_descriptor_unreadable",
                    "error",
                    f"Frontend descriptor cannot be read: {type(exc).__name__}",
                    location.pack_id,
                )
            ]
        actual_hash = "sha256:" + hashlib.sha256(raw).hexdigest()
        if actual_hash != declared_hash:
            return None, [
                _diagnostic(
                    "frontend_descriptor_hash_mismatch",
                    "error",
                    "Frontend descriptor hash does not match the manifest",
                    location.pack_id,
                    str(payload.get("id") or "") if isinstance(payload, dict) else None,
                )
            ]
        if self._validator is None:
            return None, [
                _diagnostic(
                    "frontend_descriptor_invalid",
                    "error",
                    "Frontend schema validation is unavailable",
                    location.pack_id,
                    str(payload.get("id") or "")
                    if isinstance(payload, dict)
                    else None,
                )
            ]
        errors = sorted(
            self._validator.iter_errors(payload),
            key=lambda item: tuple(str(part) for part in item.absolute_path),
        )
        if errors:
            return None, [
                _diagnostic(
                    "frontend_descriptor_invalid",
                    "error",
                    "; ".join(error.message for error in errors),
                    location.pack_id,
                    str(payload.get("id") or "") if isinstance(payload, dict) else None,
                )
            ]
        contribution_id = str(payload["id"])
        mode = str(payload["mode"])
        if mode == "same_origin_builtin" and trust_class != "system":
            return None, [
                _diagnostic(
                    "frontend_same_origin_not_system",
                    "error",
                    "Same-origin executable UI is restricted to system packs",
                    location.pack_id,
                    contribution_id,
                )
            ]
        if mode == "same_origin_builtin":
            module = payload.get("module") or {}
            module_path = _module_source_path(
                str(module.get("path") or ""),
                location,
            )
            if module_path is None or not module_path.is_file():
                return None, [
                    _diagnostic(
                        "frontend_module_path_unbound",
                        "error",
                        "Executable module path is not bound to its owner pack",
                        location.pack_id,
                        contribution_id,
                    )
                ]
            module_hash = "sha256:" + hashlib.sha256(
                module_path.read_bytes()
            ).hexdigest()
            if module_hash != str(module.get("content_hash")):
                return None, [
                    _diagnostic(
                        "frontend_module_hash_mismatch",
                        "error",
                        "Executable module content hash does not match",
                        location.pack_id,
                        contribution_id,
                    )
                ]
        if mode == "isolated":
            isolated_path = str((payload.get("isolated") or {}).get("path") or "")
            expected_prefix = f"/isolated/packs/{location.pack_id}/"
            if not isolated_path.startswith(expected_prefix):
                return None, [
                    _diagnostic(
                        "frontend_isolated_owner_mismatch",
                        "error",
                        "Isolated surface path is not bound to its owner pack",
                        location.pack_id,
                        contribution_id,
                    )
                ]
        return (
            VerifiedFrontendContribution(
                contribution_id=contribution_id,
                kind=str(payload["kind"]),
                mode=mode,
                label=str(payload["label"]),
                description=_optional_string(payload.get("description")),
                priority=int(payload["priority"]),
                owner_pack_id=location.pack_id,
                owner_pack_hash=expected_pack_hash,
                build_identity=build_identity,
                resolved_profile_revision=self.plan.profile_revision,
                resolved_plan_hash=self.plan.plan_hash,
                descriptor_hash=actual_hash,
                route=_optional_string(payload.get("route")),
                region=_optional_string(payload.get("region")),
                renderer=_optional_string(payload.get("renderer")),
                action_contract=_optional_string(payload.get("action_contract")),
                data_source_contract=_optional_string(
                    payload.get("data_source_contract")
                ),
                schema=_optional_mapping(payload.get("schema")),
                view=_optional_mapping(payload.get("view")),
                module=_optional_mapping(payload.get("module")),
                isolated=_optional_mapping(payload.get("isolated")),
                localization={
                    str(key): str(value)
                    for key, value in (payload.get("localization") or {}).items()
                },
                accessibility=dict(payload["accessibility"]),
            ),
            [],
        )


def build_frontend_catalog(
    plan: ResolvedProfile,
    *,
    ecosystem_dir: Path | str | None = None,
) -> FrontendCatalog:
    """Return the complete generic host catalog for one resolved plan."""
    return FrontendHostRegistry(
        plan,
        ecosystem_dir=ecosystem_dir,
    ).build_catalog()


def _declared_descriptors(
    manifest: Mapping[str, Any], pack_root: Path
) -> list[tuple[Path, str]]:
    descriptors: list[tuple[Path, str]] = []
    if manifest.get("pack_api_version") == "io.tobkiri.pack.v4":
        resources = manifest.get("artifacts")
        path_field = "path"
        digest_field = "digest"
    else:
        resources = manifest.get("resources")
        path_field = "id"
        digest_field = "content_hash"
    if not isinstance(resources, list):
        return descriptors
    for resource in resources:
        if not isinstance(resource, dict):
            continue
        if resource.get("kind") != "ui.contribution":
            continue
        resource_id = str(resource.get(path_field) or "").strip()
        content_hash = str(resource.get(digest_field) or "").strip()
        if not resource_id or not content_hash:
            continue
        descriptors.append((pack_root / resource_id, content_hash))
    return sorted(descriptors, key=lambda item: item[0].as_posix())


def _reject_collisions(
    contributions: Iterable[VerifiedFrontendContribution],
) -> tuple[
    list[VerifiedFrontendContribution],
    list[FrontendDiagnostic],
]:
    by_identity: dict[tuple[str, str], list[VerifiedFrontendContribution]] = {}
    for contribution in contributions:
        identities = [(contribution.kind, contribution.contribution_id)]
        if contribution.kind == "route" and contribution.route:
            identities.append(("route-path", contribution.route))
        for identity in identities:
            by_identity.setdefault(identity, []).append(contribution)
    rejected: set[tuple[str, str]] = set()
    diagnostics: list[FrontendDiagnostic] = []
    for identity, candidates in sorted(by_identity.items()):
        if len(candidates) < 2:
            continue
        highest = max(item.priority for item in candidates)
        winners = [item for item in candidates if item.priority == highest]
        if len(winners) == 1:
            for item in candidates:
                if item is winners[0]:
                    continue
                rejected.add((item.owner_pack_id, item.contribution_id))
                diagnostics.append(
                    _diagnostic(
                        "frontend_shadowed",
                        "warning",
                        f"Frontend identity was shadowed: {identity[0]}:{identity[1]}",
                        item.owner_pack_id,
                        item.contribution_id,
                    )
                )
            continue
        for item in candidates:
            rejected.add((item.owner_pack_id, item.contribution_id))
            diagnostics.append(
                _diagnostic(
                    "frontend_priority_tie",
                    "error",
                    f"Ambiguous frontend identity: {identity[0]}:{identity[1]}",
                    item.owner_pack_id,
                    item.contribution_id,
                )
            )
    return (
        [
            item
            for item in contributions
            if (item.owner_pack_id, item.contribution_id) not in rejected
        ],
        diagnostics,
    )


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
    except (OSError, ValueError):
        return False
    return True


def _module_source_path(
    public_path: str,
    location: PackLocation,
) -> Path | None:
    prefix = f"/static/packs/{location.pack_id}/"
    if not public_path.startswith(prefix):
        return None
    relative = Path(public_path.removeprefix(prefix))
    if ".." in relative.parts:
        return None
    candidate = location.pack_subdir / relative
    return candidate if _is_within(candidate, location.pack_subdir) else None


def _optional_string(value: Any) -> str | None:
    normalized = str(value or "").strip()
    return normalized or None


def _optional_mapping(value: Any) -> Mapping[str, Any] | None:
    return dict(value) if isinstance(value, Mapping) else None


def _diagnostic(
    code: str,
    severity: str,
    message: str,
    owner_pack_id: str | None = None,
    contribution_id: str | None = None,
) -> FrontendDiagnostic:
    return FrontendDiagnostic(
        code,
        severity,
        message,
        owner_pack_id,
        contribution_id,
    )
