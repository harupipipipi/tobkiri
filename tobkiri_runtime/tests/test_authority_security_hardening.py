from __future__ import annotations

import json
import hashlib
import subprocess
from pathlib import Path

import pytest

from core_runtime.pack_artifact_integrity import (
    verify_declared_artifacts,
    write_host_install_record,
)
from core_runtime.resolved_profile import _manifest_contract_metadata
from ecosystem.rumi_agent_runtime_service_pack.runtime import runtime
from ecosystem.rumi_agent_state_store_pack.runtime.store import (
    AgentStateStore,
)
from ecosystem.rumi_git_publish_pack.runtime.publish import _arguments as publish_arguments
from ecosystem.rumi_git_write_pack.runtime.write import _arguments as write_arguments
from ecosystem.rumi_git_write_pack.runtime.write import GitWriteService
from ecosystem.rumi_host_authority_bridge_pack.runtime import bridge


def test_unsigned_nonbuiltin_pack_requires_host_install_record(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("RUMI_PACK_PUBLISHER_TRUST_STORE", raising=False)
    ok, diagnostics = verify_declared_artifacts(
        tmp_path,
        {"id": "third_party", "version": "1.0.0"},
    )
    assert ok is False
    assert "Host install record" in diagnostics[0]


def test_unsigned_nonbuiltin_requires_explicit_host_developer_record(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pack_root = tmp_path / "pack"
    pack_root.mkdir()
    trust_store = tmp_path / "trust.json"
    write_host_install_record(
        trust_store,
        pack_id="third_party",
        record={
            "signature_required": False,
            "developer_mode": True,
            "publisher_id": "",
            "key_id": "",
            "installed_version": "1.0.0",
            "signed_manifest_path": "",
            "contract_versions": {},
            "requested_capabilities": [],
        },
    )
    monkeypatch.setenv("RUMI_PACK_PUBLISHER_TRUST_STORE", str(trust_store))
    monkeypatch.setenv("RUMI_PACK_DEVELOPER_MODE", "1")
    ok, diagnostics = verify_declared_artifacts(
        pack_root,
        {"id": "third_party", "version": "1.0.0"},
    )
    assert ok is True
    assert diagnostics == ()


def test_provider_trust_uses_only_host_attestation() -> None:
    manifest = {
        "version": "1.0.0",
        "_v3_manifest": {
            "provenance": {
                "content_hash": "sha256:" + "a" * 64,
                "build_identity": "fixture",
                "trust_class": "system",
            },
            "contracts": {
                "provides": [
                    {
                        "id": "rumi.service.fixture.v1",
                        "version": "1.0.0",
                        "provider_instance_id": "fixture.provider",
                        "cardinality": "one",
                        "security": "internal",
                        "failure": "fail_closed",
                        "lifecycle": {
                            "introduced": "1.0.0",
                            "deprecated": False,
                        },
                        "schemas": {},
                        "isolation": "process",
                    }
                ]
            },
        },
    }
    providers, _, diagnostics = _manifest_contract_metadata(
        ("third-party",),
        {"third-party": manifest},
        verified_pack_trust={"third-party": "verified"},
    )
    assert not diagnostics
    assert providers[0].trust_class == "verified"


def test_agent_effect_commit_barrier_is_atomic(tmp_path: Path) -> None:
    store = AgentStateStore("default", root=tmp_path)
    begun = store.apply(
        "run.begin",
        {
            "expected_revision": 0,
            "run_id": "run-1",
            "idempotency_key": "key-1",
            "agent_profile_id": "default",
            "conversation_id": "conversation-1",
            "turn_id": "turn-1",
            "parent_run_id": "",
        },
    )
    store.apply(
        "run.transition",
        {
            "expected_revision": begun["revision"],
            "run_id": "run-1",
            "status": "running",
            "step": 0,
            "details": {},
        },
    )
    effect = store.apply(
        "run.effect.begin",
        {
            "expected_revision": 2,
            "run_id": "run-1",
            "executor_token": "executor-secret",
            "effect_receipt": "sha256:effect",
        },
    )
    cancelled = store.apply(
        "run.cancel",
        {
            "expected_revision": effect["revision"],
            "run_id": "run-1",
            "reason": "stop",
        },
    )
    assert cancelled["too_late"] is True
    assert cancelled["run"]["cancel_requested"] is True
    assert cancelled["run"]["status"] == "running"


def test_raw_tool_payload_is_ephemeral_and_one_shot() -> None:
    receipt = runtime._stash_ephemeral_tool_payload(
        {"pending_tool_intents": [{"arguments": {"secret": "value"}}]}
    )
    assert "secret" not in receipt
    assert runtime._load_ephemeral_tool_payload(receipt)["pending_tool_intents"]
    with pytest.raises(RuntimeError):
        runtime._load_ephemeral_tool_payload(receipt)


def test_git_mutations_require_pinned_snapshot() -> None:
    with pytest.raises(ValueError):
        write_arguments("stage", {"paths": ["a.txt"]})
    with pytest.raises(ValueError):
        publish_arguments({"branch": "main"}, dry_run=False)


def test_git_commit_uses_isolated_index_and_ref_cas(tmp_path: Path) -> None:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(
        ["git", "-C", str(tmp_path), "config", "user.email", "test@example.com"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(tmp_path), "config", "user.name", "Test"],
        check=True,
    )
    target = tmp_path / "file.txt"
    target.write_text("before\n")
    subprocess.run(["git", "-C", str(tmp_path), "add", "file.txt"], check=True)
    subprocess.run(
        ["git", "-C", str(tmp_path), "commit", "-q", "-m", "initial"],
        check=True,
    )
    target.write_text("after\n")

    def git(*args: str) -> str:
        return subprocess.check_output(
            ["git", "-C", str(tmp_path), *args],
            text=True,
        )

    head = git("rev-parse", "HEAD").strip()
    tree = git("rev-parse", "HEAD^{tree}").strip()
    status = git("status", "--porcelain=v2", "--untracked-files=all")

    class Client:
        def invoke(self, contract: str, name: str, payload: object) -> dict[str, object]:
            if contract.endswith("workspace.v1"):
                return {
                    "root_path": str(tmp_path),
                    "mount_revision": 1,
                }
            if contract.endswith("git.read.v1"):
                return {"repository_root": "."}
            return {"authorized": True}

    result = GitWriteService(Client()).invoke(
        "commit",
        {
            "profile_id": "default",
            "workspace_id": "workspace",
            "expected_mount_revision": 1,
            "expected_head": head,
            "expected_tree": tree,
            "expected_status_hash": hashlib.sha256(
                status.encode("utf-8")
            ).hexdigest(),
            "paths": ["file.txt"],
            "message": "isolated commit",
        },
    )
    assert result["commit_hash"] == git("rev-parse", "HEAD").strip()


def test_authority_receipt_is_durable_and_one_shot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(bridge, "_RECEIPT_ROOT", tmp_path / "receipts")
    scope = {
        "service_pack_id": "service-pack",
        "operation": "effect.write",
        "authority": "effect.write",
        "caller_id": "caller",
        "caller_pack_id": "caller-pack",
        "caller_function_id": "function",
        "profile_id": "default",
        "workspace_id": "workspace",
        "session_id": "session",
        "arguments": {"path": "safe.txt"},
        "approval_required": False,
    }
    issued = bridge._authorize(scope)
    stored = list((tmp_path / "receipts").glob("*.json"))
    assert len(stored) == 1
    assert json.loads(stored[0].read_text())["status"] == "issued"
    redeemed = bridge._redeem({**scope, "receipt": issued["receipt"]})
    assert redeemed["authorized"] is True
    replay = bridge._redeem({**scope, "receipt": issued["receipt"]})
    assert replay["authorized"] is False
