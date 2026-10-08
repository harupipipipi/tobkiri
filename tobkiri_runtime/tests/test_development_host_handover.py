"""Isolated real-HMAC storage/data transfer; no VM, network or real user state."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
import hashlib
import hmac
import json
from pathlib import Path
import time

import pytest

from core_runtime.development_host_handover_guard import (
    read_handover_journal,
    require_completed_handover,
    write_handover_journal,
)
from core_runtime.process_identity import ProcessIdentityEvidence
from core_runtime.profile_definition_store_v4 import ProfileDefinitionStore
from ecosystem.defaultspack.backend.sandbox.isolation import macos_vz_handover as storage
from ecosystem.defaultspack.backend.sandbox.isolation.macos_vz_provisioner import (
    MacOSVZProvisioner,
    VZ_STATE_VERSION,
    PACKVM_BACKEND_ID,
    VZ_PLATFORM,
    VZ_INSTANCE,
)
from ecosystem.defaultspack.defaultspack import development_host_handover as owner
from ecosystem.defaultspack.defaultspack.development_host_data import (
    import_host_data,
    prepare_host_data,
)
from ecosystem.rumi_provider_registry_pack.runtime.registry import ProviderRegistry
from ecosystem.rumi_model_registry_pack.runtime.registry import ModelRegistry
from ecosystem.rumi_conversation_store_pack.runtime.store import ConversationStore
from tests.test_macos_vz_provisioner import provisioner_fixture as provisioner_fixture
from tobkiri_protocol.canonical import canonical_digest
from tobkiri_protocol.secure_persistence import SecureDirectory

SOURCE_HOST = "sha256:" + "a" * 64
TARGET_HOST = "sha256:" + "b" * 64


@pytest.fixture
def transfer_fixture(tmp_path, provisioner_fixture, monkeypatch):
    source, manifest, original_base = provisioner_fixture
    source_root = tmp_path / "builds" / SOURCE_HOST[7:] / "user_data" / "packvm-vz"
    target_root = tmp_path / "builds" / TARGET_HOST[7:] / "user_data" / "packvm-vz"
    for root in (source_root, target_root):
        SecureDirectory(root, create=True)
    source._requested_state_dir = source_root
    source._state_dir = source_root
    del source._load_state
    del source._verify_state_bindings
    source._ensure_state_root()
    instance = source_root / "instances" / VZ_INSTANCE
    SecureDirectory(instance, create=True).write_bytes_atomic("base-image.json", b"{}")
    cache = source_root / "image-cache" / "verified"
    SecureDirectory(cache, create=True).write_bytes_atomic("base.raw", original_base.read_bytes())
    base = cache / "base.raw"
    manifest = replace(manifest, image_size_bytes=base.stat().st_size)
    source._require_manifest = lambda: manifest
    source._write_attested_state(
        {
            "version": VZ_STATE_VERSION,
            "backend_id": PACKVM_BACKEND_ID,
            "platform": VZ_PLATFORM,
            "instance": VZ_INSTANCE,
            "image_digest": manifest.image_digest,
            "image_source": manifest.image_source,
            "cloud_template_digest": manifest.config_digest,
            "helper_digest": manifest.helper_digest,
            "guest_runner_digest": manifest.agent_digest,
            "bubblewrap_digest": manifest.bubblewrap_digest,
            "host_build_digest": manifest.helper_digest,
            "instance_root": str(instance),
            "instance_root_device": instance.stat().st_dev,
            "instance_root_inode": instance.stat().st_ino,
            "base_image_path": str(base),
            "protocol_ready": True,
            "stopped": False,
            **source.recovery_identity(),
        }
    )
    target = MacOSVZProvisioner(
        state_dir=target_root,
        platform_system="darwin",
        machine="arm64",
        helper_identity_verifier=lambda _: (True, None),
    )
    target._ensure_state_root()
    target._require_manifest = lambda: manifest
    monkeypatch.setattr(storage, "_require_storage_quiescent", lambda _: None)
    transfer = storage.MacOSVZStorageHandover(
        source, target, source_host_digest=SOURCE_HOST, target_host_digest=TARGET_HOST, owners={}
    )
    return transfer


def _commit(transfer, plan):
    return transfer.commit(plan, session_digest="sha256:" + "c" * 64, ceremony_nonce="n" * 43)


def test_exact_storage_moves_and_fresh_registration_retains_old_keys(transfer_fixture):
    transfer = transfer_fixture
    source, target = transfer.source, transfer.target
    old_key = (source.state_path.parent / "packvm-vz-attestation.key").read_bytes()
    old_record = source.state_path.read_bytes()
    plan = transfer.prepare()
    result = _commit(transfer, plan)
    assert result["stage"] == "finalized"
    assert source.state_path.read_bytes() == old_record
    assert (source.state_path.parent / "packvm-vz-attestation.key").read_bytes() == old_key
    assert (target.state_path.parent / "packvm-vz-attestation.key").read_bytes() != old_key
    fresh = target._load_state()
    assert fresh["session_digest"] == "sha256:" + "c" * 64
    assert fresh["previous_attestation_digest"] == plan["source_attestation_digest"]
    assert fresh["host_handover_plan_digest"] == plan["plan_digest"]
    for move in plan["moves"]:
        assert not Path(move["source"]).exists()
        assert storage._identity(Path(move["target"])) == move["identity"]
    target._verify_state_bindings(fresh, target._require_manifest())
    with pytest.raises((OSError, ValueError)):
        source._verify_registration_source(source._load_state(), target._require_manifest())
    with pytest.raises(ValueError):
        transfer.recover_to_source(plan)  # finalized standalone VM never rolls back


@pytest.mark.parametrize("stage", ["prepared", "moved-1", "moved-2", "registered"])
def test_interruption_fences_destination_and_restores_same_inodes(transfer_fixture, stage):
    transfer = transfer_fixture
    plan = transfer.prepare()

    def fail(current):
        if current == stage:
            raise RuntimeError("isolated publication interruption")

    transfer._fault = fail
    with pytest.raises(RuntimeError, match="interruption"):
        _commit(transfer, plan)
    if transfer.target.state_path.exists():
        with pytest.raises(ValueError, match="recovery"):
            transfer.target._load_state()
    result = transfer.recover_to_source(plan)
    assert result["stage"] == "rolled-back"
    assert transfer.recover_to_source(plan) == result
    for move in plan["moves"]:
        assert storage._identity(Path(move["source"])) == move["identity"]
        assert not Path(move["target"]).exists()
    transfer.source._verify_registration_source(
        transfer.source._load_state(), transfer.target._require_manifest()
    )
    assert (transfer.target.state_path.parent / storage.JOURNAL_NAME).exists()


@pytest.mark.parametrize("state", ["live", "unknown"])
def test_live_or_unknown_old_owner_cannot_transfer(transfer_fixture, monkeypatch, state):
    transfer = transfer_fixture
    transfer.owners = {99_999_999: "frozen-process-start"}
    monkeypatch.setattr(
        storage,
        "process_start_identity",
        lambda _: ProcessIdentityEvidence(state, "current-start" if state == "live" else ""),
    )
    plan = transfer.prepare()
    with pytest.raises(ValueError, match="owners to exit"):
        _commit(transfer, plan)
    assert transfer.source.state_path.exists()
    assert not transfer.target.state_path.exists()
    assert not (transfer.target.state_path.parent / storage.JOURNAL_NAME).exists()


@pytest.mark.parametrize(
    "case", ["domain", "stopped", "source-mac", "destination", "extra-instance"]
)
def test_unreviewed_or_changed_storage_fails_before_move(transfer_fixture, case):
    transfer = transfer_fixture
    root = transfer.source.state_path.parent
    if case == "domain":
        SecureDirectory(root / "domains" / "unreviewed-domain", create=True)
    elif case == "stopped":
        transfer.source._write_attested_state({**transfer.source._load_state(), "stopped": True})
    elif case == "source-mac":
        value = json.loads(transfer.source.state_path.read_bytes())
        value["helper_digest"] = "sha256:" + "d" * 64
        transfer.source.state_path.write_text(json.dumps(value))
    elif case == "destination":
        SecureDirectory(transfer.target.state_path.parent / "instances" / VZ_INSTANCE, create=True)
    else:
        instance = root / "instances" / VZ_INSTANCE
        SecureDirectory(instance, create=False).write_bytes_atomic(
            "guest-key.pem", b"not transferable"
        )
    with pytest.raises((OSError, ValueError)):
        transfer.prepare()
    assert not transfer.target.state_path.exists()


def test_recovery_rejects_tampered_journal_paths_and_mac(transfer_fixture):
    transfer = transfer_fixture
    plan = transfer.prepare()
    transfer._fault = lambda stage: (
        (_ for _ in ()).throw(RuntimeError("stop")) if stage == "moved-1" else None
    )
    with pytest.raises(RuntimeError):
        _commit(transfer, plan)
    changed = deepcopy(plan)
    changed["moves"][0]["source"] = str(transfer.source.state_path.parent / "unreviewed")
    changed["plan_digest"] = canonical_digest(
        {key: item for key, item in changed.items() if key != "plan_digest"}
    )
    with pytest.raises(ValueError, match="paths changed"):
        transfer.recover_to_source(changed)
    journal_path = transfer.target.state_path.parent / storage.JOURNAL_NAME
    journal = json.loads(journal_path.read_bytes())
    journal["stage"] = "finalized"
    journal_path.write_text(json.dumps(journal))
    with pytest.raises(ValueError, match="authentication"):
        transfer.recover_to_source(plan)
    assert Path(plan["moves"][0]["target"]).exists()


def test_exclusive_move_does_not_overwrite_destination(tmp_path):
    source = SecureDirectory(tmp_path, create=False)
    source.write_bytes_atomic("source", b"owned source")
    source.write_bytes_atomic("destination", b"retained destination")
    with pytest.raises(ValueError, match="appeared"):
        storage._move_exclusive(
            tmp_path / "source", tmp_path / "destination", storage._identity(tmp_path / "source")
        )
    assert (tmp_path / "source").read_bytes() == b"owned source"
    assert (tmp_path / "destination").read_bytes() == b"retained destination"


def _data_fixture(root):
    SecureDirectory(root, create=True)
    provider = ProviderRegistry("defaults", user_data_root=root)
    provider.save(
        {
            "provider_instance_id": "provider.test",
            "adapter_id": "openrouter",
            "display_name": "Test connection",
            "credential_handle": "credential:old-private-handle",
            "endpoint": "https://example.invalid/v1?token=private",
            "health_evidence": {"status": "ready", "verified": True},
            "metadata": {"catalog_provider_id": "openrouter", "private_note": "not imported"},
        },
        expected_revision=0,
    )
    model = ModelRegistry("defaults", user_data_root=root)
    model.save(
        {
            "model_profile_id": "model.test",
            "model_id": "test-model",
            "credential_handle": "credential:old-model-handle",
            "parameters": {"max_tokens": 2048},
            "metadata": {"provider_connection_id": "provider.test", "private_note": "not imported"},
        },
        expected_revision=0,
    )
    conversation = ConversationStore("defaults", user_data_root=root)
    conversation.create(
        {"id": "conversation.test", "title": "Retained conversation"}, expected_revision=0
    )
    conversation.append_message(
        "conversation.test",
        {
            "id": "message.test",
            "role": "assistant",
            "parts": [{"type": "text", "text": "parts-only text"}],
            "status": "pending",
            "widget": {"authority_reference": "not imported"},
            "metadata": {"credential_handle": "not imported"},
        },
        expected_conversation_revision=1,
    )


def test_data_owner_import_keeps_text_and_route_without_old_credentials(tmp_path):
    source, target = tmp_path / "old", tmp_path / "new"
    _data_fixture(source)
    SecureDirectory(target, create=True)
    prepared = prepare_host_data(source, bootstrap_profile_id="defaults")
    encoded = json.dumps(prepared)
    for secret in (
        "credential:old-private-handle",
        "credential:old-model-handle",
        "?token=private",
        "not imported",
    ):
        assert secret not in encoded
    result = import_host_data(target, prepared)
    assert result["provider_credentials_required"]
    provider = ProviderRegistry("defaults", user_data_root=target).snapshot()["providers"][0]
    assert provider["credential_handle"] is None and not provider["enabled"]
    assert not provider["health_evidence"]["verified"] and provider["endpoint"] is None
    model = ModelRegistry("defaults", user_data_root=target).get("model.test")
    assert model["metadata"]["provider_connection_id"] == "provider.test"
    assert model["parameters"]["max_tokens"] == 2048
    assert model["credential_handle"] is None and not model["enabled"]
    conversation = ConversationStore("defaults", user_data_root=target).get("conversation.test")
    assert conversation["messages"][0]["parts"] == [{"type": "text", "text": "parts-only text"}]
    assert conversation["messages"][0]["status"] == "cancelled"
    assert conversation["lifecycle"]["state"] == "unknown"
    assert (
        not (target / "authority").exists() and not (target / "profiles" / "active.json").exists()
    )
    assert (
        ProviderRegistry("defaults", user_data_root=source).snapshot()["providers"][0][
            "credential_handle"
        ]
        is not None
    )


def test_local_endpoint_and_complete_payload_validation_before_any_import(tmp_path):
    source, target = tmp_path / "old", tmp_path / "new"
    _data_fixture(source)
    provider = ProviderRegistry("defaults", user_data_root=source)
    provider.save(
        {
            "provider_instance_id": "provider.local",
            "adapter_id": "local-openai-compatible",
            "endpoint": "http://127.0.0.1:11434/v1",
        },
        expected_revision=1,
    )
    prepared = prepare_host_data(source, bootstrap_profile_id="defaults")
    assert (
        next(
            row
            for row in prepared["profiles"][0]["providers"]
            if row["provider_instance_id"] == "provider.local"
        )["endpoint"]
        == "http://127.0.0.1:11434/v1"
    )
    broken = deepcopy(prepared)
    broken["profiles"][0]["models"][0]["metadata"]["provider_connection_id"] = "unknown-provider"
    broken["data_digest"] = canonical_digest(
        {key: value for key, value in broken.items() if key != "data_digest"}
    )
    SecureDirectory(target, create=True)
    with pytest.raises(ValueError, match="connection is missing"):
        import_host_data(target, broken)
    assert not (target / "packs").exists() and not (target / "profiles").exists()


def _proof(plan_digest, nonce, secret="isolated-native-secret", action="commit"):
    now = int(time.time())
    message = "\n".join(
        (
            "v1",
            "tobkiri.development-host-handover",
            "main",
            action,
            plan_digest,
            nonce,
            str(now),
            str(now + 180),
        )
    )
    return {
        "version": 1,
        "window_label": "main",
        "action": action,
        "plan_digest": plan_digest,
        "ceremony_nonce": nonce,
        "issued_at": now,
        "expires_at": now + 180,
        "signature": hmac.new(secret.encode(), message.encode(), hashlib.sha256).hexdigest(),
    }


def test_native_provenance_rejects_flag_stale_wrong_plan_and_recovery_replay():
    digest, nonce = "sha256:" + "d" * 64, "n" * 43
    owner._verify_native_consent(
        _proof(digest, nonce),
        plan_digest=digest,
        nonce=nonce,
        native_secret="isolated-native-secret",
    )
    for value in (
        {"approved": True},
        {**_proof(digest, nonce), "plan_digest": "sha256:" + "e" * 64},
        {**_proof(digest, nonce), "expires_at": 1},
        _proof(digest, nonce, action="recover"),
        {**_proof(digest, nonce), "version": True},
    ):
        with pytest.raises(ValueError):
            owner._verify_native_consent(
                value, plan_digest=digest, nonce=nonce, native_secret="isolated-native-secret"
            )


def test_complete_native_successor_uses_fresh_owner_data_and_requires_activation(
    transfer_fixture, monkeypatch
):
    transfer = transfer_fixture
    source_root, target_root = (
        transfer.source.state_path.parent.parent,
        transfer.target.state_path.parent.parent,
    )
    _data_fixture(source_root)
    from core_runtime.bootstrap.profile_capture import host_profile_catalog

    definition = deepcopy(host_profile_catalog().profiles["defaults"])
    definition["profile_id"] = "retained-profile"
    ProfileDefinitionStore(source_root).create_profile(definition)
    monkeypatch.setattr(owner, "_verify_previous_host", lambda root, exe: SOURCE_HOST)
    monkeypatch.setattr(owner, "_source_owners", lambda root: {})
    service = owner.DevelopmentHostHandover(transfer.target, bootstrap_profile_id="defaults")
    review = service.prepare(
        {
            "source_root": str(source_root),
            "source_executable": str(source_root / "unused-test-executable"),
        }
    )
    proof = _proof(review["plan"]["plan_digest"], review["ceremony_nonce"])
    payload = {
        "plan_digest": review["plan"]["plan_digest"],
        "ceremony_nonce": review["ceremony_nonce"],
        "native_consent": proof,
    }
    result = service.commit(payload, native_secret="isolated-native-secret")
    assert result["stage"] == "completed" and result["activation_required"]
    require_completed_handover(target_root)
    assert read_handover_journal(target_root)["stage"] == "completed"
    assert (
        ModelRegistry("defaults", user_data_root=target_root).get("model.test")["metadata"][
            "provider_connection_id"
        ]
        == "provider.test"
    )
    assert not (target_root / "profiles" / "active.json").exists()
    assert not (target_root / "authority").exists()
    with pytest.raises(ValueError, match="consumed"):
        service.commit(payload, native_secret="isolated-native-secret")
    assert service.prepare_recovery()["completed"]


def test_global_fence_is_authenticated_and_blocks_interrupted_activation(tmp_path):
    root = tmp_path / "user_data"
    SecureDirectory(root, create=True)
    write_handover_journal(
        root, {"stage": "data-staged", "plan": {"plan_digest": "sha256:" + "a" * 64}}
    )
    with pytest.raises(ValueError, match="recovery before activation"):
        require_completed_handover(root)
    value = json.loads((root / "development-host-handover.json").read_bytes())
    value["stage"] = "completed"
    (root / "development-host-handover.json").write_text(json.dumps(value))
    with pytest.raises(ValueError, match="authentication"):
        require_completed_handover(root)


def _service_review(transfer, monkeypatch):
    source_root = transfer.source.state_path.parent.parent
    _data_fixture(source_root)
    monkeypatch.setattr(owner, "_verify_previous_host", lambda root, exe: SOURCE_HOST)
    monkeypatch.setattr(owner, "_source_owners", lambda root: {})
    service = owner.DevelopmentHostHandover(transfer.target, bootstrap_profile_id="defaults")
    review = service.prepare(
        {
            "source_root": str(source_root),
            "source_executable": str(source_root / "unused-test-executable"),
        }
    )
    return service, review


def _consented(review, action="commit"):
    return {
        "plan_digest": review["plan"]["plan_digest"],
        "ceremony_nonce": review["ceremony_nonce"],
        "native_consent": _proof(
            review["plan"]["plan_digest"], review["ceremony_nonce"], action=action
        ),
    }


@pytest.mark.parametrize("phase", ["staging", "published-before-journal", "definitions"])
def test_native_recovery_retains_staging_and_restores_finalized_vm(
    transfer_fixture, monkeypatch, phase
):
    transfer = transfer_fixture
    service, review = _service_review(transfer, monkeypatch)
    target_root = transfer.target.state_path.parent.parent
    if phase == "staging":
        real_import = owner.import_host_data

        def fail_import(root, data):
            real_import(root, data)
            raise RuntimeError("isolated interruption")

        monkeypatch.setattr(owner, "import_host_data", fail_import)
    elif phase == "published-before-journal":
        real_move = owner._move_exclusive

        def fail_move(source, target, identity):
            real_move(source, target, identity)
            raise RuntimeError("isolated interruption")

        monkeypatch.setattr(owner, "_move_exclusive", fail_move)
    else:

        def fail_definitions(*args, **kwargs):
            raise RuntimeError("isolated interruption")

        monkeypatch.setattr(ProfileDefinitionStore, "import_profile_successors", fail_definitions)
    with pytest.raises(RuntimeError, match="interruption"):
        service.commit(_consented(review), native_secret="isolated-native-secret")
    with pytest.raises(ValueError, match="recovery before activation"):
        require_completed_handover(target_root)
    monkeypatch.undo()
    # Restore only isolated process/signature probes, with real storage/data.
    monkeypatch.setattr(storage, "_require_storage_quiescent", lambda _: None)
    monkeypatch.setattr(owner, "_verify_previous_host", lambda root, exe: SOURCE_HOST)
    recovery = service.prepare_recovery()
    result = service.recover(
        _consented(recovery, "recover"), native_secret="isolated-native-secret"
    )
    assert result["stage"] == "source-restored"
    journal = read_handover_journal(target_root)
    assert journal["stage"] == "rolled-back"
    assert Path(journal["staging"]).exists()
    for move in review["plan"]["vm_plan"]["moves"]:
        assert storage._identity(Path(move["source"])) == move["identity"]
        assert not Path(move["target"]).exists()
    assert not (
        target_root / "packs" / "rumi_model_registry_pack" / "profiles" / "defaults"
    ).exists()
    assert service.prepare_recovery()["completed"]
    with pytest.raises(ValueError, match="recovery before activation"):
        require_completed_handover(target_root)


def test_destination_definition_cas_does_not_adopt_unreviewed_edits(tmp_path):
    from core_runtime.bootstrap.profile_capture import host_profile_catalog
    from core_runtime.profile_definition_store_v4 import ProfileDefinitionStoreConflict

    root = tmp_path / "user_data"
    store = ProfileDefinitionStore(root)
    definition = deepcopy(host_profile_catalog().profiles["defaults"])
    current = store.create_profile(definition)
    reviewed = store.snapshot()
    successor = {**definition, "display_name": "Reviewed import"}
    concurrent = store.update_profile(current.profile_id, patch={"display_name": "Concurrent edit"})
    completed = []
    with pytest.raises(ProfileDefinitionStoreConflict):
        store.import_profile_successors(
            [successor],
            expected_store_generation=reviewed["generation"],
            expected_snapshot_digest=canonical_digest(reviewed),
            on_committed=completed.append,
        )
    assert not completed
    assert store.get_profile(current.profile_id).profile_revision == concurrent.profile_revision
    fresh = store.snapshot()
    store.import_profile_successors(
        [successor],
        expected_store_generation=fresh["generation"],
        expected_snapshot_digest=canonical_digest(fresh),
        on_committed=completed.append,
    )
    assert len(completed) == 1 and completed[0]["generation"] == fresh["generation"] + 1
    assert store.get_profile(current.profile_id).parent_revision == concurrent.profile_revision


def test_missing_vm_handover_journal_remains_fenced(transfer_fixture):
    transfer = transfer_fixture
    plan = transfer.prepare()
    _commit(transfer, plan)
    (transfer.target.state_path.parent / storage.JOURNAL_NAME).unlink()
    with pytest.raises(ValueError, match="authenticated recovery journal"):
        transfer.target._load_state()


def test_legacy_workspace_activation_denies_destination(tmp_path):
    root = tmp_path / "user_data"
    SecureDirectory(
        root / "workspaces" / "defaults" / "activation", create=True
    ).write_bytes_atomic("active.json", b"{}")
    with pytest.raises(ValueError, match="inactive destination"):
        owner._require_inactive(root)


def test_private_native_route_rejects_origin_even_with_authenticated_launcher():
    from types import SimpleNamespace
    from core_runtime.native_host_handover_http import handle_native_host_handover

    calls = []
    handler = SimpleNamespace(
        headers={"Origin": "http://127.0.0.1:8765"},
        _native_pack_launcher_authenticated=lambda: True,
        _discard_request_body=lambda: calls.append("discarded"),
        _send_response=lambda response, status: calls.append(status),
    )
    assert handle_native_host_handover(handler, "POST", "/api/internal/native-host-handover/commit")
    assert calls == ["discarded", 401]


@pytest.mark.parametrize(
    "witness",
    [
        "development-host-handover.key",
        "development-host-staging",
        "packvm-vz/development-host-handover.json",
    ],
)
def test_missing_global_journal_cannot_release_migration_fence(tmp_path, witness):
    root = tmp_path / "user_data"
    path = root / witness
    if witness == "development-host-staging":
        SecureDirectory(path, create=True)
    else:
        SecureDirectory(path.parent, create=True).write_bytes_atomic(path.name, b"retained witness")
    with pytest.raises(ValueError, match="authenticated recovery journal"):
        require_completed_handover(root)


def test_source_namespace_replacement_denies_before_staging(transfer_fixture, monkeypatch):
    service, review = _service_review(transfer_fixture, monkeypatch)
    root = transfer_fixture.source.state_path.parent.parent
    moved = root.with_name("retained-original")
    root.rename(moved)
    SecureDirectory(root, create=True)
    with pytest.raises(ValueError, match="source namespace changed"):
        service.commit(_consented(review), native_secret="isolated-native-secret")
    assert not (
        transfer_fixture.target.state_path.parent.parent / "development-host-handover.key"
    ).exists()


def test_vanished_staged_owner_cannot_complete(transfer_fixture, monkeypatch):
    service, review = _service_review(transfer_fixture, monkeypatch)
    real_commit = storage.MacOSVZStorageHandover.commit
    target_root = transfer_fixture.target.state_path.parent.parent

    def remove_owner_after_vm_commit(transfer, *args, **kwargs):
        result = real_commit(transfer, *args, **kwargs)
        journal = read_handover_journal(target_root)
        relative = journal["plan"]["publications"][0]
        path = Path(journal["staging"]) / relative
        path.rename(path.with_name("retained-missing-owner"))
        return result

    monkeypatch.setattr(storage.MacOSVZStorageHandover, "commit", remove_owner_after_vm_commit)
    with pytest.raises(ValueError, match="staged data owner is missing"):
        service.commit(_consented(review), native_secret="isolated-native-secret")
    assert read_handover_journal(target_root)["stage"] == "vm-transferred"
    with pytest.raises(ValueError, match="recovery before activation"):
        require_completed_handover(target_root)
