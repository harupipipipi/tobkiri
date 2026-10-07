"""Real production capture and Broker with a selected declarative tool Pack.

This isolated Profile proves the owner edge, not ordinary Defaults or native UI.
"""

from pathlib import Path
import json
from typing import Any

import pytest

from core_runtime.authority.v4 import AuthorityDenied
from tests.conformance_support.host_profile import captured_host_profile
from tobkiri_host.errors import ProviderExecutionError


def _exclude_default_tools_incident_edges(monkeypatch: pytest.MonkeyPatch) -> None:
    """Close only this excluded-Pack fixture before its official Profile render."""
    from tests.conformance_support import host_profile
    from tests.conformance_support.packaged_profile import load_packaged_profile_catalog

    manifest = load_packaged_profile_catalog().packs["rumi_default_tools_pack"]
    function_ids = frozenset(item["id"] for item in manifest["functions"])
    assert "rumi_default_tools_pack.file-create-prepare.service" in function_ids
    official_render = host_profile.render

    def render_without_excluded_edges(**kwargs: Any) -> dict[Path, bytes]:
        intent_path = kwargs["intent_path"]
        intent = json.loads(intent_path.read_bytes())
        assert all(item["pack_id"] != "rumi_default_tools_pack" for item in intent["packs"])
        intent["requested_edges"] = [
            edge for edge in intent["requested_edges"]
            if edge["caller_function_id"] not in function_ids
            and edge["target_provider_id"] not in function_ids
        ]
        intent_path.write_text(json.dumps(intent, indent=2) + "\n")
        return official_render(**kwargs)

    monkeypatch.setattr(host_profile, "render", render_without_excluded_edges)


@pytest.mark.parametrize("selected", [True, False])
def test_production_registry_uses_only_selected_pack_data(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    selected: bool,
) -> None:
    contract = "tobkiri.resource.tool.definition.v1"
    operation = "rumi_tool_registry_pack.tool-definition-resource"
    if not selected:
        _exclude_default_tools_incident_edges(monkeypatch)
    with captured_host_profile(
        tmp_path,
        monkeypatch,
        packs=() if selected else ("rumi_default_tool_projection_pack",),
        edges=(),
        exclude_packs=() if selected else ("rumi_default_tools_pack",),
        exclude_callers=(
            ()
            if selected
            else (
                "rumi_tool_local_executor_pack.tool-executor.local",
                "rumi_default_tools_pack.files-read",
                "rumi_turn_runtime_pack.chat-message-deliver",
                "rumi_host_authority_bridge_pack.host-authority.approval-policy",
            )
        ),
        backends=(),
    ) as (session, _store):
        result = session.invoke(
            contract,
            operation,
            {
                "operation": "list",
                "_session_id": "registry-data",
            },
        )
        assert len(result["definitions"]) == (152 if selected else 119)
        assert not (tmp_path / "user-data/packs/rumi_tool_registry_pack").exists()
        assert {item["pack_id"] for item in result["pack_data_sources"]} == (
            {"defaultspack", "rumi_default_tools_pack"}
            if selected
            else {"defaultspack"}
        )
        identifiers = {item["tool_id"] for item in result["definitions"]}
        assert {
            "artifact_file_read",
            "memo_note_upsert",
            "settings_update",
        } <= identifiers
        assert ("calculator" in identifiers) is selected
        chat_ids = {"chat_list_targets", "chat_resolve_target", "chat_send_message"}
        assert (chat_ids <= identifiers) if selected else identifiers.isdisjoint(chat_ids)
        from core_runtime.bootstrap.profile_capture import capture_active_profile

        session.assert_current()
        active = capture_active_profile()
        assert active.resolved.profile["profile_id"] == session.profile_id
        selected_packs = {
            item["identity"] for item in active.resolved.lock["effective_set"]
        }
        assert ("rumi_default_tools_pack" in selected_packs) is selected
        session.assert_current()
        with pytest.raises(ProviderExecutionError):
            session.invoke(
                contract,
                operation,
                {
                    "operation": "list",
                    "pack_id": "foreign",
                    "_session_id": "registry-data",
                },
            )
        with pytest.raises(AuthorityDenied):
            session.invoke(
                "tobkiri.service.tool.local.operation.v1",
                "rumi_default_tool_projection_pack.default-tool-local-operation",
                {"_session_id": "registry-data"},
            )


@pytest.mark.parametrize("local_selected", [False, True])
def test_production_tool_broker_resolves_owner_and_rejects_unavailable_executor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    local_selected: bool,
) -> None:
    """Exercise real capture and nested authority without enabling legacy code."""
    from ecosystem.rumi_tool_broker_pack.runtime import broker

    factory = broker.HOST_PROVIDER_FACTORY
    routes = (
        ("shell.tauri.default", factory.function_id, broker.CONTRACT, broker.OPERATION),
        (
            factory.function_id,
            "rumi_tool_registry_pack.tool-registry.definition",
            broker.DEFINITION,
            "rumi_tool_registry_pack.tool-definition-resource",
        ),
        (
            factory.function_id,
            "rumi_tool_validation_pack.tool-validation.arguments",
            broker.VALIDATE,
            "rumi_tool_validation_pack.tool-arguments-validate",
        ),
        (
            factory.function_id,
            "rumi_tool_result_pack.tool-result.normalize",
            broker.NORMALIZE,
            "rumi_tool_result_pack.tool-result-normalize",
        ),
    )
    local = "rumi_tool_local_executor_pack.tool-executor.local"
    if local_selected:
        routes += (
            (
                factory.function_id,
                local,
                broker.EXECUTE,
                "rumi_tool_local_executor_pack.tool-local-execute",
            ),
            (
                local,
                "rumi_tool_registry_pack.tool-registry.definition",
                broker.DEFINITION,
                "rumi_tool_registry_pack.tool-definition-resource",
            ),
        )
    edges = [
        {
            "caller_function_id": caller,
            "target_provider_id": target,
            "contract_id": contract,
            "operation_id": operation,
            "authority_mode": "profile_grant",
            "requested_scope_template": {
                "capability": "operation.invoke",
                "dimensions": {"contract": [contract], "operation": [operation]},
                "quotas": {},
                "exact_request_digest": None,
                "opaque": False,
            },
        }
        for caller, target, contract, operation in routes
    ]
    with captured_host_profile(
        tmp_path,
        monkeypatch,
        packs=(),
        exclude_packs=(
            ()
            if local_selected
            else (
                "rumi_tool_local_executor_pack",
                "rumi_default_tool_projection_pack",
            )
        ),
        exclude_callers=(
            factory.function_id,
            local,
            "rumi_default_tools_pack.files-read",
            "rumi_default_tools_pack.file-create-tool",
            "rumi_default_tools_pack.chat-reference-tools",
            "rumi_default_tools_pack.chat-message-tool",
            "rumi_host_authority_bridge_pack.host-authority.approval-policy",
        ),
        edges=edges,
        backends=(),
    ) as (session, _store):
        session.assert_operation_ready(broker.CONTRACT, broker.OPERATION)
        request = {
            "tool_id": "file_reader",
            "tool_call_id": "call-1",
            "arguments": {"path": "readme.txt"},
            "_session_id": "tool-composition",
        }
        if local_selected:
            # A direct Shell call has no authenticated Saved request ancestry.
            result = session.invoke(broker.CONTRACT, broker.OPERATION, request)
            assert result["status"] == "error"
            assert result["is_error"] is True
            assert json.loads(result["result"])["error"]["code"] == (
                "ACTION_APPROVAL_POLICY_UNAVAILABLE"
            )
        else:
            with pytest.raises(
                ProviderExecutionError, match="provider execution failed"
            ) as failure:
                session.invoke(broker.CONTRACT, broker.OPERATION, request)
            assert isinstance(failure.value.__cause__, PermissionError)
            assert str(failure.value.__cause__) == "selected tool executor is unavailable"
        with pytest.raises(
            ProviderExecutionError, match="provider execution failed"
        ) as failure:
            session.invoke(
                broker.CONTRACT, broker.OPERATION, {**request, "arguments": {}}
            )
        assert isinstance(failure.value.__cause__, ValueError)
        assert str(failure.value.__cause__) == "tool arguments are invalid"
        with pytest.raises(
            ProviderExecutionError, match="provider execution failed"
        ) as failure:
            session.invoke(
                broker.CONTRACT, broker.OPERATION, {**request, "approved": True}
            )
        assert isinstance(failure.value.__cause__, ValueError)
        assert str(failure.value.__cause__) == "tool invocation payload is invalid"
        assert not (tmp_path / "user-data/packs/rumi_tool_registry_pack").exists()


@pytest.mark.parametrize(
    "tool_id,arguments",
    [
        ("calculator", {"expression": "2+2"}),
        ("coding_file_read", {"path": "hello.txt"}),
    ],
)
def test_direct_shell_finite_tools_stop_without_saved_ancestry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    tool_id: str,
    arguments: dict,
) -> None:
    """Keep direct Shell tools denied; real Saved/native success has separate proof."""
    from ecosystem.rumi_tool_broker_pack.runtime import broker
    from ecosystem.rumi_workspace_mount_pack.runtime.mounts import WorkspaceMountStore

    edge = {
        "caller_function_id": "shell.tauri.default",
        "target_provider_id": broker.HOST_PROVIDER_FACTORY.function_id,
        "contract_id": broker.CONTRACT,
        "operation_id": broker.OPERATION,
        "authority_mode": "profile_grant",
        "requested_scope_template": {
            "capability": "operation.invoke",
            "dimensions": {
                "contract": [broker.CONTRACT],
                "operation": [broker.OPERATION],
            },
            "quotas": {},
            "exact_request_digest": None,
            "opaque": False,
        },
    }
    with captured_host_profile(
        tmp_path,
        monkeypatch,
        packs=(),
        edges=[edge],
        backends=(),
    ) as (session, _store):
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        (workspace / "hello.txt").write_text("owned file content", encoding="utf-8")
        mounts = WorkspaceMountStore("defaults", user_data_root=tmp_path / "user-data")
        mounts.mount("owned", str(workspace), expected_revision=0)
        mounts.select("owned", expected_revision=1)
        result = session.invoke(
            broker.CONTRACT,
            broker.OPERATION,
            {
                "tool_id": tool_id,
                "tool_call_id": "finite-call",
                "arguments": arguments,
                "_session_id": "finite-owner-proof",
            },
        )
        assert result["status"] == "error"
        assert result["is_error"] is True
        assert json.loads(result["result"])["error"]["code"] == (
            "ACTION_APPROVAL_POLICY_UNAVAILABLE"
        )
        assert (workspace / "hello.txt").read_text(encoding="utf-8") == "owned file content"
        assert sorted(path.name for path in workspace.iterdir()) == ["hello.txt"]
