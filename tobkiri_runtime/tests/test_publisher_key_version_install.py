"""Install CAS consumes exact additive trust without replacing legacy policy."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from core_runtime import pack_artifact_integrity as integrity
from core_runtime.publisher_key_versions import (
    prepare_publisher_key_version,
    write_publisher_key_version,
)
from tests.test_publisher_key_versions import (
    ARTIFACT,
    CONTEXT,
    PACK,
    PUBLISHER,
    _policy,
    _proof,
    _public,
    _store,
)
from tobkiri_host import artifact_compiler
from tobkiri_protocol.canonical import canonical_digest


def _fixture(tmp_path: Path, monkeypatch, *, prior: bool = False) -> dict:
    source = tmp_path / "candidate"
    (source / ".tobkiri").mkdir(parents=True)
    (source / "payload.py").write_text("public fixture")
    signed = {
        "pack_id": PACK,
        "publisher_id": PUBLISHER,
        "signature": {"key_id": _public("new")[1], "value": "fixture"},
    }
    signed_path = source / ".tobkiri/signed-pack.json"
    signed_path.write_text(json.dumps(signed))
    policy = _policy()
    previous = {
        "signature_required": True,
        "publisher_id": PUBLISHER,
        "key_id": _public("legacy")[1],
        "installed_version": "1.0.0",
    }
    if prior:
        policy["install_records"][PACK] = previous
    policy["policy_digest"] = integrity._policy_digest(policy)
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
    identity = write_publisher_key_version(
        target,
        plan=plan,
        approval_proof=_proof(plan),
        approval_verifier=lambda *_: None,
        clock_ms=lambda: 1500,
    )
    compiled = SimpleNamespace(
        pack_id=PACK, version="1.0.1", publisher_lineage=PUBLISHER, digest=ARTIFACT
    )
    monkeypatch.setattr(
        artifact_compiler, "compile_pack_root", lambda _root: SimpleNamespace(artifact=compiled)
    )
    calls = []
    monkeypatch.setattr(
        integrity, "verify_signed_pack", lambda *args, **kwargs: calls.append((args, kwargs))
    )
    return dict(
        source=source,
        signed_path=signed_path,
        signed=signed,
        target=target,
        identity=identity,
        previous=previous if prior else {},
        compiled=compiled,
        calls=calls,
        record={
            "signature_required": True,
            "publisher_id": PUBLISHER,
            "key_id": _public("new")[1],
            "installed_version": "1.0.1",
            "signed_manifest_path": ".tobkiri/signed-pack.json",
            "contract_versions": {"contract.one": "1.0.0"},
            "requested_capabilities": ["cap.one"],
        },
    )


def _install(fixture: dict, **overrides) -> None:
    arguments = dict(
        pack_id=PACK,
        install_path=fixture["source"],
        record=fixture["record"],
        expected_install_record=fixture["previous"],
        expected_key_version_policy_identity=fixture["identity"],
    )
    arguments.update(overrides)
    integrity.write_host_install_record(fixture["target"], **arguments)


@pytest.mark.parametrize("prior", [False, True])
def test_explicit_scoped_install_preserves_other_records_and_legacy_key(
    tmp_path,
    monkeypatch,
    prior,
):
    fixture = _fixture(tmp_path, monkeypatch, prior=prior)
    before = integrity.read_host_policy_snapshot(fixture["target"])
    _install(fixture)
    after = integrity.read_host_policy_snapshot(fixture["target"])
    assert after["publishers"] == before["publishers"]
    assert after["publisher_key_versions"] == before["publisher_key_versions"]
    assert after["install_records"]["old.pack"] == before["install_records"]["old.pack"]
    assert after["install_records"][PACK]["key_id"] == _public("new")[1]
    assert after["policy_generation"] == before["policy_generation"] + 1
    assert len(fixture["calls"]) == 1
    _, reviewed = fixture["calls"][0]
    assert reviewed["expected_version"] == fixture["record"]["installed_version"]
    assert reviewed["expected_contract_versions"] == fixture["record"]["contract_versions"]
    assert reviewed["expected_capabilities"] == fixture["record"]["requested_capabilities"]
    integrity.verify_host_install_binding(
        fixture["source"], after["install_records"][PACK], fixture["signed"]
    )


@pytest.mark.parametrize(
    "attack",
    [
        "artifact",
        "key",
        "signed",
        "policy",
        "prior",
        "publisher",
        "version",
        "pack",
        "legacy",
        "absence",
        "kind_relabel",
    ],
)
def test_scope_identity_and_prior_cas_attacks_never_write(tmp_path, monkeypatch, attack):
    fixture = _fixture(tmp_path, monkeypatch, prior=attack in {"prior", "absence"})
    overrides = {}
    if attack == "artifact":
        fixture["compiled"].digest = "sha256:" + "f" * 64
    elif attack == "key":
        fixture["record"]["key_id"] = _public("other")[1]
    elif attack in {"signed", "kind_relabel"}:
        changed = copy.deepcopy(fixture["signed"])
        changed["kind" if attack == "kind_relabel" else "other"] = "normal_sandbox"
        fixture["signed_path"].write_text(json.dumps(changed))
    elif attack == "policy":
        policy = integrity.read_host_policy_snapshot(fixture["target"])
        policy["policy_generation"] += 1
        policy["policy_digest"] = integrity._policy_digest(policy)
        fixture["target"].write_text(json.dumps(policy))
    elif attack == "prior":
        overrides["expected_install_record"] = {**fixture["previous"], "key_id": "other"}
    elif attack == "publisher":
        fixture["compiled"].publisher_lineage = "other.publisher"
    elif attack == "version":
        fixture["compiled"].version = "1.0.2"
    elif attack == "pack":
        fixture["compiled"].pack_id = "other.pack"
    elif attack == "legacy":
        fixture["record"]["key_id"] = _public("legacy")[1]
    else:
        overrides["expected_install_record"] = {}
    before = fixture["target"].read_bytes()
    with pytest.raises(ValueError):
        _install(fixture, **overrides)
    assert fixture["target"].read_bytes() == before
    assert not fixture["calls"]


@pytest.mark.parametrize(
    "overrides",
    [
        {"publisher_record": {}},
        {"expected_install_record": None},
        {"expected_key_version_policy_identity": (True, "sha256:" + "a" * 64)},
        {"expected_key_version_policy_identity": [2, "sha256:" + "a" * 64]},
    ],
)
def test_explicit_install_cannot_mix_legacy_writer_or_omit_prior(tmp_path, monkeypatch, overrides):
    fixture = _fixture(tmp_path, monkeypatch)
    before = fixture["target"].read_bytes()
    with pytest.raises(ValueError):
        _install(fixture, **overrides)
    assert fixture["target"].read_bytes() == before


def test_signature_denial_and_payload_change_during_verification_never_write(tmp_path, monkeypatch):
    fixture = _fixture(tmp_path, monkeypatch)
    before = fixture["target"].read_bytes()

    def deny(*_args, **_kwargs):
        raise ValueError("signature verification denied")

    monkeypatch.setattr(integrity, "verify_signed_pack", deny)
    with pytest.raises(ValueError, match="signature verification"):
        _install(fixture)
    assert fixture["target"].read_bytes() == before

    def change(*_args, **_kwargs):
        (fixture["source"] / "payload.py").write_text("changed after review")

    monkeypatch.setattr(integrity, "verify_signed_pack", change)
    with pytest.raises(ValueError, match="changed"):
        _install(fixture)
    assert fixture["target"].read_bytes() == before


@pytest.mark.parametrize("attack", ["signature", "contracts", "capabilities", "version"])
def test_real_verifier_rejects_invalid_signature_and_record_mismatches(
    tmp_path,
    monkeypatch,
    attack,
):
    import base64

    from core_runtime.pack_signature import build_signed_manifest, verify_signed_pack

    fixture = _fixture(tmp_path, monkeypatch)
    # Build only a public unsigned document. No private key/signing operation.
    signed = build_signed_manifest(
        fixture["source"],
        pack_id=PACK,
        version="1.0.1",
        publisher_id=PUBLISHER,
        core_compatibility=">=0",
        contract_versions={"contract.one": "1.0.0"},
        requested_capabilities=["cap.one"],
    )
    signed["signature"] = {
        "algorithm": "Ed25519",
        "key_id": _public("new")[1],
        "value": base64.urlsafe_b64encode(bytes(64)).decode().rstrip("="),
    }
    if attack == "version":
        signed["version"] = "1.0.2"
    fixture["signed_path"].write_text(json.dumps(signed))
    policy = integrity.read_host_policy_snapshot(fixture["target"])
    plan = prepare_publisher_key_version(
        policy,
        publisher_id=PUBLISHER,
        public_key_pem=_public("new")[0],
        pack_id=PACK,
        artifact_digest=ARTIFACT,
        signed_manifest_digest=canonical_digest(signed),
        approval_context=CONTEXT,
    )
    proof = _proof(plan)
    proof["request_id"] = "request_000000000002"
    fixture["identity"] = write_publisher_key_version(
        fixture["target"],
        plan=plan,
        approval_proof=proof,
        approval_verifier=lambda *_: None,
        clock_ms=lambda: 1500,
    )
    if attack == "contracts":
        fixture["record"]["contract_versions"] = {"contract.one": "2.0.0"}
    elif attack == "capabilities":
        fixture["record"]["requested_capabilities"] = ["cap.other"]
    monkeypatch.setattr(integrity, "verify_signed_pack", verify_signed_pack)
    before = fixture["target"].read_bytes()
    with pytest.raises(ValueError, match="mismatch"):
        _install(fixture)
    assert fixture["target"].read_bytes() == before


def test_namespace_prefix_does_not_expand_additive_scope(tmp_path, monkeypatch):
    fixture = _fixture(tmp_path, monkeypatch)
    child = PACK + ".child"
    fixture["compiled"].pack_id = child
    before = fixture["target"].read_bytes()
    with pytest.raises(ValueError, match="exact artifact"):
        _install(fixture, pack_id=child)
    assert fixture["target"].read_bytes() == before


def test_prior_install_cannot_change_publisher_even_with_exact_record_cas(tmp_path, monkeypatch):
    fixture = _fixture(tmp_path, monkeypatch, prior=True)
    policy = integrity.read_host_policy_snapshot(fixture["target"])
    fixture["previous"]["publisher_id"] = "another.publisher"
    policy["install_records"][PACK] = fixture["previous"]
    policy["policy_digest"] = integrity._policy_digest(policy)
    fixture["target"].write_text(json.dumps(policy))
    fixture["identity"] = integrity.host_policy_identity(policy)
    before = fixture["target"].read_bytes()
    with pytest.raises(ValueError, match="inconsistent"):
        _install(fixture)
    assert fixture["target"].read_bytes() == before
