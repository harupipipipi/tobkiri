"""Real prompt owner reads; captured authority is exercised separately in Broker tests."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from core_runtime.host_provider_backend_v4 import HostProviderCaptureContextV4
from ecosystem.rumi_prompt_studio_pack.runtime import resource_host as host
from ecosystem.rumi_prompt_studio_pack.runtime.store import PromptStudioStore
from tobkiri_host.models import OpaqueAuthorityRef
from tobkiri_protocol.canonical import canonical_digest
from tobkiri_protocol.secure_persistence import SecurePersistenceError

ABSENT = "sha256:" + hashlib.sha256(b"").hexdigest()


@pytest.fixture
def prompt_host(tmp_path: Path):
    binding = SimpleNamespace(
        function=SimpleNamespace(function_id=host.FUNCTION_ID, implementation_digest="impl"),
        operation=SimpleNamespace(
            contract_id=host.CONTRACT_ID, operation_id=host.OPERATION_ID,
            contract_version="1.0.0",
        ),
        principal_ref=OpaqueAuthorityRef("prompt-owner"),
        artifact=SimpleNamespace(digest="artifact"),
    )
    activation = {"activation_id": "active"}
    capture = HostProviderCaptureContextV4(
        profile_id="defaults", plan_digest="plan", security_epoch=1,
        activation=activation, state_root=tmp_path,
        provider_bindings=(binding,), catalog_bindings=(binding,),
        domain_ids={(host.CONTRACT_ID, host.OPERATION_ID, "prompt-owner"): "domain"},
        user_data_root=tmp_path,
    )
    provider = host.HOST_PROVIDER_FACTORY.capture(capture)

    def invoke(payload, *, change=None, stale=False):
        context = SimpleNamespace(
            profile_id="defaults", activation_id="active", plan_digest="plan",
            security_epoch=1, activation_digest=canonical_digest(activation),
        )
        envelope = SimpleNamespace(
            context=context, target_principal=binding.principal_ref,
            target_domain=OpaqueAuthorityRef("domain"), contract_id=host.CONTRACT_ID,
            contract_version="1.0.0", operation_id=host.OPERATION_ID, payload=payload,
        )
        if change is not None:
            change(envelope)

        def guard():
            if stale:
                raise PermissionError("stale capture")

        invocation = SimpleNamespace(envelope=envelope, assert_current=guard)
        return provider.contributions[0].invoke(host.OPERATION_ID, payload, invocation)

    yield invoke, provider
    provider.close()


def test_reads_use_the_captured_profile_and_existing_owner_records(prompt_host, tmp_path):
    invoke, _ = prompt_host
    assert invoke({"operation": "list"})["prompts"] == []
    assert not (tmp_path / "packs").exists()
    store = PromptStudioStore("defaults", user_data_root=tmp_path)
    store.save("system", "Answer precisely.", expected_body_hash=ABSENT)
    PromptStudioStore("other", user_data_root=tmp_path).save(
        "system", "Other profile body", expected_body_hash=ABSENT,
    )
    before = store.path.read_bytes()
    result = invoke({"operation": "get", "prompt_id": "system"})
    assert result["profile_id"] == "defaults"
    assert result["prompt"]["body"] == "Answer precisely."
    assert "versions" not in result["prompt"]
    assert invoke({"operation": "list"})["prompts"][0]["prompt_id"] == "system"
    assert invoke({"operation": "edge_states"})["edge_states"] == {}
    assert store.path.read_bytes() == before


@pytest.mark.parametrize("patch", [
    {"profile_id": "other"}, {"profile_id": None}, {"approved": True},
    {"caller_id": "other"}, {"_session_id": "forged"},
    {"user_data_root": "/foreign"}, {"operation": "save"},
    {"operation": "migration.apply"}, {"operation": None},
    {"prompt_id": "../outside"}, {"prompt_id": "/absolute"},
    {"prompt_id": 123}, {"prompt_id": "a" * 257},
])
def test_invalid_scope_is_rejected_before_reading(prompt_host, monkeypatch, patch):
    invoke, _ = prompt_host

    def forbidden_read(_self):
        raise AssertionError("invalid request read the store")

    monkeypatch.setattr(PromptStudioStore, "_read", forbidden_read)
    with pytest.raises((ValueError, PermissionError)):
        invoke({"operation": "get", "prompt_id": "system", **patch})


@pytest.mark.parametrize("field,value", [
    ("profile_id", "other"), ("activation_id", "other"),
    ("plan_digest", "other"), ("security_epoch", 2),
    ("activation_digest", "other"),
])
def test_changed_capture_cannot_read(prompt_host, monkeypatch, field, value):
    invoke, _ = prompt_host
    monkeypatch.setattr(PromptStudioStore, "_read", lambda _: pytest.fail("read occurred"))
    with pytest.raises(PermissionError):
        invoke({"operation": "list"}, change=lambda e: setattr(e.context, field, value))


def test_revoked_and_closed_capture_cannot_read(prompt_host, monkeypatch):
    invoke, provider = prompt_host
    monkeypatch.setattr(PromptStudioStore, "_read", lambda _: pytest.fail("read occurred"))
    with pytest.raises(PermissionError):
        invoke({"operation": "list"}, stale=True)
    provider.close()
    with pytest.raises(PermissionError):
        invoke({"operation": "list"})


@pytest.mark.parametrize("kind", ["file", "ancestor", "replacement"])
def test_prompt_reads_reject_links_and_root_replacement(tmp_path, kind):
    store = PromptStudioStore("defaults", user_data_root=tmp_path)
    store.save("system", "Original body", expected_body_hash=ABSENT)
    if kind == "file":
        outside = tmp_path / "outside.json"
        outside.write_bytes(store.path.read_bytes())
        store.path.unlink()
        store.path.symlink_to(outside)
    elif kind == "ancestor":
        root = store.root.parent
        original = root.with_name("retained-profiles")
        root.rename(original)
        root.symlink_to(original, target_is_directory=True)
    else:
        assert store.get("system")["body"] == "Original body"
        store.root.rename(store.root.with_name("retained-defaults"))
        replacement = PromptStudioStore("defaults", user_data_root=tmp_path)
        replacement.save("system", "Replacement body", expected_body_hash=ABSENT)
    with pytest.raises(SecurePersistenceError):
        store.get("system")


def test_oversized_or_corrupt_store_is_not_treated_as_empty(tmp_path):
    store = PromptStudioStore("defaults", user_data_root=tmp_path)
    store.save("system", "body", expected_body_hash=ABSENT)
    original = store.path.read_bytes()
    with store.path.open("r+b") as output:
        output.truncate(16 * 1024 * 1024 + 1)
    with pytest.raises((ValueError, SecurePersistenceError)):
        store.snapshot()
    store.path.write_text("not JSON")
    with pytest.raises(ValueError):
        store.snapshot()
    data = json.loads(original)
    data["profile_id"] = "other"
    store.path.write_text(json.dumps(data))
    with pytest.raises(ValueError, match="profile mismatch"):
        store.snapshot()
