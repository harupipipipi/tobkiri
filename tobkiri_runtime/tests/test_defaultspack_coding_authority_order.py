from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parent.parent
DEFAULTSPACK_ROOT = ROOT / "ecosystem" / "defaultspack"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(DEFAULTSPACK_ROOT) not in sys.path:
    sys.path.insert(0, str(DEFAULTSPACK_ROOT))

pytestmark = pytest.mark.contract


def test_coding_contract_recovers_persisted_profile_for_request_workers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from domain.coding import contract_adapter

    registry = object()
    monkeypatch.setattr(
        contract_adapter,
        "captured_profile_id",
        lambda current_registry: "profile-1",
    )
    monkeypatch.setattr(
        contract_adapter,
        "get_container",
        lambda: SimpleNamespace(get_or_none=lambda key: registry),
    )
    captured: dict[str, Any] = {}

    def invoke(interface_registry, contract_id, operation, payload):
        captured.update(
            registry=interface_registry,
            contract_id=contract_id,
            operation=operation,
            payload=payload,
        )
        return {"ok": True}

    monkeypatch.setattr(contract_adapter, "invoke_global_contract", invoke)

    result = contract_adapter.invoke_coding_contract(
        contract_adapter.WORKSPACE_RESOURCE,
        "list",
        {"cursor": None},
    )

    assert result == {"ok": True}
    assert captured == {
        "registry": registry,
        "contract_id": contract_adapter.WORKSPACE_RESOURCE,
        "operation": "list",
        "payload": {
            "profile_id": "profile-1",
            "cursor": None,
            "_contract_consumer_pack_id": "defaultspack",
        },
    }


def _authorize(monkeypatch: pytest.MonkeyPatch, events: list[str], mutation_guard):
    from domain.coding import contract_adapter
    from domain.safety.approval import TokenVerification

    def verify(*_args: Any, consume: bool, **_kwargs: Any) -> TokenVerification:
        events.append(f"approval:{consume}")
        return TokenVerification(True, request_id="approval-1")

    def invoke(contract_id: str, operation: str, payload: dict[str, Any]):
        assert contract_id == contract_adapter.HOST_AUTHORITY
        assert operation == "authorize"
        events.append("host-receipt")
        return {"authorized": True, "receipt": "receipt-1"}

    monkeypatch.setattr(contract_adapter.approval, "verify_execution_token", verify)
    monkeypatch.setattr(contract_adapter, "invoke_coding_contract", invoke)
    monkeypatch.setattr(contract_adapter, "_profile_id", lambda: "profile-1")
    return contract_adapter.authorize_legacy_coding_operation(
        legacy_operation="file.write",
        service_pack_id="rumi_file_mutation_pack",
        service_operation="file.write",
        authority="file.write",
        arguments={"path": "src/App.tsx", "content": "new"},
        input_data={
            "workspace_id": "workspace-1",
            "path": "src/App.tsx",
            "content": "new",
            "approval_token": "token",
        },
        context={"principal_id": "agent-1"},
        selected_workspace_id="workspace-1",
        mutation_guard=mutation_guard,
    )


def test_mutation_authority_order_is_precheck_guard_consume_then_host(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []

    def guard(*_args: Any) -> None:
        events.append("mutation-guard")

    result = _authorize(monkeypatch, events, guard)

    assert result["authorized"] is True
    assert result["approval_request_id"] == "approval-1"
    assert events == [
        "approval:False",
        "mutation-guard",
        "approval:True",
        "host-receipt",
    ]


def test_mutation_guard_denial_does_not_consume_token_or_mint_host_receipt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []

    def guard(*_args: Any) -> dict[str, Any]:
        events.append("mutation-guard")
        return {
            "reason": "adaptive_lease_held",
            "code": "ADAPTIVE_LEASE_HELD",
            "message": "lease is held",
        }

    result = _authorize(monkeypatch, events, guard)

    assert result == {
        "authorized": False,
        "reason": "adaptive_lease_held",
        "code": "ADAPTIVE_LEASE_HELD",
        "message": "lease is held",
    }
    assert events == ["approval:False", "mutation-guard"]


def test_every_legacy_mutation_entrypoint_uses_canonical_guard() -> None:
    block_root = DEFAULTSPACK_ROOT / "blocks" / "coding"
    callers = {
        "file_create.py": 1,
        "file_delete.py": 1,
        "file_patch.py": 1,
        "file_write.py": 1,
        "git_branch.py": 1,
        "git_commit.py": 1,
        "git_push.py": 1,
        "terminal_exec.py": 1,
        "terminal_stream.py": 1,
        "sandbox_common.py": 2,
        "workspace/_contract.py": 1,
    }

    for relative_path, expected_calls in callers.items():
        source = (block_root / relative_path).read_text(encoding="utf-8")
        assert source.count("authorize_legacy_coding_operation(") == expected_calls
        if relative_path == "git_commit.py":
            assert source.count("preflight_legacy_coding_operation(") == 1
            assert source.count("mutation_guard=canonical_mutation_guard") == 2
            assert source.index("preflight_legacy_coding_operation(") < source.index(
                "git_snapshot("
            )
            assert source.index("git_snapshot(") < source.index(
                "authorize_legacy_coding_operation("
            )
        else:
            assert source.count("mutation_guard=canonical_mutation_guard") == expected_calls


def test_git_commit_has_distinct_preflight_snapshot_and_one_shot_authorize_phases(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from blocks.coding import git_commit
    from blocks.coding._approval import approval_required
    from domain.coding import contract_adapter
    from domain.safety import approval

    approval.reset_approval_state_for_tests()
    base_input = {
        "workspace_id": "workspace-1",
        "message": "commit once",
        "paths": ["src/app.py"],
    }
    approval_request = approval_required(
        "git.commit",
        "high",
        args=base_input,
        message=base_input["message"],
        tool_name="coding_git_commit",
    )
    decision = approval.approve(approval_request["approval_request_id"])
    assert decision["approved"] is True
    input_data = {**base_input, "approval_token": decision["token"]}
    context = {"principal_id": "agent-1", "session_id": "session-1"}
    snapshot = {
        "expected_head": "head-1",
        "expected_tree": "tree-1",
        "expected_status_hash": "status-1",
        "expected_mount_revision": 7,
    }
    events: list[str] = []
    guard_calls: list[dict[str, Any]] = []
    preflight_calls: list[dict[str, Any]] = []
    authorize_calls: list[dict[str, Any]] = []
    snapshot_calls: list[str] = []
    provider_calls: list[str] = []

    real_preflight = git_commit.preflight_legacy_coding_operation
    real_authorize = git_commit.authorize_legacy_coding_operation

    def preflight(**kwargs: Any) -> dict[str, Any]:
        events.append("preflight")
        preflight_calls.append(dict(kwargs))
        return real_preflight(**kwargs)

    def authorize(**kwargs: Any) -> dict[str, Any]:
        events.append("final_authorize")
        authorize_calls.append(dict(kwargs))
        return real_authorize(**kwargs)

    def mutation_guard(
        selected_workspace_id: str,
        request: dict[str, Any],
        seen_context: dict[str, Any] | None,
        operation: str,
    ) -> None:
        events.append("lease_guard")
        guard_calls.append(
            {
                "workspace_id": selected_workspace_id,
                "request": dict(request),
                "context": dict(seen_context or {}),
                "operation": operation,
            }
        )

    def invoke(contract_id: str, operation: str, payload: dict[str, Any]) -> dict[str, Any]:
        if contract_id == contract_adapter.HOST_AUTHORITY:
            events.append("host_authorize")
            provider_calls.append("host_authorize")
            assert operation == "authorize"
            assert payload["workspace_id"] == "workspace-1"
            assert payload["arguments"] == {
                "message": "commit once",
                "paths": ["src/app.py"],
                "all_tracked": False,
                **snapshot,
            }
            return {"authorized": True, "receipt": "receipt-1"}
        if contract_id == contract_adapter.GIT_WRITE:
            events.append("git_write")
            provider_calls.append("git_write")
            return {"commit_hash": "commit-1"}
        raise AssertionError(f"unexpected coding contract: {contract_id} {operation}")

    def read_snapshot(selected_workspace_id: str) -> dict[str, Any]:
        events.append("snapshot")
        snapshot_calls.append(selected_workspace_id)
        return dict(snapshot)

    monkeypatch.setattr(git_commit, "preflight_legacy_coding_operation", preflight)
    monkeypatch.setattr(git_commit, "authorize_legacy_coding_operation", authorize)
    monkeypatch.setattr(git_commit, "canonical_mutation_guard", mutation_guard)
    monkeypatch.setattr(git_commit, "git_snapshot", read_snapshot)
    monkeypatch.setattr(git_commit, "invoke_coding_contract", invoke)
    monkeypatch.setattr("domain.coding.contract_adapter._profile_id", lambda: "profile-1")
    monkeypatch.setattr("domain.coding.contract_adapter.invoke_coding_contract", invoke)
    monkeypatch.setattr(git_commit, "record_attempt", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(git_commit, "record_execution", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(git_commit, "record_failure", lambda *_args, **_kwargs: None)

    first = git_commit.run(input_data, context)

    assert first["status"] == "ok"
    assert events == [
        "preflight",
        "lease_guard",
        "snapshot",
        "final_authorize",
        "lease_guard",
        "host_authorize",
        "git_write",
    ]
    assert len(preflight_calls) == 1
    assert len(authorize_calls) == 1
    assert len(guard_calls) == 2
    assert guard_calls[0] == guard_calls[1]
    assert guard_calls[0]["workspace_id"] == "workspace-1"
    assert guard_calls[0]["context"] == context
    assert guard_calls[0]["operation"] == "git.commit"
    assert snapshot_calls == ["workspace-1"]
    assert authorize_calls[0]["selected_workspace_id"] == "workspace-1"
    assert authorize_calls[0]["context"] == context
    assert authorize_calls[0]["arguments"] == {
        "message": "commit once",
        "paths": ["src/app.py"],
        "all_tracked": False,
        **snapshot,
    }
    assert provider_calls == ["host_authorize", "git_write"]

    events.clear()
    second = git_commit.run(input_data, context)

    assert second["status"] == "error"
    assert second["error"]["code"] == "APPROVAL_TOKEN_USED"
    assert events == ["preflight"]
    assert len(preflight_calls) == 2
    assert len(authorize_calls) == 1
    assert len(guard_calls) == 2
    assert snapshot_calls == ["workspace-1"]
    assert provider_calls == ["host_authorize", "git_write"]
