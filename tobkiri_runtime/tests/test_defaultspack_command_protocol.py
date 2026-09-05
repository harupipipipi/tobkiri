from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

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
from tests.conformance_support.command_protocol_activation import (  # noqa: E402
    command_protocol_binding_findings,
    load_current_signed_application_bindings,
    route_pattern_exposes_command_protocol,
)
from transport.registry import (  # noqa: E402
    canonical_http_route_specs,
)


def test_resolved_catalog_projects_all_legacy_commands_to_v1() -> None:
    catalog = CommandProtocolRegistry(DEFAULTSPACK_ROOT).catalog()

    assert catalog["api_version"] == "tobkiri.commands/v1"
    assert len(catalog["commands"]) == 55
    assert len({item["canonical_id"] for item in catalog["commands"]}) == 55


def test_pack_generation_reads_the_canonical_v4_manifest(tmp_path: Path) -> None:
    """The command generation pin remains usable without legacy ecosystem.json."""
    for relative in (
        Path("pack.v4.json"),
        Path("commands/default_commands.json"),
        Path("schemas/command-protocol-v1.schema.json"),
    ):
        destination = tmp_path / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(DEFAULTSPACK_ROOT / relative, destination)

    registry = CommandProtocolRegistry(tmp_path)
    first = registry._pack_generation()
    assert first > 0

    manifest = tmp_path / "pack.v4.json"
    manifest.write_text(
        manifest.read_text(encoding="utf-8") + "\n",
        encoding="utf-8",
    )
    assert registry._pack_generation() != first


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
    assert {item["execution"]["kind"] for item in catalog["commands"]} <= {
        "state_mutation",
        "host_operation",
        "pack_operation",
    }
    assert {item["presentation"]["input"]["kind"] for item in catalog["commands"]} <= {
        "search_select",
        "select",
        "toggle",
        "action",
        "form",
    }
    assert all("legacy_type" not in item["execution"] for item in catalog["commands"])
    assert all("legacy" not in item for item in catalog["commands"])

    by_id = {item["identity"]["id"]: item for item in catalog["commands"]}
    assert by_id["deepthink"]["presentation"]["input"]["kind"] == "toggle"
    assert by_id["deepthink"]["execution"]["kind"] == "state_mutation"
    assert by_id["model"]["presentation"]["input"]["kind"] == "search_select"
    assert (
        by_id["model"]["presentation"]["input"]["datasource_ref"]
        == "tobkiri:model_catalog"
    )
    assert by_id["home_title"]["execution"]["operation_ref"] == "host:set_home_title"
    assert by_id["home_title"]["presentation"]["input"]["kind"] == "form"
    assert by_id["home_title"]["presentation"]["input"]["fields"][0]["placeholder"] == {
        "fallback": "表示したい文字を入力"
    }


def test_owner_scope_comes_only_from_trusted_context() -> None:
    registry = CommandProtocolRegistry(DEFAULTSPACK_ROOT)

    assert (
        registry.owner_key(
            {},
            {"authenticated_principal_id": "alice", "authorized_profile_id": "work"},
        )
        == "alice:work"
    )
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


def test_resolved_catalog_exposes_high_risk_commands_to_the_host_adapter() -> None:
    catalog = CommandProtocolRegistry(DEFAULTSPACK_ROOT).catalog()
    unavailable = [
        item
        for item in catalog["commands"]
        if item["availability"]["status"] == "unavailable"
    ]
    high_risk = [
        item
        for item in catalog["commands"]
        if item["authorization"]["approval_required"]
    ]

    assert unavailable == []
    assert {item["identity"]["id"] for item in high_risk} == {
        "commit",
        "patch",
        "push",
        "restore",
        "terminal",
    }
    assert all(item["availability"] == {"status": "available"} for item in high_risk)
    assert not any(item["code"] == "handler_missing" for item in catalog["diagnostics"])


def test_all_55_commands_have_authority_and_completion_conformance() -> None:
    matrix = CommandProtocolRegistry(DEFAULTSPACK_ROOT).conformance_matrix()

    assert len(matrix) == 55
    assert len({item["command_id"] for item in matrix}) == 55
    assert all(item["operation_ref"] for item in matrix)
    assert all(item["completion_semantics"] != "noop" for item in matrix)
    high_risk = [item for item in matrix if item["authority"]["approval_required"]]
    assert len(high_risk) == 5
    assert all(item["authority"]["permissions"] for item in high_risk)
    assert all(
        item["completion_semantics"] == "backend_side_effect" for item in high_risk
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
        item
        for item in protocol.catalog()["commands"]
        if item["identity"]["name"] == "go"
    )
    result = protocol.invoke(
        {
            "command_ref": command["canonical_id"],
            "args": {"enabled": True},
            "mode": "chat",
        }
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


def test_high_risk_command_requires_the_captured_host_adapter(
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
    durable_secret = "durable-raw-execution-secret-62e6099b"
    payload = {
        "command_ref": "defaultspack:terminal",
        "args": {"cmd": f"python -c \"print('{durable_secret}')\""},
        "conversation_id": "conversation-1",
        "invocation_id": "terminal-approval-1",
        "mode": "coding",
    }

    trusted_context = {
        "workspace_path": str(workspace),
        "authorized_workspace_roots": [str(workspace)],
    }
    result = protocol.invoke(payload, trusted_context)

    assert result["status"] == "failed"
    assert result["error"]["code"] == "HIGH_RISK_COMMAND_ADAPTER_REQUIRED"
    assert "approval" not in result
    assert durable_secret not in json.dumps(result, sort_keys=True)


def test_high_risk_executor_policy_requires_the_captured_host_adapter(
    tmp_path: Path,
) -> None:
    protocol = CommandProtocolRegistry(DEFAULTSPACK_ROOT)
    result = protocol._enforce_runtime_authority(
        {
            "canonical_id": "defaultspack:terminal",
            "execution": {"operation_ref": "host:request_terminal_approval"},
            "authorization": {"executor_policy_ref": "tobkiri.command.human_approved"},
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
    assert result["status"] == "failed"
    assert result["error"]["code"] == "HIGH_RISK_COMMAND_ADAPTER_REQUIRED"


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
    assert Path(approved["argv"][0]).name == "true"
    assert approved["argv"][0] == approved["executable"]["path"]
    assert approved["executable"]["sha256"].startswith("sha256:")
    assert approved["plan_sha256"] != changed["plan_sha256"]

    patch_text = (
        "diff --git a/new.txt b/new.txt\n"
        "new file mode 100644\n"
        "index 0000000..3e75765\n"
        "--- /dev/null\n"
        "+++ b/new.txt\n"
        "@@ -0,0 +1 @@\n"
        "+new\n"
    )
    patch = protocol.operations.prepare_high_risk_plan(
        "request_patch_approval",
        {"patch": patch_text},
        context,
    )
    restore = protocol.operations.prepare_high_risk_plan(
        "request_restore_approval",
        {"paths": "tracked.txt"},
        context,
    )
    assert Path(patch["argv"][0]).name == "git"
    assert patch["argv"][1] == "apply"
    assert Path(restore["argv"][0]).name == "git"
    assert restore["argv"][1:3] == ["restore", "--worktree"]
    applied = protocol.operations._execute_high_risk_host_operation(
        {"id": "patch"},
        "request_patch_approval",
        {"patch": patch_text},
        {**context, "_approved_operation_plan": patch},
    )
    assert applied["status"] == "ok"
    assert (workspace / "new.txt").read_text(encoding="utf-8") == "new\n"


def test_high_risk_terminal_rejects_path_executable_swap_after_approval(
    tmp_path: Path,
    monkeypatch,
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
    (workspace / "tracked.txt").write_text("seed\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(workspace), "add", "tracked.txt"], check=True)
    subprocess.run(
        ["git", "-C", str(workspace), "commit", "-qm", "seed"],
        check=True,
    )
    executable_a_dir = tmp_path / "bin-a"
    executable_b_dir = tmp_path / "bin-b"
    executable_a_dir.mkdir()
    executable_b_dir.mkdir()
    executable_name = "python.exe" if os.name == "nt" else "python"
    executable_a = executable_a_dir / executable_name
    executable_b = executable_b_dir / executable_name
    shutil.copy2(sys.executable, executable_a)
    shutil.copy2(sys.executable, executable_b)
    executable_a.chmod(0o755)
    executable_b.chmod(0o755)
    original_path = os.environ.get("PATH", os.defpath)
    operations = CommandProtocolRegistry(DEFAULTSPACK_ROOT).operations
    context = {
        "workspace_path": str(workspace),
        "authorized_workspace_roots": [str(workspace)],
    }

    monkeypatch.setenv(
        "PATH",
        os.pathsep.join((str(executable_a_dir), original_path)),
    )
    approved = operations.prepare_high_risk_plan(
        "request_terminal_approval",
        {"cmd": "python -c \"open('ran.txt', 'w').write('ran')\""},
        context,
    )
    monkeypatch.setenv(
        "PATH",
        os.pathsep.join((str(executable_b_dir), original_path)),
    )
    result = operations._execute_high_risk_host_operation(
        {"id": "terminal"},
        "request_terminal_approval",
        {"cmd": "python -c \"open('ran.txt', 'w').write('ran')\""},
        {**context, "_approved_operation_plan": approved},
    )

    assert approved["argv"][0] == str(executable_a.resolve())
    assert result["status"] == "error"
    assert result["error"]["code"] == "APPROVED_OPERATION_PLAN_CHANGED"
    assert not (workspace / "ran.txt").exists()


def test_high_risk_host_policy_requires_authoritative_roots_and_executable(
    tmp_path: Path,
    monkeypatch,
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
    (workspace / "tracked.txt").write_text("seed\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(workspace), "add", "tracked.txt"], check=True)
    subprocess.run(
        ["git", "-C", str(workspace), "commit", "-qm", "seed"],
        check=True,
    )
    operations = CommandProtocolRegistry(DEFAULTSPACK_ROOT).operations

    with pytest.raises(ValueError, match="authorized_workspace_roots"):
        operations.prepare_high_risk_plan(
            "request_terminal_approval",
            {"cmd": "true"},
            {"workspace_path": str(workspace)},
        )

    with pytest.raises(ValueError, match="not allowlisted"):
        operations.prepare_high_risk_plan(
            "request_terminal_approval",
            {"cmd": "/bin/rm tracked.txt"},
            {
                "workspace_path": str(workspace),
                "authorized_workspace_roots": [str(workspace)],
            },
        )

    host_only_secret = "host-environment-secret-43de6ab0"
    monkeypatch.setenv("UNTRUSTED_COMMAND_SECRET", host_only_secret)
    environment_probe = operations._run_host_process(
        argv=(
            "python",
            "-c",
            ("import os; print(os.environ.get('UNTRUSTED_COMMAND_SECRET', 'absent'))"),
        ),
        cwd=workspace,
        stdin=None,
        timeout_seconds=10,
        command_class="terminal",
        allowed_cwds=(workspace,),
    )
    assert environment_probe.exit_code == 0
    assert environment_probe.stdout.strip() == "absent"
    assert host_only_secret not in environment_probe.stdout
    with pytest.raises(ValueError, match="not allowlisted"):
        operations.prepare_high_risk_plan(
            "request_terminal_approval",
            {"cmd": "/tmp/git status"},
            {
                "workspace_path": str(workspace),
                "authorized_workspace_roots": [str(workspace)],
            },
        )


@pytest.mark.skipif(sys.platform != "win32", reason="real Windows environment test")
def test_windows_host_process_gets_required_curated_environment(
    tmp_path: Path,
) -> None:
    operations = CommandProtocolRegistry(DEFAULTSPACK_ROOT).operations
    probe = operations._run_host_process(
        argv=(
            "python",
            "-c",
            (
                "import json, os; "
                "print(json.dumps({'path': os.environ.get('PATH'), "
                "'system_root': os.environ.get('SystemRoot')}))"
            ),
        ),
        cwd=tmp_path,
        stdin=None,
        timeout_seconds=10,
        command_class="terminal",
        allowed_cwds=(tmp_path,),
    )

    assert probe.exit_code == 0
    environment = json.loads(probe.stdout)
    assert environment["system_root"] == str(Path(os.environ["SystemRoot"]).resolve())
    path_entries = environment["path"].split(os.pathsep)
    assert path_entries
    assert all(
        entry and entry != "." and Path(entry).is_absolute() for entry in path_entries
    )


def test_only_captured_command_protocol_route_is_not_legacy_transport() -> None:
    legacy_specs = canonical_http_route_specs(include_always_available=True)
    bindings = load_current_signed_application_bindings()

    assert not any(
        route_pattern_exposes_command_protocol(spec.pattern) for spec in legacy_specs
    )
    assert command_protocol_binding_findings(bindings) == []

    high_risk = next(
        binding for binding in bindings if "command-protocol" in binding.path
    )
    assert command_protocol_binding_findings(
        (replace(high_risk, path="/api/command-protocol/v1/invoke"),)
    )
    assert command_protocol_binding_findings(
        (
            replace(
                high_risk,
                targets=(
                    replace(high_risk.targets[0], function_id="untrusted.function"),
                ),
            ),
        )
    )


def test_interactive_command_routes_are_captured_host_contract_operations() -> None:
    """The Composer's adapter calls resolve only through the signed map."""

    bindings = load_current_signed_application_bindings()
    expected = {
        ("GET", "/api/interactive-approval/v1/list"): (
            "tobkiri.service.interactive-approval.v1",
            "interactive_approval.list",
            "rumi_host_authority_bridge_pack.host-authority.interactive-approval",
            frozenset(),
        ),
        ("POST", "/api/interactive-approval/v1/get"): (
            "tobkiri.service.interactive-approval.v1",
            "interactive_approval.get",
            "rumi_host_authority_bridge_pack.host-authority.interactive-approval",
            frozenset({"request_id"}),
        ),
        ("POST", "/api/interactive-approval/v1/approve"): (
            "tobkiri.service.interactive-approval.v1",
            "interactive_approval.approve",
            "rumi_host_authority_bridge_pack.host-authority.interactive-approval",
            frozenset({"request_id", "confirmation_text", "ui_operator"}),
        ),
        ("POST", "/api/interactive-approval/v1/deny"): (
            "tobkiri.service.interactive-approval.v1",
            "interactive_approval.deny",
            "rumi_host_authority_bridge_pack.host-authority.interactive-approval",
            frozenset({"request_id", "ui_operator"}),
        ),
        ("POST", "/api/command-protocol/v1/high-risk"): (
            "tobkiri.service.command.high-risk.v1",
            "high_risk_command.manage",
            "rumi_command_protocol_pack.high-risk-command.service",
            frozenset(
                {"phase", "invocation_id", "command_ref", "arguments", "presentation"}
            ),
        ),
    }

    captured = {
        (binding.method, binding.path): binding
        for binding in bindings
        if binding.path.startswith("/api/interactive-approval/")
        or binding.path == "/api/command-protocol/v1/high-risk"
    }

    assert set(captured) == set(expected)
    for key, (contract_id, operation_id, function_id, allowed_keys) in expected.items():
        binding = captured[key]
        assert binding.presentation == "broker_result"
        assert len(binding.targets) == 1
        target = binding.targets[0]
        assert target.contract_id == contract_id
        assert target.operation_id == operation_id
        assert target.function_id == function_id
        assert target.allowed_payload_keys == allowed_keys


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
