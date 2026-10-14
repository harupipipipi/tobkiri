"""Additive publisher trust stays exact and preserves its legacy trust anchor.

Reader cases use public RFC 8032 test-vector keys and in-memory policies only.
They neither authenticate Native consent nor read a running Host's state.
"""

from __future__ import annotations

from copy import deepcopy
import hashlib
from typing import Any

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
import pytest

from core_runtime.pack_artifact_integrity import _policy_digest
from core_runtime.publisher_key_versions import (
    FIELD,
    is_legacy_publisher_key,
    prepare_publisher_key_version,
    resolve_publisher_key,
)


PUBLISHER = "tobkiri.repository"
GATEWAY = "rumi_ai_gateway_pack"
ADAPTER = "rumi_provider_adapters_pack"
ARTIFACT = "sha256:" + "a" * 64
SIGNED_MANIFEST = "sha256:" + "b" * 64


def _public_key(hexadecimal: str) -> tuple[str, str, str]:
    """Format a published test-vector public key without private material."""
    raw = bytes.fromhex(hexadecimal)
    key = Ed25519PublicKey.from_public_bytes(raw)
    digest = hashlib.sha256(raw).hexdigest()
    pem = key.public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode("ascii")
    return pem, digest[:32], "sha256:" + digest


OLD_PEM, OLD_ID, OLD_FINGERPRINT = _public_key(
    "d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a"
)
NEW_PEM, NEW_ID, NEW_FINGERPRINT = _public_key(
    "3d4017c3e843895a92b70aa74d1b7ebc9c982ccf2ec4968cc0cd55f12af4660c"
)


def _policy(*, additive: bool = True) -> dict[str, Any]:
    """Build separate legacy Gateway trust and one exact new Adapter scope."""
    policy: dict[str, Any] = {
        "publishers": {
            PUBLISHER: {
                "public_key_pem": OLD_PEM,
                "allowed_pack_namespaces": [GATEWAY],
                "revoked_key_ids": [],
            },
        },
        "install_records": {
            GATEWAY: {
                "publisher_id": PUBLISHER,
                "key_id": OLD_ID,
                "signature_required": True,
            },
        },
        "policy_generation": 7,
    }
    if additive:
        policy[FIELD] = {
            "schema_version": 1,
            "publishers": {
                PUBLISHER: {
                    NEW_ID: {
                        "public_key_pem": NEW_PEM,
                        "key_fingerprint": NEW_FINGERPRINT,
                        "scopes": [{
                            "pack_id": ADAPTER,
                            "artifact_digest": ARTIFACT,
                            "signed_manifest_digest": SIGNED_MANIFEST,
                        }],
                    },
                },
            },
            "consumed_requests": [],
        }
    policy["policy_digest"] = _policy_digest(policy)
    return policy


def _resolve(policy: dict[str, Any], **changes: str) -> Ed25519PublicKey:
    """Resolve the exact new scope with optional adversarial identity changes."""
    return resolve_publisher_key(policy, **{
        "publisher_id": PUBLISHER,
        "key_id": NEW_ID,
        "pack_id": ADAPTER,
        "artifact_digest": ARTIFACT,
        "signed_manifest_digest": SIGNED_MANIFEST,
        **changes,
    })


def test_additive_reader_keeps_old_gateway_key_and_policy_unchanged() -> None:
    """A new Adapter key cannot replace or widen the old Gateway namespace."""
    legacy = _policy(additive=False)
    additive = _policy()
    before = deepcopy(additive)
    legacy_arguments = {
        "publisher_id": PUBLISHER, "key_id": OLD_ID, "pack_id": GATEWAY,
        "artifact_digest": ARTIFACT, "signed_manifest_digest": SIGNED_MANIFEST,
    }
    assert resolve_publisher_key(legacy, **legacy_arguments).public_bytes_raw() == (
        resolve_publisher_key(additive, **legacy_arguments).public_bytes_raw()
    )
    assert _resolve(additive).public_bytes_raw() == bytes.fromhex(
        "3d4017c3e843895a92b70aa74d1b7ebc9c982ccf2ec4968cc0cd55f12af4660c"
    )
    assert is_legacy_publisher_key(additive, publisher_id=PUBLISHER, key_id=OLD_ID)
    assert not is_legacy_publisher_key(additive, publisher_id=PUBLISHER, key_id=NEW_ID)
    with pytest.raises(ValueError, match="namespace"):
        _resolve(additive, key_id=OLD_ID)
    with pytest.raises(ValueError, match="exact artifact authorization"):
        _resolve(additive, pack_id=GATEWAY)
    assert additive == before
    assert additive["publishers"] == legacy["publishers"]
    assert additive["install_records"] == legacy["install_records"]


@pytest.mark.parametrize("change", [
    {"pack_id": "different.pack"},
    {"artifact_digest": "sha256:" + "c" * 64},
    {"signed_manifest_digest": "sha256:" + "d" * 64},
    {"publisher_id": "different.publisher"},
    {"key_id": "e" * 32},
])
def test_new_key_cannot_be_reused_outside_exact_authorized_scope(
    change: dict[str, str],
) -> None:
    """Every component of the new key's artifact authorization is mandatory."""
    policy = _policy()
    before = deepcopy(policy)
    with pytest.raises(ValueError):
        _resolve(policy, **change)
    assert policy == before


@pytest.mark.parametrize("key_id", [OLD_ID, NEW_ID])
def test_additive_reader_honors_revocation_for_both_key_generations(
    key_id: str,
) -> None:
    """An exact additive scope does not override a revoked publisher key."""
    policy = _policy()
    policy["publishers"][PUBLISHER]["revoked_key_ids"] = [key_id]
    with pytest.raises(ValueError, match="revoked"):
        _resolve(
            policy, key_id=key_id,
            pack_id=GATEWAY if key_id == OLD_ID else ADAPTER,
        )


@pytest.mark.parametrize("change", [
    "wrong-schema", "duplicate-scope", "wrong-key-id", "wrong-fingerprint",
    "duplicate-request", "empty-keys", "unbounded-scopes", "extra-record-field",
])
def test_malformed_additive_policy_fails_closed_even_for_legacy_key(
    change: str,
) -> None:
    """Malformed additional trust must never silently fall back to legacy trust."""
    policy = _policy()
    envelope = policy[FIELD]
    keys = envelope["publishers"][PUBLISHER]
    record = keys[NEW_ID]
    if change == "wrong-schema":
        envelope["schema_version"] = True
    elif change == "duplicate-scope":
        record["scopes"].append(deepcopy(record["scopes"][0]))
    elif change == "wrong-key-id":
        keys["f" * 32] = keys.pop(NEW_ID)
    elif change == "wrong-fingerprint":
        record["key_fingerprint"] = "sha256:" + "f" * 64
    elif change == "duplicate-request":
        envelope["consumed_requests"] = ["native_request_0001"] * 2
    elif change == "empty-keys":
        envelope["publishers"][PUBLISHER] = {}
    elif change == "unbounded-scopes":
        record["scopes"] *= 257
    else:
        record["approved"] = True
    with pytest.raises(ValueError):
        _resolve(policy, key_id=OLD_ID, pack_id=GATEWAY)


def test_preparing_exact_addition_confers_no_trust_and_returns_detached_plan() -> None:
    """A review plan preserves legacy trust and cannot be mutated by its reader."""
    policy = _policy(additive=False)
    before = deepcopy(policy)
    context = {
        "owner_id": "synthetic-owner", "activation_id": "synthetic-activation",
        "security_epoch": 3, "fencing_token": 5,
    }
    plan = prepare_publisher_key_version(
        policy, publisher_id=PUBLISHER, public_key_pem=NEW_PEM,
        pack_id=ADAPTER, artifact_digest=ARTIFACT,
        signed_manifest_digest=SIGNED_MANIFEST, approval_context=context,
    )
    document = plan.document()
    assert document["old_key_fingerprint"] == OLD_FINGERPRINT
    assert document["new_key_fingerprint"] == NEW_FINGERPRINT
    assert document["policy_generation"] == 7
    assert document["policy_digest"] == before["policy_digest"]
    document["approval_context"]["security_epoch"] = 999
    assert plan.document()["approval_context"]["security_epoch"] == 3
    assert policy == before
    with pytest.raises(ValueError, match="exact artifact authorization"):
        _resolve(policy)
