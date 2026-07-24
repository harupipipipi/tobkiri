from __future__ import annotations

import sys
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parent.parent
DEFAULTSPACK_ROOT = ROOT / "ecosystem" / "defaultspack"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(DEFAULTSPACK_ROOT))

from domain.frontend.command_protocol import (  # noqa: E402
    CommandProtocolRegistry,
    CommandProtocolSchemaError,
    validate_protocol_document,
)
from transport.registry import (  # noqa: E402
    canonical_http_route_specs,
    require_legacy_route_allowlisted,
)


def test_resolved_catalog_projects_all_legacy_commands_to_v1() -> None:
    catalog = CommandProtocolRegistry(DEFAULTSPACK_ROOT).catalog()

    assert catalog["api_version"] == "tobkiri.commands/v1"
    assert len(catalog["commands"]) == 55
    assert len({item["canonical_id"] for item in catalog["commands"]}) == 55


def test_all_command_bindings_are_concretely_probed_and_pack_blocks_execute(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv(
        "RUMI_DEFAULTSPACK_FRONTEND_SETTINGS_PATH",
        str(tmp_path / "settings.json"),
    )
    protocol = CommandProtocolRegistry(DEFAULTSPACK_ROOT)

    catalog = protocol.catalog()
    matrix = protocol.conformance_matrix()
    fast = protocol.invoke(
        {
            "command_ref": "defaultspack:fast",
            "args": {"enabled": True},
            "mode": "chat",
        }
    )

    assert len(matrix) == 55
    assert all(item["verified_handler"] is True for item in matrix)
    assert all(item["concrete_binding"] for item in matrix)
    assert fast["status"] == "succeeded"
    assert {
        item["execution"]["kind"] for item in catalog["commands"]
    } <= {"state_mutation", "host_operation", "pack_operation"}
    assert {
        item["presentation"]["input"]["kind"] for item in catalog["commands"]
    } <= {"search_select", "select", "toggle", "action", "form"}
    assert all(
        "legacy_type" not in item["execution"]
        for item in catalog["commands"]
    )
    assert all("legacy" not in item for item in catalog["commands"])

    by_id = {item["identity"]["id"]: item for item in catalog["commands"]}
    assert by_id["deepthink"]["presentation"]["input"]["kind"] == "toggle"
    assert by_id["deepthink"]["execution"]["kind"] == "state_mutation"
    assert by_id["model"]["presentation"]["input"]["kind"] == "search_select"
    assert by_id["model"]["presentation"]["input"]["datasource_ref"] == "tobkiri:model_catalog"
    assert by_id["home_title"]["execution"]["operation_ref"] == "host:set_home_title"
    assert by_id["home_title"]["presentation"]["input"]["kind"] == "form"
    assert by_id["home_title"]["presentation"]["input"]["fields"][0]["placeholder"] == {
        "fallback": "表示したい文字を入力"
    }


def test_owner_scope_comes_only_from_trusted_context() -> None:
    registry = CommandProtocolRegistry(DEFAULTSPACK_ROOT)

    assert registry.owner_key(
        {},
        {"authenticated_principal_id": "alice", "authorized_profile_id": "work"},
    ) == "alice:work"
    with pytest.raises(ValueError, match="reserved transport fields"):
        registry.owner_key(
            {"_owner_key": "bob:default"},
            {"authenticated_principal_id": "alice"},
        )
    with pytest.raises(ValueError, match="reserved transport fields"):
        registry.owner_key(
            {"principal_id": "bob"},
            {"authenticated_principal_id": "alice"},
        )
    with pytest.raises(ValueError, match="not authorized"):
        registry.owner_key(
            {"profile_id": "admin"},
            {
                "authenticated_principal_id": "alice",
                "authorized_profile_id": "work",
            },
        )


def test_resolved_catalog_never_silently_exposes_missing_frontend_handler() -> None:
    catalog = CommandProtocolRegistry(DEFAULTSPACK_ROOT).catalog()
    unavailable = [
        item
        for item in catalog["commands"]
        if item["availability"]["status"] == "unavailable"
    ]

    assert unavailable == []
    assert not any(
        item["code"] == "handler_missing"
        for item in catalog["diagnostics"]
    )


def test_all_55_commands_have_authority_and_completion_conformance() -> None:
    matrix = CommandProtocolRegistry(DEFAULTSPACK_ROOT).conformance_matrix()

    assert len(matrix) == 55
    assert len({item["command_id"] for item in matrix}) == 55
    assert all(item["operation_ref"] for item in matrix)
    assert all(item["completion_semantics"] != "noop" for item in matrix)
    high_risk = [
        item for item in matrix if item["authority"]["approval_required"]
    ]
    assert len(high_risk) == 5
    assert all(item["authority"]["permissions"] for item in high_risk)
    assert all(
        item["completion_semantics"] == "backend_side_effect"
        for item in high_risk
    )


def test_protocol_deepthink_invocation_returns_authoritative_state(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv(
        "RUMI_DEFAULTSPACK_FRONTEND_SETTINGS_PATH",
        str(tmp_path / "frontend_settings.json"),
    )
    protocol = CommandProtocolRegistry(DEFAULTSPACK_ROOT)

    enabled = protocol.invoke(
        {
            "command_ref": "defaultspack:deepthink",
            "args": {"enabled": True},
            "mode": "chat",
            "invocation_id": "deepthink-protocol-1",
            "idempotency_key": "deepthink-protocol-1",
            "expected_revision": 0,
        }
    )
    disabled = protocol.invoke(
        {
            "command_ref": "defaultspack:deepthink",
            "args": {"enabled": False},
            "mode": "chat",
            "invocation_id": "deepthink-protocol-2",
            "idempotency_key": "deepthink-protocol-2",
            "expected_revision": 1,
        }
    )

    assert enabled["status"] == "succeeded"
    assert enabled["state_changes"][0]["value"] is True
    assert enabled["state_changes"][0]["revision"] == 1
    assert disabled["state_changes"][0]["value"] is False
    assert disabled["state_changes"][0]["revision"] == 2


def test_home_title_invocation_returns_frontend_action() -> None:
    result = CommandProtocolRegistry(DEFAULTSPACK_ROOT).invoke(
        {
            "command_ref": "defaultspack:home_title",
            "args": {"value": "My Tobkiri"},
        }
    )

    assert result["status"] == "succeeded"
    assert result["legacy_result"]["action"] == "set_home_title"
    assert result["legacy_result"]["args"] == {"value": "My Tobkiri"}
    assert result["progress"]["status"] == "completed"
    assert result["progress"]["terminal"] is True


def test_protocol_invocation_events_can_resume_after_last_event_id(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv(
        "RUMI_DEFAULTSPACK_FRONTEND_SETTINGS_PATH",
        str(tmp_path / "frontend_settings.json"),
    )
    protocol = CommandProtocolRegistry(DEFAULTSPACK_ROOT)

    result = protocol.invoke(
        {
            "command_ref": "defaultspack:home_title",
            "args": {"value": "Resumable"},
            "invocation_id": "resume-protocol-1",
        }
    )
    resumed = protocol.events.resume("resume-protocol-1", after_sequence=1)

    assert result["progress"]["last_sequence"] == 3
    assert [event["type"] for event in resumed] == ["validating", "completed"]


def test_model_datasource_returns_standard_option_items() -> None:
    result = CommandProtocolRegistry(DEFAULTSPACK_ROOT).query_datasource(
        {"datasource_ref": "tobkiri:model_catalog", "query": "stub", "limit": 10}
    )

    assert result["status"] == "succeeded"
    assert result["items"]
    item = result["items"][0]
    assert item["value"]
    assert item["label"]["fallback"]
    assert "provider_id" in item["metadata"]


def test_provider_datasource_uses_same_option_item_contract() -> None:
    result = CommandProtocolRegistry(DEFAULTSPACK_ROOT).query_datasource(
        {"datasource_ref": "tobkiri:provider_catalog", "limit": 100}
    )

    assert result["status"] == "succeeded"
    assert result["items"]
    assert all(item["value"] and item["label"]["fallback"] for item in result["items"])
    assert all("model_count" in item["metadata"] for item in result["items"])


def test_protocol_schema_rejects_unknown_normative_fields_and_major() -> None:
    catalog = CommandProtocolRegistry(DEFAULTSPACK_ROOT).catalog()
    catalog["unexpected"] = True
    try:
        validate_protocol_document(catalog)
    except CommandProtocolSchemaError:
        pass
    else:
        raise AssertionError("unknown normative field must be rejected")

    catalog.pop("unexpected")
    catalog["api_version"] = "tobkiri.commands/v2"
    try:
        validate_protocol_document(catalog)
    except CommandProtocolSchemaError:
        pass
    else:
        raise AssertionError("unsupported major must be rejected")


def test_settings_registered_command_is_resolved_and_invoked_through_protocol(
    tmp_path: Path, monkeypatch
) -> None:
    settings_path = tmp_path / "frontend_settings.json"
    settings_path.write_text(
        json.dumps(
            {
                "commands": {
                    "registered_slash_commands": [
                        {
                            "name": "go",
                            "action": "toggle_yolo",
                            "aliases": ["ship it"],
                        }
                    ]
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("RUMI_DEFAULTSPACK_FRONTEND_SETTINGS_PATH", str(settings_path))
    protocol = CommandProtocolRegistry(DEFAULTSPACK_ROOT)

    command = next(
        item for item in protocol.catalog()["commands"] if item["identity"]["name"] == "go"
    )
    result = protocol.invoke(
        {"command_ref": command["canonical_id"], "args": {"enabled": True}, "mode": "chat"}
    )

    legacy = next(
        item
        for item in protocol.legacy_read_projection()
        if item["canonical_id"] == command["canonical_id"]
    )
    assert legacy["source"] == "settings.registered_slash_commands"
    assert command["presentation"]["input"]["kind"] == "toggle"
    assert command["identity"]["aliases"] == ["ship_it"]
    assert result["status"] == "succeeded"
    assert result["legacy_result"]["action"] == "toggle_yolo"


def test_high_risk_command_requires_one_shot_approval_resume(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv(
        "RUMI_DEFAULTSPACK_APPROVAL_DB_PATH",
        str(tmp_path / "approval.sqlite3"),
    )
    monkeypatch.setenv(
        "RUMI_DEFAULTSPACK_APPROVAL_SECRET_PATH",
        str(tmp_path / "approval.secret"),
    )
    monkeypatch.setenv(
        "RUMI_DEFAULTSPACK_FRONTEND_SETTINGS_PATH",
        str(tmp_path / "frontend_settings.json"),
    )
    monkeypatch.setenv("RUMI_AUTHORITY_MODE", "off")
    from domain.safety.approval import approve, reset_approval_state_for_tests

    reset_approval_state_for_tests()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    subprocess.run(["git", "init", "-q", str(workspace)], check=True)
    subprocess.run(
        ["git", "-C", str(workspace), "config", "user.email", "test@example.com"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(workspace), "config", "user.name", "Test"],
        check=True,
    )
    (workspace / "seed.txt").write_text("seed\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(workspace), "add", "seed.txt"], check=True)
    subprocess.run(
        ["git", "-C", str(workspace), "commit", "-qm", "seed"],
        check=True,
    )
    protocol = CommandProtocolRegistry(DEFAULTSPACK_ROOT)
    payload = {
        "command_ref": "defaultspack:terminal",
        "args": {"cmd": "python -c \"print('approved')\""},
        "conversation_id": "conversation-1",
        "invocation_id": "terminal-approval-1",
        "mode": "coding",
    }

    trusted_context = {
        "workspace_path": str(workspace),
        "authorized_workspace_roots": [str(workspace)],
    }
    pending = protocol.invoke(payload, trusted_context)
    decision = approve(pending["approval"]["request_id"])
    resumed = protocol.invoke(
        {
            **payload,
            "approval_token": decision["token"],
        },
        trusted_context,
    )
    replay = protocol.invoke(
        {
            **payload,
            "invocation_id": "terminal-approval-2",
            "approval_token": decision["token"],
        },
        trusted_context,
    )

    assert pending["status"] == "approval_required"
    assert pending["approval"]["request_id"].startswith("apr_")
    assert resumed["status"] == "succeeded"
    assert resumed["legacy_result"]["action"] == "request_terminal_approval"
    assert resumed["legacy_result"]["executed"] is True
    assert "approved" in resumed["legacy_result"]["stdout"]
    assert replay["status"] == "failed"
    assert replay["error"]["code"] in {
        "APPROVAL_ARGUMENTS_CHANGED",
        "APPROVAL_TOKEN_ARGUMENTS_MISMATCH",
        "APPROVAL_TOKEN_USED",
    }


def test_high_risk_executor_policy_calls_runtime_authority(
    tmp_path: Path,
    monkeypatch,
) -> None:
    captured: dict[str, object] = {}

    class Authority:
        def check(self, **kwargs):
            captured.update(kwargs)
            return SimpleNamespace(
                allowed=False,
                approval_required=True,
                request_id="auth-1",
                permission_id="host.process.exec_guarded",
                reason="approval required",
            )

    monkeypatch.setattr(
        "core_runtime.authority.get_authority_service",
        lambda: Authority(),
    )
    monkeypatch.setenv(
        "RUMI_DEFAULTSPACK_FRONTEND_SETTINGS_PATH",
        str(tmp_path / "settings.json"),
    )
    protocol = CommandProtocolRegistry(DEFAULTSPACK_ROOT)
    result = protocol._enforce_runtime_authority(
        {
            "canonical_id": "defaultspack:terminal",
            "execution": {"operation_ref": "host:request_terminal_approval"},
            "authorization": {
                "executor_policy_ref": "tobkiri.command.human_approved"
            },
        },
        {"invocation_id": "inv-1", "conversation_id": "conversation-1"},
        {},
        {"_trusted_owner_key": "alice:profile-a"},
        {
            "plan_sha256": "abc",
            "cwd": str(tmp_path),
            "argv": ["true"],
        },
    )

    assert result is not None
    assert result["status"] == "approval_required"
    assert result["approval"]["kind"] == "authority"
    assert captured["permission_id"] == "host.process.exec_guarded"
    assert captured["principal_id"] == "alice"


def test_high_risk_operation_plan_binds_workspace_and_git_state(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    subprocess.run(["git", "init", "-q", str(workspace)], check=True)
    subprocess.run(
        ["git", "-C", str(workspace), "config", "user.email", "test@example.com"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(workspace), "config", "user.name", "Test"],
        check=True,
    )
    tracked = workspace / "tracked.txt"
    tracked.write_text("before\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(workspace), "add", "tracked.txt"], check=True)
    subprocess.run(
        ["git", "-C", str(workspace), "commit", "-qm", "seed"],
        check=True,
    )
    protocol = CommandProtocolRegistry(DEFAULTSPACK_ROOT)
    context = {
        "workspace_path": str(workspace),
        "authorized_workspace_roots": [str(workspace)],
    }

    approved = protocol.operations.prepare_high_risk_plan(
        "request_terminal_approval",
        {"cmd": "true"},
        context,
    )
    tracked.write_text("after\n", encoding="utf-8")
    changed = protocol.operations.prepare_high_risk_plan(
        "request_terminal_approval",
        {"cmd": "true"},
        context,
    )

    assert approved["cwd"] == str(workspace.resolve())
    assert approved["argv"] == ["true"]
    assert approved["plan_sha256"] != changed["plan_sha256"]


def test_command_protocol_routes_are_registered() -> None:
    specs = canonical_http_route_specs()
    routes = {(item.method, item.pattern) for item in specs}
    assert ("GET", "/api/command-protocol/v1/catalog") in routes
    assert ("POST", "/api/command-protocol/v1/invoke") in routes
    assert (
        "POST",
        "/api/command-protocol/v1/invocations/events/query",
    ) in routes
    assert (
        "GET",
        "/api/command-protocol/v1/invocations/{invocation_id}/events",
    ) in routes
    assert ("POST", "/api/command-protocol/v1/offline") in routes
    assert ("POST", "/api/command-protocol/v1/resume") in routes
    assert ("POST", "/api/command-protocol/v1/states/query") in routes
    assert ("POST", "/api/command-protocol/v1/datasources/query") in routes

    protocol_specs = [
        item for item in specs if item.pattern.startswith("/api/command-protocol/v1/")
    ]
    assert len(protocol_specs) == 8
    remotely_resumable = {
        "/api/command-protocol/v1/resume",
        "/api/command-protocol/v1/invocations/{invocation_id}/events",
    }
    assert all(
        not item.local_only
        for item in protocol_specs
        if item.pattern in remotely_resumable
    )
    for item in protocol_specs:
        require_legacy_route_allowlisted(item)


def test_invocation_id_is_idempotent_and_conflict_safe(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv(
        "RUMI_DEFAULTSPACK_FRONTEND_SETTINGS_PATH",
        str(tmp_path / "frontend_settings.json"),
    )
    registry = CommandProtocolRegistry(DEFAULTSPACK_ROOT)
    payload = {
        "command_ref": "defaultspack:help",
        "invocation_id": "inv-idempotent",
        "mode": "chat",
        "args": {},
    }

    first = registry.invoke(payload)
    replay = registry.invoke(payload)
    conflict = registry.invoke({**payload, "args": {"different": True}})

    assert first["status"] == "succeeded"
    assert replay["status"] == "succeeded"
    assert replay["operation_id"] == first["operation_id"]
    assert conflict["status"] == "failed"
    assert conflict["error"]["code"] == "INVOCATION_CONFLICT"
