"""Exact additive trust is separate from legacy keys and install policy."""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from core_runtime.pack_artifact_integrity import (
    _policy_digest,
    read_host_policy_snapshot,
)
from core_runtime.publisher_key_versions import (
    DOMAIN,
    FIELD,
    PublisherKeyVersionPlan,
    prepare_publisher_key_version,
    resolve_publisher_key,
    write_publisher_key_version,
)
from tobkiri_protocol.canonical import canonical_bytes, canonical_digest

PUBLISHER = "tobkiri.repository"
PACK = "rumi_provider_adapters_pack"
ARTIFACT = "sha256:" + "a" * 64
SIGNED = "sha256:" + "b" * 64
CONTEXT = {
    "owner": "owner.one",
    "profile_id": "defaults",
    "epoch": 1,
    "plan_digest": "sha256:" + "c" * 64,
    "fence": "fence.one",
}


def _public(seed: str) -> tuple[str, str]:
    # Deterministic public-only bytes: no private key or signing operation.
    raw = hashlib.sha256(seed.encode()).digest()
    key = Ed25519PublicKey.from_public_bytes(raw)
    pem = key.public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    ).decode()
    return pem, hashlib.sha256(raw).hexdigest()[:32]


def _policy() -> dict:
    pem, _ = _public("legacy")
    value = {
        "publishers": {
            PUBLISHER: {
                "public_key_pem": pem,
                "allowed_pack_namespaces": ["rumi_ai_client_gateway_pack"],
                "revoked_key_ids": [],
            }
        },
        "install_records": {"old.pack": {"key_id": "unchanged", "opaque": "old"}},
        "policy_generation": 1,
    }
    value["policy_digest"] = _policy_digest(value)
    return value


def _plan(policy: dict) -> PublisherKeyVersionPlan:
    return prepare_publisher_key_version(
        policy,
        publisher_id=PUBLISHER,
        public_key_pem=_public("new")[0],
        pack_id=PACK,
        artifact_digest=ARTIFACT,
        signed_manifest_digest=SIGNED,
        approval_context=CONTEXT,
    )


def _proof(plan: PublisherKeyVersionPlan) -> dict:
    return {
        "domain": DOMAIN,
        "request_id": "request_000000000001",
        "issued_at_ms": 1000,
        "expires_at_ms": 2000,
        "plan_digest": plan.digest,
        "context": copy.deepcopy(CONTEXT),
        "evidence": {"native": "fixture"},
    }


def _store(tmp_path: Path, policy: dict) -> Path:
    target = tmp_path / "private" / "policy.json"
    target.parent.mkdir(mode=0o700)
    target.write_text(json.dumps(policy))
    target.chmod(0o600)
    return target


def _write(target: Path, plan: PublisherKeyVersionPlan, **kwargs) -> tuple:
    return write_publisher_key_version(
        target,
        plan=plan,
        approval_proof=kwargs.pop("proof", _proof(plan)),
        approval_verifier=kwargs.pop("verifier", lambda *_: None),
        clock_ms=kwargs.pop("clock", lambda: 1500),
        **kwargs,
    )


def test_addition_preserves_legacy_and_install_records_and_exact_scope(tmp_path):
    before = _policy()
    target = _store(tmp_path, before)
    plan = _plan(before)
    assert _write(target, plan)[0] == 2
    after = read_host_policy_snapshot(target)
    assert after["publishers"] == before["publishers"]
    assert after["install_records"] == before["install_records"]
    assert resolve_publisher_key(
        after,
        publisher_id=PUBLISHER,
        key_id=_public("new")[1],
        pack_id=PACK,
        artifact_digest=ARTIFACT,
        signed_manifest_digest=SIGNED,
    )
    assert resolve_publisher_key(
        after,
        publisher_id=PUBLISHER,
        key_id=_public("legacy")[1],
        pack_id="rumi_ai_client_gateway_pack",
        artifact_digest="",
        signed_manifest_digest="",
    )
    with pytest.raises(ValueError, match="namespace"):
        resolve_publisher_key(
            after,
            publisher_id=PUBLISHER,
            key_id=_public("legacy")[1],
            pack_id=PACK,
            artifact_digest=ARTIFACT,
            signed_manifest_digest=SIGNED,
        )


@pytest.mark.parametrize(
    "field,value",
    [
        ("publisher_id", "other.publisher"),
        ("key_id", _public("other")[1]),
        ("pack_id", "other.pack"),
        ("artifact_digest", "sha256:" + "d" * 64),
        ("signed_manifest_digest", "sha256:" + "e" * 64),
    ],
)
def test_additive_key_cannot_authorize_other_identity(tmp_path, field, value):
    policy = _policy()
    target = _store(tmp_path, policy)
    _write(target, _plan(policy))
    args = dict(
        publisher_id=PUBLISHER,
        key_id=_public("new")[1],
        pack_id=PACK,
        artifact_digest=ARTIFACT,
        signed_manifest_digest=SIGNED,
    )
    args[field] = value
    with pytest.raises(ValueError):
        resolve_publisher_key(read_host_policy_snapshot(target), **args)


@pytest.mark.parametrize(
    "field,value",
    [
        ("domain", "other"),
        ("plan_digest", "sha256:" + "0" * 64),
        ("context", {"owner": "other"}),
        ("evidence", {}),
        ("request_id", "short"),
        ("issued_at_ms", True),
        ("issued_at_ms", 1600),
        ("expires_at_ms", 1500),
        ("expires_at_ms", 301001),
        ("extra", True),
    ],
)
def test_invalid_proof_never_invokes_verifier_or_writes(tmp_path, field, value):
    policy = _policy()
    target = _store(tmp_path, policy)
    before = target.read_bytes()
    plan = _plan(policy)
    proof = _proof(plan)
    proof[field] = value
    called = []
    with pytest.raises(ValueError):
        _write(target, plan, proof=proof, verifier=lambda *_: called.append(True))
    assert not called and target.read_bytes() == before


def test_native_denial_and_boolean_approval_fail_closed(tmp_path):
    policy = _policy()
    target = _store(tmp_path, policy)
    before = target.read_bytes()

    def deny(*_):
        raise ValueError("Native denied")

    for verifier in (deny, lambda *_: True):
        with pytest.raises(ValueError):
            _write(target, _plan(policy), verifier=verifier)
        assert target.read_bytes() == before


def test_expiry_after_native_verification_does_not_write(tmp_path):
    policy = _policy()
    target = _store(tmp_path, policy)
    before = target.read_bytes()
    times = iter([1500, 1500, 2000])
    with pytest.raises(ValueError, match="fresh"):
        _write(target, _plan(policy), clock=lambda: next(times))
    assert target.read_bytes() == before


def test_generation_cas_and_request_replay_fail_closed(tmp_path):
    policy = _policy()
    target = _store(tmp_path, policy)
    plan = _plan(policy)
    _write(target, plan)
    before = target.read_bytes()
    with pytest.raises(ValueError, match="stale"):
        _write(target, plan)
    current = read_host_policy_snapshot(target)
    fresh = prepare_publisher_key_version(
        current,
        publisher_id=PUBLISHER,
        public_key_pem=_public("new")[0],
        pack_id=PACK,
        artifact_digest="sha256:" + "f" * 64,
        signed_manifest_digest=SIGNED,
        approval_context=CONTEXT,
    )
    with pytest.raises(ValueError, match="consumed"):
        _write(target, fresh)
    assert target.read_bytes() == before


def test_policy_replacement_during_verifier_is_caught_by_existing_cas(tmp_path):
    policy = _policy()
    target = _store(tmp_path, policy)

    def replace(*_):
        changed = copy.deepcopy(policy)
        changed["policy_generation"] = 2
        changed["policy_digest"] = _policy_digest(changed)
        target.write_text(json.dumps(changed))

    with pytest.raises(ValueError, match="compare-and-swap"):
        _write(target, _plan(policy), verifier=replace)
    assert FIELD not in read_host_policy_snapshot(target)


@pytest.mark.parametrize(
    "mutation", ["unknown", "fingerprint", "pem", "scope", "duplicate", "version", "revoked"]
)
def test_corrupt_version_schema_or_revocation_denies(tmp_path, mutation):
    policy = _policy()
    target = _store(tmp_path, policy)
    _write(target, _plan(policy))
    current = read_host_policy_snapshot(target)
    record = current[FIELD]["publishers"][PUBLISHER][_public("new")[1]]
    if mutation == "unknown":
        record["extra"] = True
    elif mutation == "fingerprint":
        record["key_fingerprint"] = "sha256:" + "0" * 64
    elif mutation == "pem":
        record["public_key_pem"] = _public("other")[0]
    elif mutation == "scope":
        record["scopes"][0]["extra"] = True
    elif mutation == "duplicate":
        record["scopes"].append(record["scopes"][0])
    elif mutation == "version":
        current[FIELD]["schema_version"] = True
    else:
        current["publishers"][PUBLISHER]["revoked_key_ids"] = [_public("new")[1]]
    current["policy_digest"] = _policy_digest(current)
    with pytest.raises(ValueError):
        resolve_publisher_key(
            current,
            publisher_id=PUBLISHER,
            key_id=_public("new")[1],
            pack_id=PACK,
            artifact_digest=ARTIFACT,
            signed_manifest_digest=SIGNED,
        )


def test_forged_plan_fields_cannot_rebind_current_policy(tmp_path):
    policy = _policy()
    target = _store(tmp_path, policy)
    document = _plan(policy).document()
    document["old_key_fingerprint"] = "sha256:" + "0" * 64
    forged = PublisherKeyVersionPlan(canonical_bytes(document), canonical_digest(document))
    with pytest.raises(ValueError, match="differs"):
        _write(target, forged)


def test_missing_legacy_anchor_and_unenveloped_policy_cannot_add_trust():
    for policy in ({}, {"publishers": {}, "policy_generation": 1}):
        with pytest.raises(ValueError):
            _plan(policy)


def test_verifier_cannot_mutate_approved_proof(tmp_path):
    policy = _policy()
    target = _store(tmp_path, policy)
    before = target.read_bytes()

    def mutate(_plan, proof):
        proof["expires_at_ms"] = 3000

    with pytest.raises(ValueError, match="changed"):
        _write(target, _plan(policy), verifier=mutate)
    assert target.read_bytes() == before


@pytest.mark.parametrize("reader", ["integrity", "catalog"])
def test_both_readers_use_compiled_digest_and_still_verify_install_binding(
    tmp_path,
    monkeypatch,
    reader,
):
    from types import SimpleNamespace

    from core_runtime import external_pack_catalog_v4 as catalog
    from core_runtime import pack_artifact_integrity as integrity
    from tobkiri_host import artifact_compiler

    source = tmp_path / "pack"
    (source / ".tobkiri").mkdir(parents=True)
    signed = {
        "pack_id": PACK,
        "publisher_id": PUBLISHER,
        "signature": {"key_id": _public("new")[1]},
    }
    (source / ".tobkiri/signed-pack.json").write_text(json.dumps(signed))
    policy = _policy()
    policy["install_records"][PACK] = {
        "signature_required": True,
        "publisher_id": PUBLISHER,
        "key_id": _public("new")[1],
        "installed_version": "1.0.1",
        "signed_manifest_path": ".tobkiri/signed-pack.json",
        "signed_manifest_digest": canonical_digest(signed),
        "artifact_digest": "install-binding-distinct-from-compiled-digest",
        "install_path": str(source),
        "contract_versions": {},
        "requested_capabilities": [],
    }
    policy["policy_digest"] = _policy_digest(policy)
    target = _store(tmp_path, policy)
    plan = prepare_publisher_key_version(
        policy,
        publisher_id=PUBLISHER,
        public_key_pem=_public("new")[0],
        pack_id=PACK,
        artifact_digest=ARTIFACT,
        signed_manifest_digest=canonical_digest(signed),
        approval_context=CONTEXT,
    )
    _write(target, plan)
    module = integrity if reader == "integrity" else catalog
    called = []

    def compile_root(root):
        assert root == source
        called.append("compile")
        return SimpleNamespace(artifact=SimpleNamespace(digest=ARTIFACT))

    def verify_signature(_root, _signed, key, **_kwargs):
        assert (
            key.public_bytes(
                serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
            ).decode()
            == _public("new")[0]
        )
        called.append("signature")

    monkeypatch.setattr(artifact_compiler, "compile_pack_root", compile_root)
    monkeypatch.setattr(catalog, "compile_pack_root", compile_root)
    monkeypatch.setattr(module, "verify_signed_pack", verify_signature)
    monkeypatch.setattr(
        module, "verify_host_install_binding", lambda *_args, **_kwargs: called.append("binding")
    )
    if reader == "integrity":
        monkeypatch.setenv("RUMI_PACK_PUBLISHER_TRUST_STORE", str(target))
        monkeypatch.setattr(integrity, "_is_host_bundled_pack", lambda *_: False)
        assert (
            integrity._verify_declared_publisher_signature(
                source,
                {},
                {"pack": {"id": PACK, "version": "1.0.1"}},
            )
            == ()
        )
    else:
        catalog._verify_signed_source(source, target)
    assert called == ["compile", "signature", "binding"]


def test_existing_single_key_writer_still_rejects_key_replacement(tmp_path):
    from core_runtime.pack_artifact_integrity import write_host_install_record

    source = tmp_path / "pack"
    (source / ".tobkiri").mkdir(parents=True)
    (source / ".tobkiri/signed-pack.json").write_text("{}")
    policy = _policy()
    target = _store(tmp_path, policy)
    before = target.read_bytes()
    with pytest.raises(ValueError, match="already bound to another key"):
        write_host_install_record(
            target,
            pack_id=PACK,
            install_path=source,
            record={
                "signature_required": True,
                "publisher_id": PUBLISHER,
                "key_id": _public("new")[1],
                "installed_version": "1.0.1",
                "signed_manifest_path": ".tobkiri/signed-pack.json",
                "contract_versions": {},
                "requested_capabilities": [],
            },
            publisher_record={
                "public_key_pem": _public("new")[0],
                "allowed_pack_namespaces": [PACK],
                "revoked_key_ids": [],
            },
        )
    assert target.read_bytes() == before


def test_same_generation_different_policy_digest_is_stale(tmp_path):
    policy = _policy()
    target = _store(tmp_path, policy)
    plan = _plan(policy)
    policy["install_records"]["another.pack"] = {"unchanged": "other"}
    policy["policy_digest"] = _policy_digest(policy)
    target.write_text(json.dumps(policy))
    before = target.read_bytes()
    with pytest.raises(ValueError, match="stale"):
        _write(target, plan)
    assert target.read_bytes() == before


def test_each_additional_scope_needs_new_bound_approval(tmp_path):
    policy = _policy()
    target = _store(tmp_path, policy)
    _write(target, _plan(policy))
    current = read_host_policy_snapshot(target)
    plan = prepare_publisher_key_version(
        current,
        publisher_id=PUBLISHER,
        public_key_pem=_public("new")[0],
        pack_id=PACK,
        artifact_digest="sha256:" + "f" * 64,
        signed_manifest_digest=SIGNED,
        approval_context=CONTEXT,
    )
    proof = _proof(plan)
    proof["request_id"] = "request_000000000002"
    _write(target, plan, proof=proof)
    after = read_host_policy_snapshot(target)
    assert len(after[FIELD]["publishers"][PUBLISHER][_public("new")[1]]["scopes"]) == 2
    assert after["publishers"] == policy["publishers"]
    assert after["install_records"] == policy["install_records"]


def test_additive_records_without_authenticated_policy_envelope_deny(tmp_path):
    policy = _policy()
    target = _store(tmp_path, policy)
    _write(target, _plan(policy))
    current = read_host_policy_snapshot(target)
    del current["policy_generation"]
    del current["policy_digest"]
    with pytest.raises(ValueError, match="authenticated"):
        resolve_publisher_key(
            current,
            publisher_id=PUBLISHER,
            key_id=_public("new")[1],
            pack_id=PACK,
            artifact_digest=ARTIFACT,
            signed_manifest_digest=SIGNED,
        )
