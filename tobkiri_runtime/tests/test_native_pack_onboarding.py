"""Native publisher onboarding must bind an explicit key and exact preview."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from core_runtime import native_pack_onboarding
from core_runtime.pack_artifact_integrity import (
    read_host_policy_snapshot,
    write_host_install_record,
)
from tests import test_external_pack_catalog_v4 as fixture_module
from tests.qa_frontend_input_pack import PACK_ID, build_signed_qa_pack


pytestmark = pytest.mark.skipif(
    os.name == "nt",
    reason="signed Pack filesystem and Host policy descriptor chains require POSIX",
)


def _selection(monkeypatch, tmp_path: Path) -> tuple[Path, Path, Path]:
    # The fixture's policy writer uses a POSIX descriptor chain. Preview itself
    # is portable, so skip only the fixture's pre-existing policy write here.
    monkeypatch.setattr(fixture_module, "write_host_install_record", lambda *_args, **_kwargs: None)
    source, trust_store = fixture_module._signed_external_pack(tmp_path)
    publisher = json.loads(trust_store.read_text(encoding="utf-8"))["publishers"]
    key_file = tmp_path / "publisher-public.pem"
    key_file.write_text(publisher["publisher.conformance"]["public_key_pem"], encoding="utf-8")
    return source, key_file, trust_store


def test_preview_verifies_separately_selected_public_key(monkeypatch, tmp_path: Path) -> None:
    source, key_file, _policy = _selection(monkeypatch, tmp_path)
    preview = native_pack_onboarding.preview_signed_pack(source, key_file)
    assert preview["pack_id"] == fixture_module.PACK_ID
    assert preview["publisher_id"] == "publisher.conformance"
    assert preview["key_fingerprint"].startswith("sha256:")
    assert preview["preview_digest"].startswith("sha256:")

    wrong_key = Ed25519PrivateKey.generate().public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    key_file.write_bytes(wrong_key)
    with pytest.raises(ValueError, match="key id mismatch"):
        native_pack_onboarding.preview_signed_pack(source, key_file)


def test_commit_rejects_changed_preview_before_policy_write(monkeypatch, tmp_path: Path) -> None:
    source, key_file, policy = _selection(monkeypatch, tmp_path)
    writes: list[object] = []
    monkeypatch.setattr(
        native_pack_onboarding,
        "write_host_install_record",
        lambda *_args, **_kwargs: writes.append("written"),
    )
    with pytest.raises(ValueError, match="changed after native confirmation"):
        native_pack_onboarding.commit_signed_pack(
            source, key_file,
            expected_preview_digest="sha256:" + "0" * 64,
            trust_store_path=policy,
        )
    assert writes == []


def test_commit_writes_exact_policy_after_confirmation(monkeypatch, tmp_path: Path) -> None:
    source, key_file, policy = _selection(monkeypatch, tmp_path)
    preview = native_pack_onboarding.preview_signed_pack(source, key_file)
    observed: list[dict[str, object]] = []

    def write(_target, *, pack_id, install_path, record, publisher_record):
        observed.append({
            "pack_id": pack_id,
            "install_path": install_path,
            "record": record,
            "publisher_record": publisher_record,
        })

    monkeypatch.setattr(native_pack_onboarding, "write_host_install_record", write)
    monkeypatch.setattr(native_pack_onboarding, "admit_signed_external_pack", lambda *_args, **_kwargs: {
        "pack_id": preview["pack_id"],
        "artifact_digest": preview["artifact_digest"],
        "publisher_id": preview["publisher_id"],
    })
    result = native_pack_onboarding.commit_signed_pack(
        source, key_file,
        expected_preview_digest=preview["preview_digest"],
        trust_store_path=policy,
    )
    assert result["pack_id"] == preview["pack_id"]
    assert observed[0]["pack_id"] == preview["pack_id"]
    assert observed[0]["publisher_record"]["allowed_pack_namespaces"] == [preview["pack_id"]]
    assert observed[0]["record"]["signature_required"] is True


def test_host_policy_writes_publisher_and_install_together(monkeypatch, tmp_path: Path) -> None:
    source, key_file, policy = _selection(monkeypatch, tmp_path)
    preview = native_pack_onboarding.preview_signed_pack(source, key_file)
    signed = json.loads((source / ".tobkiri" / "signed-pack.json").read_text(encoding="utf-8"))
    policy.unlink()
    write_host_install_record(
        policy,
        pack_id=preview["pack_id"],
        install_path=source,
        record={
            "signature_required": True,
            "publisher_id": preview["publisher_id"],
            "key_id": preview["key_id"],
            "installed_version": preview["version"],
            "signed_manifest_path": ".tobkiri/signed-pack.json",
            "contract_versions": signed["contract_versions"],
            "requested_capabilities": signed["requested_capabilities"],
        },
        publisher_record={
            "public_key_pem": key_file.read_text(encoding="utf-8"),
            "allowed_pack_namespaces": [preview["pack_id"]],
            "revoked_key_ids": [],
        },
    )
    snapshot = read_host_policy_snapshot(policy)
    assert snapshot["publishers"][preview["publisher_id"]]["allowed_pack_namespaces"] == [preview["pack_id"]]
    assert snapshot["install_records"][preview["pack_id"]]["signature_required"] is True
    assert snapshot["policy_generation"] == 1


@pytest.mark.parametrize("shared_policy_directory", [False, True])
def test_qa_frontend_pack_onboards_through_preview_commit_and_cas(
    tmp_path: Path, monkeypatch, shared_policy_directory: bool,
) -> None:
    monkeypatch.setenv("TOBKIRI_USER_DATA", str(tmp_path / "user-data"))
    source = tmp_path / PACK_ID
    public_key = tmp_path / f"{PACK_ID}.public.pem"
    build_signed_qa_pack(source, public_key)
    trust_dir = (
        tmp_path / "user-data" / "pack_control"
        if shared_policy_directory else tmp_path / "host-policy"
    )
    trust_dir.mkdir(parents=True, mode=0o700)
    trust_store = trust_dir / "publisher-trust.json"

    preview = native_pack_onboarding.preview_signed_pack(source, public_key)
    assert preview["pack_id"] == PACK_ID
    assert preview["key_fingerprint"].startswith("sha256:")
    assert preview["requested_capabilities"] == []
    admitted = native_pack_onboarding.commit_signed_pack(
        source,
        public_key,
        expected_preview_digest=preview["preview_digest"],
        trust_store_path=trust_store,
    )
    assert admitted["state"] == "committed"
    assert admitted["pack_id"] == PACK_ID
    assert admitted["artifact_digest"] == preview["artifact_digest"]
    policy = read_host_policy_snapshot(trust_store)
    assert policy["install_records"][PACK_ID]["signature_required"] is True
