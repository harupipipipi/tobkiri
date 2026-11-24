"""Verified Base/Shell/Profile composition for the v4 launch path."""

from __future__ import annotations

import base64
import binascii
import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from .canonical import canonical_digest
from .errors import ProtocolError
from .validation import validate_document


class CompositionError(ProtocolError):
    """A verified composition could not be produced safely."""


@dataclass(frozen=True)
class RuntimeProfileBinding:
    """Exact artifact and authenticated runtime-profile handoff."""

    profile_lock: Mapping[str, Any]
    shell_artifact_path: Path | None
    local_auth_protocol: str | None
    local_auth_audience: str | None


@dataclass(frozen=True)
class VerifiedCatalog:
    """A signature-verified catalog rooted at one immutable artifact tree."""

    document: Mapping[str, Any]
    artifact_root: Path

    @property
    def revision(self) -> str:
        """Return the authenticated catalog revision."""
        return str(self.document["catalog_revision"])

    def exact_base(self, pack_id: str) -> Mapping[str, Any]:
        """Return exactly one verified Base entry."""
        return _exact_entry(self.document["bases"], "pack_id", pack_id, "Base")

    def exact_shell(self, provider_id: str) -> Mapping[str, Any]:
        """Return exactly one verified Shell entry."""
        return _exact_entry(self.document["shells"], "provider_id", provider_id, "Shell")

    def exact_pack(self, pack_id: str) -> Mapping[str, Any]:
        """Return exactly one verified Pack entry."""
        matches = [item for item in self.document["packs"] if item["pack_id"] == pack_id]
        if len(matches) != 1:
            raise CompositionError(f"Pack {pack_id!r} is missing or duplicated")
        return matches[0]


def catalog_payload(document: Mapping[str, Any]) -> dict[str, Any]:
    """Return the canonical signed portion of a composition catalog."""
    return {
        "catalog_api_version": document.get("catalog_api_version"),
        "bases": document.get("bases"),
        "shells": document.get("shells"),
        "packs": document.get("packs"),
    }


def definition_revision(document: Mapping[str, Any]) -> str:
    """Compute a definition revision without its self-identifying field."""
    payload = dict(document)
    payload.pop("definition_revision", None)
    return canonical_digest(payload)


def load_verified_catalog(
    path: Path,
    *,
    artifact_root: Path,
    trusted_public_keys: Mapping[str, bytes],
) -> VerifiedCatalog:
    """Load and authenticate a v4 catalog without any fallback source."""
    if path.is_symlink() or not path.is_file():
        raise CompositionError("composition catalog must be one regular file")
    try:
        catalog = validate_document(path.read_bytes(), "composition_catalog")
    except (OSError, ProtocolError) as exc:
        raise CompositionError(f"composition catalog rejected: {exc}") from exc

    payload_digest = canonical_digest(catalog_payload(catalog))
    integrity = catalog["integrity"]
    if catalog["catalog_revision"] != payload_digest:
        raise CompositionError("catalog revision does not match its canonical payload")
    if integrity["payload_digest"] != payload_digest:
        raise CompositionError("catalog integrity digest does not match its payload")

    signature = integrity["signature"]
    public_key_bytes = trusted_public_keys.get(signature["key_id"])
    if public_key_bytes is None:
        raise CompositionError("catalog signing key is not trusted")
    try:
        signature_bytes = base64.b64decode(signature["value"], validate=True)
        Ed25519PublicKey.from_public_bytes(public_key_bytes).verify(
            signature_bytes, payload_digest.encode("ascii")
        )
    except (ValueError, binascii.Error, InvalidSignature) as exc:
        raise CompositionError("catalog signature verification failed") from exc

    root = _regular_root(artifact_root)
    _reject_duplicate_catalog_identities(catalog)
    for entry in catalog["bases"]:
        _verify_definition(entry["definition"], "Base")
    for entry in catalog["shells"]:
        _verify_definition(entry["definition"], "Shell")
    return VerifiedCatalog(document=catalog, artifact_root=root)


def compose_runtime_profile(
    profile: Mapping[str, Any],
    catalog: VerifiedCatalog,
    *,
    security_epoch: int,
) -> RuntimeProfileBinding:
    """Resolve a Profile to an immutable lock and exact local Shell artifact."""
    try:
        resolved = validate_document(profile, "profile")
    except ProtocolError as exc:
        raise CompositionError(f"profile rejected: {exc}") from exc
    if resolved["state"] != "resolved":
        raise CompositionError("only a resolved Profile can be composed")
    if resolved["catalog_revision"] != catalog.revision:
        raise CompositionError("profile references a stale catalog revision")
    if security_epoch < 1:
        raise CompositionError("security_epoch must be positive")

    base_ref = resolved["base"]
    base_entry = catalog.exact_base(base_ref["pack_id"])
    _require_approved(base_entry, "Base")
    base = base_entry["definition"]
    _require_exact_definition(base_ref, base, "Base")
    _verify_artifact(
        catalog.artifact_root,
        base_entry["relative_path"],
        base["artifact_digest"],
    )

    selected_packs: list[Mapping[str, Any]] = []
    seen_pack_ids: set[str] = set()
    for pack_ref in resolved["packs"]:
        pack_id = pack_ref["pack_id"]
        if pack_id in seen_pack_ids or pack_id == base["pack_id"]:
            raise CompositionError(f"duplicate effective Pack identity: {pack_id}")
        seen_pack_ids.add(pack_id)
        pack_entry = catalog.exact_pack(pack_id)
        _require_approved(pack_entry, "Pack")
        if pack_ref["artifact_digest"] != pack_entry["artifact_digest"]:
            raise CompositionError(f"Pack {pack_id!r} artifact digest is stale")
        _verify_artifact(
            catalog.artifact_root,
            pack_entry["relative_path"],
            pack_entry["artifact_digest"],
        )
        selected_packs.append(pack_ref)

    dependency_refs = {(item["pack_id"], item["artifact_digest"]) for item in selected_packs}
    missing_dependencies = [
        item
        for item in base["dependencies"]
        if (item["pack_id"], item["artifact_digest"]) not in dependency_refs
    ]
    if missing_dependencies:
        raise CompositionError("Base dependencies are missing from the effective set")

    shell_lock: dict[str, Any] | None = None
    shell_path: Path | None = None
    local_auth_protocol: str | None = None
    local_auth_audience: str | None = None
    shell_ref = resolved["shell"]
    if resolved["mode"] == "interactive":
        if shell_ref is None:
            raise CompositionError("interactive Profile requires one exact Shell")
        shell_entry = catalog.exact_shell(shell_ref["provider_id"])
        _require_approved(shell_entry, "Shell")
        shell = shell_entry["definition"]
        _require_exact_definition(shell_ref, shell, "Shell")
        requirements = base["shell_requirements"]
        presentation = shell["presentation"]
        if requirements["mode"] != "interactive":
            raise CompositionError("interactive Profile is incompatible with its Base")
        if presentation["family"] not in requirements["presentation_families"]:
            raise CompositionError("Shell presentation family is incompatible with Base")
        missing_capabilities = sorted(
            set(requirements["required_capabilities"]) - set(presentation["capabilities"])
        )
        if missing_capabilities:
            raise CompositionError(f"Shell lacks required capabilities: {missing_capabilities}")
        variants = [
            item
            for item in shell["launch"]["variants"]
            if item["platform"] == shell_ref["platform"]
            and item["architecture"] == shell_ref["architecture"]
        ]
        if len(variants) != 1:
            raise CompositionError("Shell platform artifact is missing or duplicated")
        variant = variants[0]
        expected_executable_digest = shell_ref.get(
            "executable_artifact_digest", shell_ref["artifact_digest"]
        )
        variant_executable_digest = variant.get(
            "entrypoint_digest", variant["artifact_digest"]
        )
        if variant_executable_digest != expected_executable_digest:
            raise CompositionError("Shell platform artifact digest is stale")
        shell_path = _verify_artifact(
            catalog.artifact_root,
            variant["relative_path"],
            variant["artifact_digest"],
        )
        shell_lock = {
            key: shell_ref[key]
            for key in (
                "provider_id",
                "pack_id",
                "artifact_digest",
                "definition_revision",
                "contract_id",
                "platform",
                "architecture",
            )
        }
        # This v5 trust field is reconstructed only from the authenticated
        # definition variant whose bytes were verified above.  A v4 Profile
        # cannot supply or invent it.
        shell_lock["executable_artifact_digest"] = variant_executable_digest
        local_auth_protocol = shell["local_auth"]["protocol"]
        local_auth_audience = shell["local_auth"]["audience"]
    elif base["shell_requirements"]["mode"] != "headless":
        raise CompositionError("headless Profile requires a headless Base")

    effective_set = [
        {
            "role": "base",
            "identity": base["pack_id"],
            "artifact_digest": base["artifact_digest"],
        }
    ]
    if shell_lock is not None:
        effective_set.append(
            {
                "role": "shell",
                "identity": shell_lock["provider_id"],
                "artifact_digest": shell_lock["artifact_digest"],
            }
        )
    effective_set.extend(
        {
            "role": "pack",
            "identity": item["pack_id"],
            "artifact_digest": item["artifact_digest"],
        }
        for item in sorted(selected_packs, key=lambda item: item["pack_id"])
    )
    content_projections = sorted(
        (dict(item) for item in resolved.get("content_projections") or []),
        key=lambda item: str(item["projection_id"]),
    )
    projection_ids = [str(item["projection_id"]) for item in content_projections]
    if len(projection_ids) != len(set(projection_ids)):
        raise CompositionError("Profile content projection IDs are duplicated")
    profile_revision = canonical_digest(resolved)
    profile_definition_digest = canonical_digest(resolved)
    requested_edges_digest = canonical_digest(resolved["requested_edges"])
    constraints_digest = canonical_digest(
        {
            "base": base["shell_requirements"],
            "packs": resolved["packs"],
            "requested_scope_templates": [
                edge["requested_scope_template"] for edge in resolved["requested_edges"]
            ],
        }
    )
    closure_digest = canonical_digest(
        {
            "effective_set": effective_set,
            "content_projections": content_projections,
        }
    )
    provenance_digest = canonical_digest(resolved["provenance"])
    application_rows = [
        item for item in resolved["packs"] if item.get("role") == "application"
    ]
    if len(application_rows) > 1:
        raise CompositionError("Profile contains multiple Application Packs")
    application = (
        {
            "pack_id": application_rows[0]["pack_id"],
            "artifact_digest": application_rows[0]["artifact_digest"],
            "definition_digest": application_rows[0]["artifact_digest"],
        }
        if application_rows
        else None
    )
    plan_digest = canonical_digest(
        {
            "profile_revision": profile_revision,
            "catalog_revision": catalog.revision,
            "effective_set": effective_set,
            "content_projections": content_projections,
        }
    )
    lock: dict[str, Any] = {
        "lock_api_version": "io.tobkiri.profile-lock.v5",
        "profile_id": resolved["profile_id"],
        "profile_revision": profile_revision,
        "profile_definition_digest": profile_definition_digest,
        "catalog_revision": catalog.revision,
        "bundle_digest": catalog.revision,
        "security_epoch": security_epoch,
        "base": {
            "pack_id": base["pack_id"],
            "artifact_digest": base["artifact_digest"],
            "definition_revision": base["definition_revision"],
        },
        "shell": shell_lock,
        "application": application,
        "effective_set": effective_set,
        "content_projections": content_projections,
        "variant_pins": [],
        "requested_edges_digest": requested_edges_digest,
        "constraints_digest": constraints_digest,
        "closure_digest": closure_digest,
        "provenance_digest": provenance_digest,
        "plan_digest": plan_digest,
        "profile_authority_snapshot_digest": resolved["profile_authority_snapshot_digest"],
    }
    lock["lock_digest"] = canonical_digest(lock)
    validate_document(lock, "profile_lock")
    return RuntimeProfileBinding(
        profile_lock=lock,
        shell_artifact_path=shell_path,
        local_auth_protocol=local_auth_protocol,
        local_auth_audience=local_auth_audience,
    )


def verify_profile_lock(lock: Mapping[str, Any]) -> dict[str, Any]:
    """Validate a serialized lock and reject any post-composition mutation."""
    try:
        verified = validate_document(lock, "profile_lock")
    except ProtocolError as exc:
        raise CompositionError(f"profile lock rejected: {exc}") from exc
    expected = dict(verified)
    actual_digest = expected.pop("lock_digest")
    if canonical_digest(expected) != actual_digest:
        raise CompositionError("profile lock digest does not match its contents")
    identities = [item["identity"] for item in verified["effective_set"]]
    if len(identities) != len(set(identities)):
        raise CompositionError("profile lock effective set contains duplicates")
    return verified


def _exact_entry(
    entries: list[Mapping[str, Any]], key: str, value: str, label: str
) -> Mapping[str, Any]:
    matches = [item for item in entries if item["definition"][key] == value]
    if len(matches) != 1:
        raise CompositionError(f"{label} {value!r} is missing or duplicated")
    return matches[0]


def _reject_duplicate_catalog_identities(catalog: Mapping[str, Any]) -> None:
    identities = [
        *(f"base:{item['definition']['pack_id']}" for item in catalog["bases"]),
        *(f"shell:{item['definition']['provider_id']}" for item in catalog["shells"]),
        *(f"pack:{item['pack_id']}" for item in catalog["packs"]),
    ]
    if len(identities) != len(set(identities)):
        raise CompositionError("composition catalog contains duplicate identities")


def _verify_definition(definition: Mapping[str, Any], label: str) -> None:
    expected = definition_revision(definition)
    if definition["definition_revision"] != expected:
        raise CompositionError(f"{label} definition revision is tampered")


def _require_approved(entry: Mapping[str, Any], label: str) -> None:
    if entry["approval_state"] != "verified":
        raise CompositionError(f"{label} artifact is not approved")


def _require_exact_definition(
    reference: Mapping[str, Any], definition: Mapping[str, Any], label: str
) -> None:
    for key in ("pack_id", "definition_revision"):
        if reference[key] != definition[key]:
            raise CompositionError(f"{label} {key} is stale or incompatible")
    if label == "Base" and reference["artifact_digest"] != definition["artifact_digest"]:
        raise CompositionError("Base artifact digest is stale")


def _regular_root(root: Path) -> Path:
    if root.is_symlink() or not root.is_dir():
        raise CompositionError("artifact root must be a real directory")
    return root.resolve(strict=True)


def _safe_artifact_path(root: Path, relative_path: str) -> Path:
    relative = Path(relative_path)
    if relative.is_absolute() or ".." in relative.parts:
        raise CompositionError("artifact path escapes the verified catalog root")
    candidate = root.joinpath(relative)
    current = root
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise CompositionError("symlinked artifact paths are forbidden")
    try:
        resolved = candidate.resolve(strict=True)
    except OSError as exc:
        raise CompositionError(f"selected artifact is missing: {relative_path}") from exc
    if not resolved.is_relative_to(root):
        raise CompositionError("artifact path escapes the verified catalog root")
    return resolved


def _verify_artifact(root: Path, relative_path: str, expected_digest: str) -> Path:
    path = _safe_artifact_path(root, relative_path)
    actual = _artifact_digest(path)
    if actual != expected_digest:
        raise CompositionError(f"selected artifact is tampered: {relative_path}")
    return path


def _artifact_digest(path: Path) -> str:
    digest = hashlib.sha256()
    if path.is_file():
        digest.update(path.read_bytes())
    elif path.is_dir():
        for item in sorted(path.rglob("*")):
            if item.is_symlink():
                raise CompositionError("artifact trees cannot contain symlinks")
            if not item.is_file():
                continue
            relative = item.relative_to(path).as_posix().encode("utf-8")
            digest.update(relative)
            digest.update(b"\0")
            digest.update(item.read_bytes())
    else:
        raise CompositionError("selected artifact is not a regular file or directory")
    return "sha256:" + digest.hexdigest()
