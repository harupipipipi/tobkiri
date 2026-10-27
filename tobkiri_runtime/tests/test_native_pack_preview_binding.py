"""Native admission must install only the exact reviewed signed artifact."""

from __future__ import annotations

import json
from pathlib import Path
import shutil

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from core_runtime import native_pack_onboarding as onboarding
from core_runtime.external_pack_catalog_v4 import (
    ExternalPackCatalogDenied,
    admit_signed_external_pack,
    load_external_pack_catalog,
)
from core_runtime.pack_artifact_integrity import write_host_install_record
from tobkiri_protocol.canonical import canonical_digest
from tests import test_external_pack_catalog_v4 as fixtures


def _releases(tmp_path: Path) -> tuple[Path, Path, Path, Path]:
    """Build two benign, same-key/version releases with different bytes."""
    key = Ed25519PrivateKey.generate()
    releases = []
    for name in ("reviewed", "changed"):
        directory = tmp_path / name
        directory.mkdir()
        releases.append(fixtures._signed_external_pack(
            directory, signing_key=key, runtime_suffix=f"\n# {name} build\n",
        ))
    public = tmp_path / "publisher.pem"
    public.write_bytes(key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    ))
    return releases[0][0], releases[0][1], releases[1][0], public


def test_commit_rejects_source_change_after_preview(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A second valid signature cannot replace the frozen native preview."""
    monkeypatch.setenv("TOBKIRI_USER_DATA", str(tmp_path / "user-data"))
    source, policy, changed, public = _releases(tmp_path)
    preview = onboarding.preview_signed_pack(source, public)
    policy_before = policy.read_bytes()

    def reviewed_preview(*_args: object) -> dict[str, object]:
        shutil.copytree(changed, source, dirs_exist_ok=True)
        return preview

    monkeypatch.setattr(onboarding, "preview_signed_pack", reviewed_preview)
    with pytest.raises(ValueError, match="changed after native confirmation"):
        onboarding.commit_signed_pack(
            source, public,
            expected_preview_digest=str(preview["preview_digest"]),
            trust_store_path=policy,
        )
    assert policy.read_bytes() == policy_before
    assert not load_external_pack_catalog().records


def test_policy_writer_rechecks_reviewed_signed_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The writer's own bounded read must match the reviewed envelope."""
    monkeypatch.setenv("TOBKIRI_USER_DATA", str(tmp_path / "user-data"))
    source, policy, changed, _public = _releases(tmp_path)
    reviewed = json.loads((source / ".tobkiri/signed-pack.json").read_text())
    policy_before = policy.read_bytes()
    shutil.copytree(changed, source, dirs_exist_ok=True)
    with pytest.raises(ValueError, match="changed after native confirmation"):
        write_host_install_record(
            policy,
            pack_id=str(reviewed["pack_id"]),
            install_path=source,
            record={
                "signature_required": True,
                "publisher_id": reviewed["publisher_id"],
                "key_id": reviewed["signature"]["key_id"],
                "installed_version": reviewed["version"],
                "signed_manifest_path": ".tobkiri/signed-pack.json",
                "contract_versions": reviewed["contract_versions"],
                "requested_capabilities": reviewed["requested_capabilities"],
            },
            expected_signed_manifest_digest=canonical_digest(reviewed),
        )
    assert policy.read_bytes() == policy_before


@pytest.mark.parametrize("field", ["artifact", "signed_manifest"])
def test_admission_rejects_unreviewed_identity_before_catalog_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, field: str,
) -> None:
    """Authenticated source policy alone cannot replace a reviewed identity."""
    monkeypatch.setenv("TOBKIRI_USER_DATA", str(tmp_path / "user-data"))
    source, policy, _changed, _public = _releases(tmp_path)
    arguments = {f"expected_{field}_digest": "sha256:" + "0" * 64}
    with pytest.raises(ExternalPackCatalogDenied, match="reviewed artifact"):
        admit_signed_external_pack(source, trust_store_path=policy, **arguments)
    assert not load_external_pack_catalog().records


def test_unsigned_developer_record_cannot_ignore_reviewed_signature(
    tmp_path: Path,
) -> None:
    """An optional exact-signature check cannot turn into an unsigned bypass."""
    source = tmp_path / "pack"
    source.mkdir()
    (source / "operation.py").write_text("VALUE = 1\n")
    policy = tmp_path / "publisher-trust.json"
    with pytest.raises(ValueError, match="changed after native confirmation"):
        write_host_install_record(
            policy, pack_id="test.developer", install_path=source,
            record={
                "signature_required": False, "developer_mode": True,
                "publisher_id": "", "key_id": "", "installed_version": "1.0.0",
                "signed_manifest_path": "", "contract_versions": {},
                "requested_capabilities": [],
            },
            expected_signed_manifest_digest="sha256:" + "0" * 64,
        )
    assert not policy.exists()
