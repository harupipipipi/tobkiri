from __future__ import annotations

import json
from pathlib import Path
import shutil
from concurrent.futures import ThreadPoolExecutor

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from core_runtime.pack_artifact_integrity import (
    read_host_policy_snapshot,
    verify_declared_artifacts,
    verify_host_install_binding,
    write_host_install_record,
)
from core_runtime.pack_signature import (
    PackSignatureError,
    build_signed_manifest,
    sign_manifest,
    verify_signed_pack,
)


def _signed_pack(tmp_path: Path):
    pack_root = tmp_path / "example_pack"
    pack_root.mkdir()
    (pack_root / "ecosystem.json").write_text(
        '{"id":"example_pack"}',
        encoding="utf-8",
    )
    (pack_root / "handler.py").write_text("VALUE = 1\n", encoding="utf-8")
    private_key = Ed25519PrivateKey.generate()
    manifest = build_signed_manifest(
        pack_root,
        pack_id="example_pack",
        version="1.2.3",
        publisher_id="publisher.example",
        core_compatibility=">=1.10,<2",
        contract_versions={"rumi.command": "1.0.0"},
        requested_capabilities=["network.read"],
        created_at="2026-07-24T00:00:00+00:00",
    )
    return pack_root, private_key, sign_manifest(manifest, private_key)


def test_signed_pack_verifies_without_granting_authority(tmp_path: Path) -> None:
    pack_root, private_key, manifest = _signed_pack(tmp_path)

    result = verify_signed_pack(
        pack_root,
        manifest,
        private_key.public_key(),
        expected_publisher_id="publisher.example",
    )

    assert result["verified"] is True
    assert result["authority_granted"] is False
    assert manifest["requested_capabilities"] == ["network.read"]
    assert manifest["authority_granted"] is False


def test_file_modification_and_unlisted_file_fail_verification(
    tmp_path: Path,
) -> None:
    pack_root, private_key, manifest = _signed_pack(tmp_path)
    (pack_root / "handler.py").write_text("VALUE = 2\n", encoding="utf-8")

    with pytest.raises(PackSignatureError, match="file manifest mismatch"):
        verify_signed_pack(pack_root, manifest, private_key.public_key())

    (pack_root / "handler.py").write_text("VALUE = 1\n", encoding="utf-8")
    (pack_root / "injected.py").write_text("UNLISTED = True\n", encoding="utf-8")
    with pytest.raises(PackSignatureError, match="file manifest mismatch"):
        verify_signed_pack(pack_root, manifest, private_key.public_key())


def test_symbolic_links_fail_signing(tmp_path: Path) -> None:
    pack_root = tmp_path / "linked_pack"
    pack_root.mkdir()
    target = tmp_path / "outside.py"
    target.write_text("VALUE = 1\n", encoding="utf-8")
    (pack_root / "handler.py").symlink_to(target)

    with pytest.raises(PackSignatureError, match="symbolic link"):
        build_signed_manifest(
            pack_root,
            pack_id="example.linked",
            version="1.0.0",
            publisher_id="publisher.example",
            core_compatibility=">=1.10,<2",
        )


def test_wrong_publisher_revoked_key_and_manifest_tampering_fail(
    tmp_path: Path,
) -> None:
    pack_root, private_key, manifest = _signed_pack(tmp_path)

    with pytest.raises(PackSignatureError, match="publisher identity mismatch"):
        verify_signed_pack(
            pack_root,
            manifest,
            private_key.public_key(),
            expected_publisher_id="another.publisher",
        )
    with pytest.raises(PackSignatureError, match="revoked"):
        verify_signed_pack(
            pack_root,
            manifest,
            private_key.public_key(),
            revoked_key_ids={manifest["signature"]["key_id"]},
        )

    tampered = {**manifest, "version": "9.9.9"}
    with pytest.raises(PackSignatureError, match="signature mismatch"):
        verify_signed_pack(pack_root, tampered, private_key.public_key())

    unknown_field = {**manifest, "trusted": True}
    with pytest.raises(PackSignatureError, match="fields are invalid"):
        verify_signed_pack(pack_root, unknown_field, private_key.public_key())

    with pytest.raises(PackSignatureError, match="incompatible"):
        verify_signed_pack(
            pack_root,
            manifest,
            private_key.public_key(),
            core_version="2.0.0",
        )


def test_pack_cli_signs_and_verifies_reserved_manifest_path(
    tmp_path: Path,
) -> None:
    from scripts.tobkiri_pack import main

    pack_root = tmp_path / "cli_pack"
    pack_root.mkdir()
    (pack_root / "ecosystem.json").write_text('{"id":"cli_pack"}', encoding="utf-8")
    private_key = Ed25519PrivateKey.generate()
    private_path = tmp_path / "private.pem"
    public_path = tmp_path / "public.pem"
    private_path.write_bytes(
        private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    public_path.write_bytes(
        private_key.public_key().public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
    )

    assert main(
        [
            "sign",
            str(pack_root),
            "--private-key",
            str(private_path),
            "--pack-id",
            "example.cli",
            "--version",
            "1.0.0",
            "--publisher-id",
            "publisher.cli",
            "--core-compatibility",
            ">=1.10,<2",
        ]
    ) == 0
    assert main(
        [
            "verify",
            str(pack_root),
            "--public-key",
            str(public_path),
            "--publisher-id",
            "publisher.cli",
        ]
    ) == 0


def test_declared_signature_is_enforced_during_pack_activation(
    tmp_path: Path,
    monkeypatch,
) -> None:
    pack_root, private_key, manifest = _signed_pack(tmp_path)
    signed_path = pack_root / ".tobkiri" / "signed-pack.json"
    signed_path.parent.mkdir()
    signed_path.write_text(json.dumps(manifest), encoding="utf-8")
    public_pem = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode("utf-8")
    trust_store = tmp_path / "publisher-trust.json"
    trust_store.write_text(
        json.dumps(
            {
                "publishers": {
                    "publisher.example": {
                        "public_key_pem": public_pem,
                        "revoked_key_ids": [],
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    trust_store.chmod(0o600)
    write_host_install_record(
        trust_store,
        pack_id="example_pack",
        install_path=pack_root,
        record={
            "signature_required": True,
            "publisher_id": "publisher.example",
            "key_id": manifest["signature"]["key_id"],
            "installed_version": "1.2.3",
            "signed_manifest_path": ".tobkiri/signed-pack.json",
            "contract_versions": {"rumi.command": "1.0.0"},
            "requested_capabilities": ["network.read"],
        },
    )
    monkeypatch.setenv("RUMI_PACK_PUBLISHER_TRUST_STORE", str(trust_store))
    ecosystem = {
        "id": "example_pack",
        "metadata": {
            "integrity": {"signed_manifest": ".tobkiri/signed-pack.json"}
        }
    }

    ok, diagnostics = verify_declared_artifacts(pack_root, ecosystem)

    assert ok is True
    assert diagnostics == ()
    (pack_root / "handler.py").write_text("VALUE = 99\n", encoding="utf-8")
    ok, diagnostics = verify_declared_artifacts(pack_root, ecosystem)
    assert ok is False
    assert "file manifest mismatch" in diagnostics[0]


def test_declared_signature_fails_closed_without_external_trust_store(
    tmp_path: Path,
    monkeypatch,
) -> None:
    pack_root, _, manifest = _signed_pack(tmp_path)
    signed_path = pack_root / ".tobkiri" / "signed-pack.json"
    signed_path.parent.mkdir()
    signed_path.write_text(json.dumps(manifest), encoding="utf-8")
    ecosystem = {
        "metadata": {
            "integrity": {"signed_manifest": ".tobkiri/signed-pack.json"}
        }
    }
    monkeypatch.delenv("RUMI_PACK_PUBLISHER_TRUST_STORE", raising=False)

    ok, diagnostics = verify_declared_artifacts(pack_root, ecosystem)

    assert ok is False
    assert diagnostics == (
        "signed Pack requires a configured publisher trust store",
    )


def test_host_install_record_prevents_signature_declaration_downgrade(
    tmp_path: Path,
    monkeypatch,
) -> None:
    pack_root, private_key, manifest = _signed_pack(tmp_path)
    signed_path = pack_root / ".tobkiri" / "signed-pack.json"
    signed_path.parent.mkdir()
    signed_path.write_text(json.dumps(manifest), encoding="utf-8")
    public_pem = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode("utf-8")
    trust_store = tmp_path / "publisher-trust.json"
    trust_store.write_text(
        json.dumps(
            {
                "publishers": {
                    "publisher.example": {
                        "public_key_pem": public_pem,
                        "revoked_key_ids": [],
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    trust_store.chmod(0o600)
    write_host_install_record(
        trust_store,
        pack_id="example_pack",
        install_path=pack_root,
        record={
            "signature_required": True,
            "publisher_id": "publisher.example",
            "key_id": manifest["signature"]["key_id"],
            "installed_version": "1.2.3",
            "signed_manifest_path": ".tobkiri/signed-pack.json",
            "contract_versions": {"rumi.command": "1.0.0"},
            "requested_capabilities": ["network.read"],
        },
    )
    monkeypatch.setenv("RUMI_PACK_PUBLISHER_TRUST_STORE", str(trust_store))

    ok, diagnostics = verify_declared_artifacts(pack_root, {"id": "example_pack"})
    assert ok is True
    assert diagnostics == ()

    signed_path.unlink()
    ok, diagnostics = verify_declared_artifacts(pack_root, {"id": "example_pack"})
    assert ok is False
    assert "signed Pack verification failed" in diagnostics[0]


def test_host_install_record_rejects_same_key_resigned_different_build(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Publisher policy cannot replace the exact Host-selected artifact."""

    pack_root, private_key, original_manifest = _signed_pack(tmp_path)
    signed_path = pack_root / ".tobkiri" / "signed-pack.json"
    signed_path.parent.mkdir()
    signed_path.write_text(json.dumps(original_manifest), encoding="utf-8")
    public_pem = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode("utf-8")
    trust_store = tmp_path / "publisher-trust.json"
    trust_store.write_text(
        json.dumps(
            {
                "publishers": {
                    "publisher.example": {
                        "public_key_pem": public_pem,
                        "revoked_key_ids": [],
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    trust_store.chmod(0o600)
    write_host_install_record(
        trust_store,
        pack_id="example_pack",
        install_path=pack_root,
        record={
            "signature_required": True,
            "publisher_id": "publisher.example",
            "key_id": original_manifest["signature"]["key_id"],
            "installed_version": "1.2.3",
            "signed_manifest_path": ".tobkiri/signed-pack.json",
            "contract_versions": {"rumi.command": "1.0.0"},
            "requested_capabilities": ["network.read"],
        },
    )
    monkeypatch.setenv("RUMI_PACK_PUBLISHER_TRUST_STORE", str(trust_store))

    (pack_root / "handler.py").write_text("VALUE = 2\n", encoding="utf-8")
    replacement = build_signed_manifest(
        pack_root,
        pack_id="example_pack",
        version="1.2.3",
        publisher_id="publisher.example",
        core_compatibility=">=1.10,<2",
        contract_versions={"rumi.command": "1.0.0"},
        requested_capabilities=["network.read"],
        created_at="2026-07-24T00:00:00+00:00",
    )
    signed_path.write_text(
        json.dumps(sign_manifest(replacement, private_key)),
        encoding="utf-8",
    )
    ok, diagnostics = verify_declared_artifacts(
        pack_root,
        {
            "id": "example_pack",
            "metadata": {
                "integrity": {"signed_manifest": ".tobkiri/signed-pack.json"}
            },
        },
    )

    assert ok is False
    assert "Host install record" in diagnostics[0]


def test_host_install_record_writer_is_atomic_and_complete(
    tmp_path: Path,
) -> None:
    pack_root, _private_key, manifest = _signed_pack(tmp_path)
    signed_path = pack_root / ".tobkiri" / "signed-pack.json"
    signed_path.parent.mkdir()
    signed_path.write_text(json.dumps(manifest), encoding="utf-8")
    trust_store = tmp_path / "policy" / "publisher-trust.json"
    record = {
        "signature_required": True,
        "publisher_id": "publisher.example",
        "key_id": manifest["signature"]["key_id"],
        "installed_version": "1.2.3",
        "signed_manifest_path": ".tobkiri/signed-pack.json",
        "contract_versions": {"rumi.command": "1.0.0"},
        "requested_capabilities": ["network.read"],
    }

    write_host_install_record(
        trust_store,
        pack_id="example_pack",
        install_path=pack_root,
        record=record,
    )

    stored = json.loads(trust_store.read_text(encoding="utf-8"))
    stored_record = stored["install_records"]["example_pack"]
    assert {key: stored_record[key] for key in record} == record
    assert stored_record["install_path"] == str(pack_root.resolve())
    assert stored_record["signed_manifest_digest"].startswith("sha256:")
    assert stored_record["artifact_digest"].startswith("sha256:")
    assert trust_store.stat().st_mode & 0o777 == 0o600
    assert not [
        path
        for path in trust_store.parent.glob(f".{trust_store.name}.*")
        if path.name != f".{trust_store.name}.lock"
    ]


def test_concurrent_install_records_merge_under_host_lock(tmp_path: Path) -> None:
    """Concurrent Host installs cannot erase one another's policy records."""

    first_root, private_key, manifest = _signed_pack(tmp_path)
    signed_path = first_root / ".tobkiri" / "signed-pack.json"
    signed_path.parent.mkdir()
    signed_path.write_text(json.dumps(manifest), encoding="utf-8")
    second_root = tmp_path / "second_pack"
    shutil.copytree(first_root, second_root)
    public_pem = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode("utf-8")
    trust_store = tmp_path / "policy" / "publisher-trust.json"
    trust_store.parent.mkdir(mode=0o700)
    trust_store.write_text(
        json.dumps(
            {
                "publishers": {
                    "publisher.example": {
                        "public_key_pem": public_pem,
                        "revoked_key_ids": [],
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    trust_store.chmod(0o600)
    base_record = {
        "signature_required": True,
        "publisher_id": "publisher.example",
        "key_id": manifest["signature"]["key_id"],
        "installed_version": "1.2.3",
        "signed_manifest_path": ".tobkiri/signed-pack.json",
        "contract_versions": {"rumi.command": "1.0.0"},
        "requested_capabilities": ["network.read"],
    }

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [
            executor.submit(
                write_host_install_record,
                trust_store,
                pack_id=pack_id,
                install_path=root,
                record=base_record,
            )
            for pack_id, root in (
                ("example_pack", first_root),
                ("second_pack", second_root),
            )
        ]
        for future in futures:
            future.result()

    policy = read_host_policy_snapshot(trust_store)
    assert set(policy["install_records"]) == {"example_pack", "second_pack"}
    assert policy["policy_generation"] == 2


def test_install_capture_rejects_hardlinks_and_noncanonical_names(
    tmp_path: Path,
) -> None:
    """The Host tree capture rejects aliasing and cross-platform collisions."""

    hardlink_root = tmp_path / "hardlink-pack"
    hardlink_root.mkdir()
    original = hardlink_root / "handler.py"
    original.write_text("VALUE = 1\n", encoding="utf-8")
    (hardlink_root / "alias.py").hardlink_to(original)
    trust_store = tmp_path / "policy" / "publisher-trust.json"
    developer_record = {
        "signature_required": False,
        "publisher_id": "",
        "key_id": "",
        "installed_version": "dev",
        "signed_manifest_path": "",
        "contract_versions": {},
        "requested_capabilities": [],
        "developer_mode": True,
    }
    with pytest.raises(ValueError, match="identity is unsafe"):
        write_host_install_record(
            trust_store,
            pack_id="hardlink-pack",
            install_path=hardlink_root,
            record=developer_record,
        )

    unicode_root = tmp_path / "unicode-pack"
    unicode_root.mkdir()
    (unicode_root / "e\u0301.txt").write_text("unsafe\n", encoding="utf-8")
    with pytest.raises(ValueError, match="normalized path collision"):
        write_host_install_record(
            trust_store,
            pack_id="unicode-pack",
            install_path=unicode_root,
            record=developer_record,
        )


def test_unsigned_binding_hashes_reserved_signed_manifest_path(
    tmp_path: Path,
) -> None:
    """Unsigned developer trees cannot hide bytes at the reserved manifest path."""

    pack_root = tmp_path / "developer-pack"
    reserved = pack_root / ".tobkiri" / "signed-pack.json"
    reserved.parent.mkdir(parents=True)
    reserved.write_text('{"unsigned":true}', encoding="utf-8")
    trust_store = tmp_path / "policy" / "publisher-trust.json"
    record = {
        "signature_required": False,
        "publisher_id": "",
        "key_id": "",
        "installed_version": "dev",
        "signed_manifest_path": "",
        "contract_versions": {},
        "requested_capabilities": [],
        "developer_mode": True,
    }
    write_host_install_record(
        trust_store,
        pack_id="developer-pack",
        install_path=pack_root,
        record=record,
    )
    policy = read_host_policy_snapshot(trust_store)
    install_record = policy["install_records"]["developer-pack"]
    verify_host_install_binding(pack_root, install_record)

    reserved.write_text('{"unsigned":false}', encoding="utf-8")
    with pytest.raises(ValueError, match="artifact differs"):
        verify_host_install_binding(pack_root, install_record)


def test_policy_reader_rejects_unsafe_ancestor_permissions(tmp_path: Path) -> None:
    """Every trust-store ancestor is part of the Host-owned security boundary."""

    unsafe = tmp_path / "unsafe"
    policy_dir = unsafe / "policy"
    policy_dir.mkdir(parents=True, mode=0o700)
    unsafe.chmod(0o777)
    trust_store = policy_dir / "publisher-trust.json"
    trust_store.write_text("{}", encoding="utf-8")
    trust_store.chmod(0o600)

    with pytest.raises(ValueError, match="ancestor permissions are unsafe"):
        read_host_policy_snapshot(trust_store)

    real = tmp_path / "real-policy"
    real.mkdir(mode=0o700)
    real_store = real / "publisher-trust.json"
    real_store.write_text("{}", encoding="utf-8")
    real_store.chmod(0o600)
    linked = tmp_path / "linked-policy"
    linked.symlink_to(real, target_is_directory=True)
    with pytest.raises((OSError, ValueError)):
        read_host_policy_snapshot(linked / real_store.name)


@pytest.mark.parametrize("name", ["private.pem", ".env", "runtime.sqlite3"])
def test_signing_rejects_secret_and_runtime_files(
    tmp_path: Path,
    name: str,
) -> None:
    pack_root = tmp_path / "unsafe_pack"
    pack_root.mkdir()
    (pack_root / "ecosystem.json").write_text("{}", encoding="utf-8")
    (pack_root / name).write_text("secret", encoding="utf-8")

    with pytest.raises(PackSignatureError, match="secret or runtime"):
        build_signed_manifest(
            pack_root,
            pack_id="example.unsafe",
            version="1.0.0",
            publisher_id="publisher.example",
            core_compatibility=">=1.10,<2",
        )


@pytest.mark.parametrize("mode", [0o666, 0o777, 0o4755])
def test_signing_rejects_noncanonical_file_modes(
    tmp_path: Path,
    mode: int,
) -> None:
    pack_root = tmp_path / "unsafe_mode_pack"
    pack_root.mkdir()
    handler = pack_root / "handler.py"
    handler.write_text("VALUE = 1\n", encoding="utf-8")
    handler.chmod(mode)

    with pytest.raises(PackSignatureError, match="0644 or 0755"):
        build_signed_manifest(
            pack_root,
            pack_id="example.mode",
            version="1.0.0",
            publisher_id="publisher.example",
            core_compatibility=">=1.10,<2",
        )


@pytest.mark.parametrize("name", ["CON.txt", "name.", "stream:secret"])
def test_signing_rejects_unsafe_windows_paths(
    tmp_path: Path,
    name: str,
) -> None:
    pack_root = tmp_path / "unsafe_path_pack"
    pack_root.mkdir()
    (pack_root / name).write_text("VALUE = 1\n", encoding="utf-8")

    with pytest.raises(PackSignatureError, match="Windows"):
        build_signed_manifest(
            pack_root,
            pack_id="example.path",
            version="1.0.0",
            publisher_id="publisher.example",
            core_compatibility=">=1.10,<2",
        )


def test_signing_rejects_repository_metadata(tmp_path: Path) -> None:
    pack_root = tmp_path / "git_pack"
    pack_root.mkdir()
    (pack_root / "handler.py").write_text("VALUE = 1\n", encoding="utf-8")
    (pack_root / ".git").mkdir()

    with pytest.raises(PackSignatureError, match="repository metadata"):
        build_signed_manifest(
            pack_root,
            pack_id="example.git",
            version="1.0.0",
            publisher_id="publisher.example",
            core_compatibility=">=1.10,<2",
        )
