"""Signed external Normal Pack admission and Profile transaction regressions."""

from __future__ import annotations

import json
from pathlib import Path
import shutil
import hashlib

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
import pytest

from core_runtime.bootstrap.profile_capture import (
    capture_default_profile,
    prepare_default_profile_confirmation,
)
from core_runtime.pack_boundary import load_pack_catalog
from core_runtime.pack_artifact_integrity import write_host_install_record
import core_runtime.pack_artifact_integrity as pack_integrity
from core_runtime.pack_control_v4 import (
    PACK_CONTROL_CONTRACT,
    capture_pack_control_session,
)
from core_runtime.external_pack_catalog_v4 import (
    ExternalPackCatalogDenied,
    admit_signed_external_pack,
    load_admitted_pack_catalog,
    load_external_pack_catalog,
)
from core_runtime.pack_signature import build_signed_manifest, sign_manifest
from tobkiri_protocol.canonical import canonical_digest


RUNTIME_ROOT = Path(__file__).resolve().parents[1]
FIXTURE = RUNTIME_ROOT / "tests" / "fixtures" / "conformance_minimal_echo_pack"
PACK_ID = "conformance.minimal.echo"
CONTRACT_ID = "io.tobkiri.conformance.echo.v1"
OPERATION_ID = "echo"


def _capture_control_session(**kwargs):
    """Compose the Defaultspack runtime surface explicitly for direct tests."""

    from ecosystem.defaultspack.domain.runtime_surface_v4 import (
        create_runtime_surface_services,
    )

    return capture_pack_control_session(
        runtime_surface_factory=create_runtime_surface_services,
        **kwargs,
    )


def _write_json(path: Path, value: object) -> None:
    path.write_text(
        json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )


def _file_digest(path: Path) -> str:
    """Return the exact digest used by the Pack v4 artifact metadata."""

    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def _refresh_fixture_artifacts(root: Path) -> None:
    """Rebuild the finite v4 authority chain after a fixture mutation."""

    manifest_path = root / "pack.v4.json"
    executable_path = root / "executables.v4.json"
    index_path = root / "artifact-index.v4.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    executable = json.loads(executable_path.read_text(encoding="utf-8"))
    runtime_digest = _file_digest(root / "runtime" / "echo.py")
    for function in manifest["functions"]:
        function["implementation_digest"] = runtime_digest
    for variant in executable["variants"]:
        variant["implementation_digest"] = runtime_digest
    executable["catalog_digest"] = canonical_digest(
        {key: value for key, value in executable.items() if key != "catalog_digest"}
    )
    _write_json(executable_path, executable)

    for artifact in manifest["artifacts"]:
        artifact["digest"] = _file_digest(root / artifact["path"])
    artifact_digest = canonical_digest(manifest["artifacts"])
    manifest["pack"]["artifact_digest"] = artifact_digest
    manifest["integrity"]["artifact_set_digest"] = artifact_digest
    _write_json(manifest_path, manifest)

    index = json.loads(index_path.read_text(encoding="utf-8"))
    for artifact in index["artifacts"]:
        artifact["digest"] = _file_digest(root / artifact["path"])
    index["artifact_set_digest"] = artifact_digest
    unsigned = {key: value for key, value in index.items() if key != "integrity_seal"}
    index["integrity_seal"]["signed_digest"] = canonical_digest(unsigned)
    _write_json(index_path, index)


def _signed_external_pack(
    tmp_path: Path,
    *,
    kind: str | None = None,
    runtime_suffix: str | None = None,
    materialization_catalog_digest: str | None = None,
) -> tuple[Path, Path]:
    source = tmp_path / PACK_ID
    shutil.copytree(FIXTURE, source)
    if kind is not None:
        pack_manifest_path = source / "pack.v4.json"
        pack_manifest = json.loads(pack_manifest_path.read_text(encoding="utf-8"))
        pack_manifest["pack"]["kind"] = kind
        _write_json(pack_manifest_path, pack_manifest)
        executable_path = source / "executables.v4.json"
        executable = json.loads(executable_path.read_text(encoding="utf-8"))
        for variant in executable["variants"]:
            variant["execution_kind"] = "host_extension"
        _write_json(executable_path, executable)
    if runtime_suffix is not None:
        runtime = source / "runtime" / "echo.py"
        runtime.write_text(
            runtime.read_text(encoding="utf-8") + runtime_suffix,
            encoding="utf-8",
        )
    if materialization_catalog_digest is not None:
        executable_path = source / "executables.v4.json"
        executable = json.loads(executable_path.read_text(encoding="utf-8"))
        executable["materialization_catalog_digest"] = materialization_catalog_digest
        _write_json(executable_path, executable)
    if (
        kind is not None
        or runtime_suffix is not None
        or materialization_catalog_digest is not None
    ):
        _refresh_fixture_artifacts(source)
    private_key = Ed25519PrivateKey.generate()
    manifest = build_signed_manifest(
        source,
        pack_id=PACK_ID,
        version="1.0.0",
        publisher_id="publisher.conformance",
        core_compatibility=">=0",
        contract_versions={CONTRACT_ID: "1.0.0"},
        requested_capabilities=[],
    )
    signed = sign_manifest(manifest, private_key)
    signed_path = source / ".tobkiri" / "signed-pack.json"
    signed_path.parent.mkdir(mode=0o700)
    _write_json(signed_path, signed)
    public_pem = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode("utf-8")
    trust_dir = tmp_path / "host-policy"
    trust_dir.mkdir(mode=0o700)
    trust_store = trust_dir / "publisher-trust.json"
    _write_json(
        trust_store,
        {
            "publishers": {
                "publisher.conformance": {
                    "public_key_pem": public_pem,
                    "allowed_pack_namespaces": ["conformance"],
                    "revoked_key_ids": [],
                }
            },
        },
    )
    trust_store.chmod(0o600)
    write_host_install_record(
        trust_store,
        pack_id=PACK_ID,
        install_path=source,
        record={
            "signature_required": True,
            "publisher_id": "publisher.conformance",
            "key_id": signed["signature"]["key_id"],
            "installed_version": "1.0.0",
            "signed_manifest_path": ".tobkiri/signed-pack.json",
            "contract_versions": {CONTRACT_ID: "1.0.0"},
            "requested_capabilities": [],
        },
    )
    return source, trust_store


def _invoke(session, operation: str, payload: dict | None = None) -> dict:
    return dict(
        session.invoke(
            PACK_CONTROL_CONTRACT,
            operation,
            {**(payload or {}), "_session_id": "external-pack-session"},
        )
    )


def test_signed_external_pack_install_approve_enable_creates_profile_revision(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user_data = tmp_path / "user-data"
    monkeypatch.setenv("TOBKIRI_USER_DATA", str(user_data))
    source, trust_store = _signed_external_pack(tmp_path)
    assert PACK_ID not in load_pack_catalog()
    committed = admit_signed_external_pack(
        source,
        trust_store_path=trust_store,
    )
    assert committed["state"] == "committed"
    assert committed["pack_id"] == PACK_ID
    assert load_admitted_pack_catalog()[PACK_ID]["authority"] == "host-signed-external-normal-v4"

    initial = capture_default_profile(
        confirmation=prepare_default_profile_confirmation()
    )
    initial_revision = canonical_digest(initial.resolved.profile)
    session = _capture_control_session()
    catalog = _invoke(session, "catalog.read")
    assert catalog["count"] == len(load_pack_catalog()) + 1
    external = next(item for item in catalog["packs"] if item["pack_id"] == PACK_ID)
    assert external["installed"] is False
    assert external["approved"] is False
    assert external["enabled"] is False

    assert _invoke(session, "pack.install", {"pack_id": PACK_ID})["installed"]
    assert _invoke(session, "pack.status", {"pack_id": PACK_ID})["enabled"] is False
    candidate = _invoke(session, "approval.candidate", {"pack_id": PACK_ID})
    approved = _invoke(
        session,
        "approval.approve",
        {"pack_id": PACK_ID, "candidate_id": candidate["candidate_id"]},
    )
    assert approved["approved"] is True
    assert _invoke(session, "pack.status", {"pack_id": PACK_ID})["enabled"] is False
    assert _invoke(session, "pack.enable", {"pack_id": PACK_ID})["enabled"] is True

    active = capture_default_profile()
    enabled_profile = json.loads(json.dumps(active.resolved.profile))
    assert canonical_digest(active.resolved.profile) != initial_revision
    assert any(
        item["pack_id"] == PACK_ID for item in active.resolved.profile["packs"]
    )
    assert any(
        item["contract_id"] == CONTRACT_ID and item["operation_id"] == OPERATION_ID
        for item in active.resolved.plan["bindings"]
    )
    restarted = _capture_control_session()
    assert _invoke(restarted, "pack.status", {"pack_id": PACK_ID})["enabled"] is True
    assert _invoke(restarted, "pack.disable", {"pack_id": PACK_ID})["enabled"] is False
    disabled = capture_default_profile()
    assert all(item["pack_id"] != PACK_ID for item in disabled.resolved.profile["packs"])
    assert active.resolved.profile == enabled_profile
    assert _invoke(restarted, "pack.enable", {"pack_id": PACK_ID})["enabled"] is True
    revoked = _invoke(restarted, "approval.revoke", {"pack_id": PACK_ID})
    assert revoked["approval_status"] == "revoked"
    assert revoked["enabled"] is False
    after_revoke = _capture_control_session()
    status = _invoke(after_revoke, "pack.status", {"pack_id": PACK_ID})
    assert status["approved"] is False
    assert status["enabled"] is False
    with pytest.raises(Exception, match="approval_revoked"):
        _invoke(after_revoke, "pack.enable", {"pack_id": PACK_ID})


def test_unsigned_wrong_digest_and_symlink_sources_fail_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TOBKIRI_USER_DATA", str(tmp_path / "user-data"))
    source, trust_store = _signed_external_pack(tmp_path)
    signed_path = source / ".tobkiri" / "signed-pack.json"
    signed_bytes = signed_path.read_bytes()
    signed_path.unlink()
    with pytest.raises(ExternalPackCatalogDenied):
        admit_signed_external_pack(source, trust_store_path=trust_store)
    signed_path.parent.mkdir(exist_ok=True)
    signed_path.write_bytes(signed_bytes)
    runtime = source / "runtime" / "echo.py"
    runtime.write_text("def echo(value):\n    return {'tampered': value}\n", encoding="utf-8")
    with pytest.raises(Exception, match="manifest|digest"):
        admit_signed_external_pack(source, trust_store_path=trust_store)

    target = tmp_path / "real-source"
    source.rename(target)
    source.symlink_to(target, target_is_directory=True)
    with pytest.raises(ExternalPackCatalogDenied, match="real directory"):
        admit_signed_external_pack(source, trust_store_path=trust_store)


@pytest.mark.parametrize("relocate", ["copy", "rename"])
def test_host_install_record_rejects_relocated_signed_source(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    relocate: str,
) -> None:
    """A valid publisher signature cannot authorize an unselected path."""

    monkeypatch.setenv("TOBKIRI_USER_DATA", str(tmp_path / "user-data"))
    source, trust_store = _signed_external_pack(tmp_path)
    relocated = tmp_path / "relocated" / PACK_ID
    relocated.parent.mkdir()
    if relocate == "copy":
        shutil.copytree(source, relocated)
    else:
        source.rename(relocated)

    with pytest.raises(ExternalPackCatalogDenied, match="Host install binding"):
        admit_signed_external_pack(relocated, trust_store_path=trust_store)


def test_signed_external_pack_cannot_claim_bundle_materialization_alias(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An external Pack cannot impersonate a sealed bundle projection."""

    monkeypatch.setenv("TOBKIRI_USER_DATA", str(tmp_path / "user-data"))
    source, trust_store = _signed_external_pack(
        tmp_path,
        materialization_catalog_digest="sha256:" + "a" * 64,
    )
    with pytest.raises(
        ExternalPackCatalogDenied,
        match="cannot replace its executable catalog identity",
    ):
        admit_signed_external_pack(source, trust_store_path=trust_store)


def test_catalog_poison_cas_tamper_and_partial_transaction_are_invisible(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user_data = tmp_path / "user-data"
    monkeypatch.setenv("TOBKIRI_USER_DATA", str(user_data))
    source, trust_store = _signed_external_pack(tmp_path)

    def fail_after_promotion(point: str) -> None:
        if point == "promoted":
            raise OSError("injected transaction failure")

    with pytest.raises(OSError, match="injected"):
        admit_signed_external_pack(
            source,
            trust_store_path=trust_store,
            fault_injector=fail_after_promotion,
        )
    assert PACK_ID not in load_external_pack_catalog().records

    entry = admit_signed_external_pack(source, trust_store_path=trust_store)
    snapshot = load_external_pack_catalog()
    root = snapshot.roots[PACK_ID]
    runtime = root / "runtime" / "echo.py"
    runtime.chmod(0o600)
    runtime.write_text("tampered = True\n", encoding="utf-8")
    runtime.chmod(0o400)
    with pytest.raises(ExternalPackCatalogDenied, match="digest"):
        load_external_pack_catalog()

    # Restore from the verified source so the catalog authentication test is
    # isolated from content tamper detection.
    runtime.chmod(0o600)
    runtime.write_bytes((source / "runtime" / "echo.py").read_bytes())
    runtime.chmod(0o400)
    assert load_external_pack_catalog().entries[PACK_ID]["artifact_digest"] == entry[
        "artifact_digest"
    ]
    catalog_path = (
        user_data / "pack_control" / "external_normal_pack_catalog.v4.json"
    )
    poisoned = json.loads(catalog_path.read_text(encoding="utf-8"))
    poisoned["entries"][PACK_ID]["catalog_record"]["display_name"] = "Poisoned"
    _write_json(catalog_path, poisoned)
    catalog_path.chmod(0o600)
    with pytest.raises(ExternalPackCatalogDenied, match="authentication"):
        load_external_pack_catalog()


def test_revocation_during_admission_cannot_commit_stale_trust(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Admission rechecks the captured trust generation immediately before commit."""

    user_data = tmp_path / "user-data"
    monkeypatch.setenv("TOBKIRI_USER_DATA", str(user_data))
    source, trust_store = _signed_external_pack(tmp_path)
    key_id = json.loads(
        (source / ".tobkiri" / "signed-pack.json").read_text(encoding="utf-8")
    )["signature"]["key_id"]

    def revoke_after_promotion(point: str) -> None:
        if point != "promoted":
            return
        policy = json.loads(trust_store.read_text(encoding="utf-8"))
        policy["publishers"]["publisher.conformance"]["revoked_key_ids"] = [
            key_id
        ]
        policy["policy_generation"] += 1
        policy["policy_digest"] = pack_integrity._policy_digest(policy)
        trust_store.write_text(json.dumps(policy), encoding="utf-8")
        trust_store.chmod(0o600)

    with pytest.raises(
        ExternalPackCatalogDenied,
        match="trust policy changed during admission",
    ):
        admit_signed_external_pack(
            source,
            trust_store_path=trust_store,
            fault_injector=revoke_after_promotion,
        )
    assert PACK_ID not in load_external_pack_catalog().records


def test_read_only_cas_rejects_pack_root_swap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TOBKIRI_USER_DATA", str(tmp_path / "user-data"))
    source, trust_store = _signed_external_pack(tmp_path)
    admit_signed_external_pack(source, trust_store_path=trust_store)
    snapshot = load_external_pack_catalog()
    root = snapshot.roots[PACK_ID]
    parent = root.parent
    parent.chmod(0o700)
    root.chmod(0o700)
    replacement = parent / f"{PACK_ID}.replacement"
    shutil.copytree(source, replacement)
    original = parent / f"{PACK_ID}.original"
    root.rename(original)
    root.symlink_to(original, target_is_directory=True)
    with pytest.raises(ExternalPackCatalogDenied, match="symlink"):
        load_external_pack_catalog()
    root.unlink()
    replacement.rename(root)
    with pytest.raises(ExternalPackCatalogDenied, match="content|identity|digest"):
        load_external_pack_catalog()


def test_admission_is_idempotent_but_pack_id_digest_rebinding_is_denied(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TOBKIRI_USER_DATA", str(tmp_path / "user-data"))
    source, trust_store = _signed_external_pack(tmp_path)
    first = admit_signed_external_pack(source, trust_store_path=trust_store)
    second = admit_signed_external_pack(source, trust_store_path=trust_store)
    assert second == first
    snapshot = load_external_pack_catalog()
    assert len(snapshot.journal) == 1
    assert snapshot.journal[0]["pack_id"] == PACK_ID
    assert snapshot.journal[0]["artifact_digest"] == first["artifact_digest"]

    conflicting_parent = tmp_path / "conflicting"
    conflicting_parent.mkdir()
    conflicting_source, conflicting_trust = _signed_external_pack(
        conflicting_parent,
        runtime_suffix="\n# different signed artifact\n",
    )
    with pytest.raises(ExternalPackCatalogDenied, match="different digest"):
        admit_signed_external_pack(
            conflicting_source,
            trust_store_path=conflicting_trust,
        )
    assert load_external_pack_catalog().entries[PACK_ID] == first


def test_host_extension_kind_cannot_enter_normal_pack_catalog(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TOBKIRI_USER_DATA", str(tmp_path / "user-data"))
    source, trust_store = _signed_external_pack(tmp_path, kind="host_extension")
    with pytest.raises(ExternalPackCatalogDenied, match="Normal Sandbox"):
        admit_signed_external_pack(source, trust_store_path=trust_store)
    assert load_external_pack_catalog().records == {}
