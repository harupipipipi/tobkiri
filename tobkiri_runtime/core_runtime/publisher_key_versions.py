"""Exact-artifact additive publisher keys; Native approval is caller-owned.

This module never rotates the legacy publisher key or changes install records.
The write entry point is internal Host composition: its required verifier must
authenticate Native evidence and current Authority bindings, never a Renderer
``approved`` flag. Merely preparing a plan confers no authority.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from tobkiri_protocol.canonical import canonical_bytes, canonical_digest

DOMAIN = "tobkiri.publisher-key-version.v1"
FIELD = "publisher_key_versions"
MAX_APPROVAL_LIFETIME_MS = 300_000
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,255}\Z")
_KEY_ID = re.compile(r"[0-9a-f]{32}\Z")
_REQUEST_ID = re.compile(r"[A-Za-z0-9_-]{16,128}\Z")


def _text(value: Any, pattern: re.Pattern[str], label: str) -> str:
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise ValueError(f"invalid publisher key {label}")
    return value


def _key(pem: Any) -> tuple[Ed25519PublicKey, str, str, str]:
    if not isinstance(pem, str) or len(pem) > 1024:
        raise ValueError("invalid publisher public key")
    key = serialization.load_pem_public_key(pem.encode("ascii"))
    if not isinstance(key, Ed25519PublicKey):
        raise ValueError("publisher key must be Ed25519")
    canonical = key.public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode("ascii")
    raw = key.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    digest = hashlib.sha256(raw).hexdigest()
    return key, canonical, digest[:32], "sha256:" + digest


def _scope(pack_id: Any, artifact: Any, signed: Any) -> dict[str, str]:
    return {
        "pack_id": _text(pack_id, _ID, "Pack ID"),
        "artifact_digest": _text(artifact, _DIGEST, "artifact digest"),
        "signed_manifest_digest": _text(signed, _DIGEST, "signed manifest digest"),
    }


def _versions(policy: Mapping[str, Any]) -> dict[str, Any]:
    value = policy.get(FIELD)
    if value is None and FIELD not in policy:
        return {"schema_version": 1, "publishers": {}, "consumed_requests": []}
    from .pack_artifact_integrity import host_policy_identity

    if not host_policy_identity(policy)[1]:
        raise ValueError("publisher key versions need an authenticated policy")
    if (
        not isinstance(value, dict)
        or set(value) != {"schema_version", "publishers", "consumed_requests"}
        or type(value["schema_version"]) is not int
        or value["schema_version"] != 1
    ):
        raise ValueError("invalid publisher key versions envelope")
    publishers = value["publishers"]
    requests = value["consumed_requests"]
    if not isinstance(publishers, dict) or len(publishers) > 256:
        raise ValueError("invalid publisher key version publishers")
    if not isinstance(requests, list) or len(requests) > 10_000:
        raise ValueError("invalid publisher key approval history")
    for request in requests:
        _text(request, _REQUEST_ID, "approval request ID")
    if len(set(requests)) != len(requests):
        raise ValueError("duplicate publisher key approval request")
    for publisher_id, keys in publishers.items():
        _text(publisher_id, _ID, "publisher ID")
        if not isinstance(keys, dict) or not keys or len(keys) > 256:
            raise ValueError("invalid publisher key versions")
        for key_id, record in keys.items():
            _text(key_id, _KEY_ID, "key ID")
            if not isinstance(record, dict) or set(record) != {
                "public_key_pem",
                "key_fingerprint",
                "scopes",
            }:
                raise ValueError("invalid publisher key version record")
            _, canonical, derived, fingerprint = _key(record["public_key_pem"])
            if (
                canonical != record["public_key_pem"]
                or derived != key_id
                or (fingerprint != record["key_fingerprint"])
            ):
                raise ValueError("publisher key version identity mismatch")
            scopes = record["scopes"]
            if not isinstance(scopes, list) or not scopes or len(scopes) > 256:
                raise ValueError("invalid publisher key scopes")
            identities: set[str] = set()
            for scope in scopes:
                if (
                    not isinstance(scope, dict)
                    or set(scope) != {"pack_id", "artifact_digest", "signed_manifest_digest"}
                    or scope
                    != _scope(
                        **{
                            "pack_id": scope.get("pack_id"),
                            "artifact": scope.get("artifact_digest"),
                            "signed": scope.get("signed_manifest_digest"),
                        }
                    )
                ):
                    raise ValueError("invalid publisher key scope")
                digest = canonical_digest(scope)
                if digest in identities:
                    raise ValueError("duplicate publisher key scope")
                identities.add(digest)
    # Detach all mutable objects from the caller's snapshot.
    return json.loads(canonical_bytes(value))


def _publisher(policy: Mapping[str, Any], publisher_id: str) -> Mapping[str, Any]:
    _text(publisher_id, _ID, "publisher ID")
    publishers = policy.get("publishers")
    record = publishers.get(publisher_id) if isinstance(publishers, Mapping) else None
    if not isinstance(record, Mapping):
        raise ValueError("publisher has no legacy trust anchor")
    revoked = record.get("revoked_key_ids")
    namespaces = record.get("allowed_pack_namespaces")
    if not isinstance(revoked, list) or not isinstance(namespaces, list):
        raise ValueError("invalid legacy publisher policy")
    for key_id in revoked:
        _text(key_id, _KEY_ID, "revoked key ID")
    for namespace in namespaces:
        _text(namespace, _ID, "namespace")
    _key(record.get("public_key_pem"))
    return record


def is_legacy_publisher_key(policy: Mapping[str, Any], *, publisher_id: str, key_id: str) -> bool:
    """Determine key selection without widening legacy namespace authority."""
    _versions(policy)
    _text(key_id, _KEY_ID, "key ID")
    return _key(_publisher(policy, publisher_id)["public_key_pem"])[2] == key_id


def resolve_publisher_key(
    policy: Mapping[str, Any],
    *,
    publisher_id: str,
    key_id: str,
    pack_id: str,
    artifact_digest: str,
    signed_manifest_digest: str,
) -> Ed25519PublicKey:
    """Select the unchanged legacy key or one exact authorized artifact scope."""
    versions = _versions(policy)
    publisher = _publisher(policy, publisher_id)
    _text(key_id, _KEY_ID, "key ID")
    if key_id in publisher["revoked_key_ids"]:
        raise ValueError("publisher key is revoked")
    key, _, legacy_id, _ = _key(publisher["public_key_pem"])
    if key_id == legacy_id:
        namespaces = publisher["allowed_pack_namespaces"]
        if namespaces and not any(
            pack_id == item or pack_id.startswith(item + ".") for item in namespaces
        ):
            raise ValueError("publisher cannot sign this Pack namespace")
        return key
    scope = _scope(pack_id, artifact_digest, signed_manifest_digest)
    record = versions["publishers"].get(publisher_id, {}).get(key_id)
    if record is None or scope not in record["scopes"]:
        raise ValueError("publisher key has no exact artifact authorization")
    return _key(record["public_key_pem"])[0]


@dataclass(frozen=True)
class PublisherKeyVersionPlan:
    """Immutable canonical review plan bound to one existing policy snapshot."""

    canonical_json: bytes
    digest: str

    def document(self) -> dict[str, Any]:
        """Return a detached copy of the reviewed fields."""
        return json.loads(self.canonical_json)


def prepare_publisher_key_version(
    policy: Mapping[str, Any],
    *,
    publisher_id: str,
    public_key_pem: str,
    pack_id: str,
    artifact_digest: str,
    signed_manifest_digest: str,
    approval_context: Mapping[str, Any],
) -> PublisherKeyVersionPlan:
    """Prepare exact additive trust without writing or issuing approval."""
    from .pack_artifact_integrity import host_policy_identity

    generation, digest = host_policy_identity(policy)
    if not digest:
        raise ValueError("publisher key addition needs an authenticated policy")
    _versions(policy)
    publisher = _publisher(policy, publisher_id)
    _, canonical, key_id, fingerprint = _key(public_key_pem)
    _, _, old_id, old_fingerprint = _key(publisher["public_key_pem"])
    if key_id == old_id or key_id in publisher["revoked_key_ids"]:
        raise ValueError("publisher key addition cannot replace or revoke legacy trust")
    if not isinstance(approval_context, Mapping) or not approval_context:
        raise ValueError("publisher key approval context is required")
    context = json.loads(canonical_bytes(dict(approval_context)))
    if len(canonical_bytes(context)) > 16_384:
        raise ValueError("publisher key approval context exceeds size limit")
    document = {
        "domain": DOMAIN,
        "publisher_id": publisher_id,
        "public_key_pem": canonical,
        "key_id": key_id,
        "old_key_fingerprint": old_fingerprint,
        "new_key_fingerprint": fingerprint,
        **_scope(pack_id, artifact_digest, signed_manifest_digest),
        "policy_generation": generation,
        "policy_digest": digest,
        "approval_context": context,
    }
    return PublisherKeyVersionPlan(canonical_bytes(document), canonical_digest(document))


def write_publisher_key_version(
    trust_store_path: Path,
    *,
    plan: PublisherKeyVersionPlan,
    approval_proof: Mapping[str, Any],
    approval_verifier: Callable[[PublisherKeyVersionPlan, Mapping[str, Any]], None],
    clock_ms: Callable[[], int] = lambda: time.time_ns() // 1_000_000,
) -> tuple[int, str]:
    """Append one scope after fresh Native proof and locked policy CAS.

    ``approval_verifier`` is mandatory trusted Host code, responsible for Native
    authentication, exact Authority owner/profile/Plan/epoch/fence freshness and
    action-specific consent. It must raise on denial and return exactly None.
    No existing onboarding writer calls this function automatically.
    """
    from .pack_artifact_integrity import (
        _canonical_policy_target,
        _policy_digest,
        _read_host_policy_state,
        _write_private_policy,
        exclusive_host_policy_lock,
        host_policy_identity,
    )

    if type(plan) is not PublisherKeyVersionPlan or not callable(approval_verifier):
        raise ValueError("publisher key approval verifier and typed plan are required")
    document = plan.document()
    if (
        not isinstance(document, dict)
        or set(document)
        != {
            "domain",
            "publisher_id",
            "public_key_pem",
            "key_id",
            "old_key_fingerprint",
            "new_key_fingerprint",
            "pack_id",
            "artifact_digest",
            "signed_manifest_digest",
            "policy_generation",
            "policy_digest",
            "approval_context",
        }
        or document["domain"] != DOMAIN
        or plan.digest != canonical_digest(document)
    ):
        raise ValueError("invalid publisher key plan")
    if not isinstance(approval_proof, Mapping) or set(approval_proof) != {
        "domain",
        "request_id",
        "issued_at_ms",
        "expires_at_ms",
        "plan_digest",
        "context",
        "evidence",
    }:
        raise ValueError("invalid publisher key approval proof")
    proof = json.loads(canonical_bytes(dict(approval_proof)))
    proof_bytes = canonical_bytes(proof)
    request_id = _text(proof["request_id"], _REQUEST_ID, "approval request ID")
    if (
        proof["domain"] != DOMAIN
        or proof["plan_digest"] != plan.digest
        or (proof["context"] != document["approval_context"])
        or not isinstance(proof["evidence"], dict)
        or not proof["evidence"]
    ):
        raise ValueError("publisher key approval binding mismatch")

    def require_fresh() -> None:
        issued, expiry, now = proof["issued_at_ms"], proof["expires_at_ms"], clock_ms()
        if any(type(item) is not int for item in (issued, expiry, now)) or not (
            0 <= issued <= now < expiry <= issued + MAX_APPROVAL_LIFETIME_MS
        ):
            raise ValueError("publisher key approval is not fresh and bounded")

    require_fresh()
    target = _canonical_policy_target(trust_store_path, require_parent=True)
    with exclusive_host_policy_lock(target) as lock:
        policy, file_identity = _read_host_policy_state(
            target,
            allow_missing=False,
            policy_lock=lock,
        )
        expected_identity = host_policy_identity(policy)
        if expected_identity != (document["policy_generation"], document["policy_digest"]):
            raise ValueError("publisher key policy snapshot is stale")
        expected = prepare_publisher_key_version(
            policy,
            publisher_id=document["publisher_id"],
            public_key_pem=document["public_key_pem"],
            pack_id=document["pack_id"],
            artifact_digest=document["artifact_digest"],
            signed_manifest_digest=document["signed_manifest_digest"],
            approval_context=document["approval_context"],
        )
        if expected != plan:
            raise ValueError("publisher key plan differs from current policy")
        versions = _versions(policy)
        if request_id in versions["consumed_requests"]:
            raise ValueError("publisher key approval was already consumed")
        keys = versions["publishers"].setdefault(document["publisher_id"], {})
        record = keys.setdefault(
            document["key_id"],
            {
                "public_key_pem": document["public_key_pem"],
                "key_fingerprint": document["new_key_fingerprint"],
                "scopes": [],
            },
        )
        scope = _scope(
            document["pack_id"], document["artifact_digest"], document["signed_manifest_digest"]
        )
        if scope in record["scopes"]:
            raise ValueError("publisher key scope is already authorized")
        record["scopes"].append(scope)
        versions["consumed_requests"].append(request_id)
        policy[FIELD] = versions
        policy["policy_generation"] = expected_identity[0] + 1
        policy["policy_digest"] = _policy_digest(policy)
        _versions(policy)
        require_fresh()
        if approval_verifier(plan, proof) is not None:
            raise ValueError("publisher key verifier must return None after authentication")
        if canonical_bytes(proof) != proof_bytes:
            raise ValueError("publisher key approval proof changed during verification")
        require_fresh()
        if len(canonical_bytes(policy)) > 4 * 1024 * 1024:
            raise ValueError("publisher trust policy exceeds size limit")
        _write_private_policy(
            target,
            policy,
            expected_file_identity=file_identity,
            expected_policy_identity=expected_identity,
            policy_lock=lock,
        )
        return host_policy_identity(policy)
