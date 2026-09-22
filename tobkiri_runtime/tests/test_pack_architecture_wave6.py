"""Focused contract tests for the Wave 6 tool-runtime ownership split."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from ecosystem.rumi_tool_approval_bridge_pack.runtime.bridge import (
    create_authorize_operation,
)
from ecosystem.rumi_tool_executor_selector_pack.runtime.selector import (
    create_select_operation,
)
from ecosystem.rumi_tool_guard_pack.runtime.guards import create_guard_operation
from ecosystem.rumi_tool_mcp_executor_pack.runtime.executor import (
    create_execute_operation as create_mcp_execute_operation,
)
from ecosystem.rumi_tool_policy_pack.runtime.policy import create_policy_operation
from ecosystem.rumi_tool_registry_pack.runtime.registry import (
    ToolDefinitionRegistry,
)
from ecosystem.rumi_tool_result_pack.runtime.normalizer import (
    create_normalize_operation,
)
from ecosystem.rumi_tool_validation_pack.runtime.validator import (
    create_validate_operation,
)
from core_runtime.global_contract_dispatch import GlobalContractUnavailable
from ecosystem.defaultspack.domain.tool.catalog_contract_client import (
    ContractToolCatalog,
)


def _definition(tool_id: str = "sample.read") -> dict:
    return {
        "tool_id": tool_id,
        "display_name": "Sample",
        "description": "Read sample data.",
        "input_schema": {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
            "additionalProperties": False,
        },
        "result_schema": {"type": "object"},
        "execution": {
            "kind": "capability",
            "contract_id": "sample.read",
            "provider_instance_id": "tool-executor.capability",
        },
        "authority": "file.read",
        "risk": "low",
        "policy_tags": ["read"],
        "aliases": ["sample.legacy_read"],
        "widget": {"kind": "text"},
        "source_adapter_id": "test.adapter",
    }


def test_registry_migration_preserves_ids_aliases_schemas_and_widgets(
    tmp_path: Path,
) -> None:
    registry = ToolDefinitionRegistry("default", user_data_root=tmp_path)
    definitions = [_definition()]
    aliases = {"legacy.sample": "sample.read"}
    source = {"definitions": definitions, "aliases": aliases}
    source_hash = hashlib.sha256(
        json.dumps(source, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()

    migrated = registry.migrate(definitions, aliases, source_hash)
    resolved = registry.resolve("legacy.sample")

    assert migrated["definitions"] == 1
    assert resolved is not None
    assert resolved["resolved_tool_id"] == "sample.read"
    assert resolved["definition"]["input_schema"]["required"] == ["path"]
    assert resolved["definition"]["widget"] == {"kind": "text"}


def test_registry_rejects_source_drift_and_marker_mismatch(tmp_path: Path) -> None:
    registry = ToolDefinitionRegistry("default", user_data_root=tmp_path)
    with pytest.raises(RuntimeError, match="source changed"):
        registry.migrate([_definition()], {}, "0" * 64)

    source = {"definitions": [_definition()], "aliases": {}}
    source_hash = hashlib.sha256(
        json.dumps(source, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    migrated = registry.migrate(source["definitions"], {}, source_hash)
    with pytest.raises(ValueError, match="marker mismatch"):
        registry.rollback_migration("migration-wrong")
    assert registry.rollback_migration(migrated["migration_id"])["rolled_back"]


def test_argument_validator_is_noncoercing_and_rejects_extra_fields() -> None:
    validate = create_validate_operation(None)
    schema = _definition()["input_schema"]
    wrong_type = validate("validate", {"schema": schema, "arguments": {"path": 3}})
    extra = validate(
        "validate",
        {"schema": schema, "arguments": {"path": "a", "approved": True}},
    )

    assert not wrong_type["valid"]
    assert not extra["valid"]
    assert wrong_type["coerced"] is False


def test_guard_order_and_cancellation_are_fail_closed() -> None:
    guard = create_guard_operation(None)
    result = guard(
        "evaluate",
        {
            "definition_enabled": True,
            "caller_id": "caller",
            "profile_id": "default",
            "profile_permission": True,
            "cancelled": True,
            "decision_time": 1,
            "deadline": 2,
        },
    )

    assert result["guard_order"] == [
        "definition_enabled",
        "caller_bound",
        "profile_bound",
        "profile_permission",
        "not_cancelled",
        "deadline_remaining",
    ]
    assert result["allowed"] is False
    assert result["reason"] == "not_cancelled"


@pytest.mark.parametrize(
    ("authority", "approval_required"),
    [
        ("file.read", False),
        ("file.write", True),
        ("shell.inspect", False),
        ("shell.execute", True),
        ("git.publish", True),
        ("browser.control", True),
        ("desktop.control", True),
        ("clipboard.read", True),
    ],
)
def test_policy_splits_declared_authorities(
    authority: str,
    approval_required: bool,
) -> None:
    policy = create_policy_operation(None)
    result = policy(
        "evaluate",
        {"authority": authority, "granted_authorities": [authority]},
    )
    assert result["allowed"] is True
    assert result["approval_required"] is approval_required


def test_unknown_authority_and_missing_profile_grant_are_denied() -> None:
    policy = create_policy_operation(None)
    unknown = policy(
        "evaluate", {"authority": "unknown.do", "granted_authorities": []}
    )
    missing = policy(
        "evaluate", {"authority": "file.read", "granted_authorities": []}
    )
    assert unknown["reason"] == "unknown_authority"
    assert missing["reason"] == "profile_permission_missing"


def test_approval_request_binds_operation_args_caller_profile_and_replay() -> None:
    authorize = create_authorize_operation(None)
    result = authorize(
        "authorize",
        {
            "approval_required": True,
            "tool_id": "sample.write",
            "authority": "file.write",
            "arguments": {"path": "a", "content": "b"},
            "caller_id": "caller",
            "profile_id": "default",
            "risk": "high",
        },
    )
    scope = result["scope"]
    assert result["authorized"] is False
    assert scope["operation"] == "tool.invoke:sample.write"
    assert scope["caller_id"] == "caller"
    assert scope["profile_id"] == "default"
    assert scope["replay_policy"] == "one_shot"
    assert len(scope["args_hash"]) == 64


def test_selector_prefers_exact_kind_and_is_deterministic() -> None:
    select = create_select_operation(None)
    result = select(
        "select",
        {
            "execution_kind": "sandbox",
            "providers": [
                {"provider_instance_id": "z", "routing_keys": ["*"]},
                {
                    "provider_instance_id": "sandbox-b",
                    "routing_keys": ["sandbox"],
                    "priority": 20,
                },
                {
                    "provider_instance_id": "sandbox-a",
                    "routing_keys": ["sandbox"],
                    "priority": 10,
                },
            ],
        },
    )
    assert result["selected"]["provider_instance_id"] == "sandbox-a"
    assert result["selected"]["routing_match"] == "exact"


def test_result_normalizer_redacts_secret_fields_and_keeps_widget() -> None:
    normalize = create_normalize_operation(None)
    result = normalize(
        "normalize",
        {
            "tool_id": "sample.read",
            "tool_call_id": "call-1",
            "value": {
                "result": {"token": "secret", "value": 1},
                "is_error": False,
                "widget": {"kind": "text"},
            },
        },
    )
    assert result["result"]["token"] == "[REDACTED]"
    assert result["widget"] == {"kind": "text"}


class _McpClient:
    def invoke(self, *args, **kwargs):
        return {"unexpected": [args, kwargs]}


def test_mcp_executor_rejects_missing_namespace_before_gateway_call() -> None:
    execute = create_mcp_execute_operation(_McpClient())
    with pytest.raises(ValueError, match="descriptor"):
        execute(
            "execute",
            {
                "_contract_consumer_pack_id": "rumi_tool_broker_pack",
                "definition": {
                    "execution": {
                        "contract_id": "rumi.service.mcp.tool.call.v1",
                        "provider_instance_id": "mcp-gateway.call",
                        "operation": "search",
                    }
                },
                "arguments": {},
            },
        )


def test_mcp_executor_rejects_nonbroker_consumer() -> None:
    execute = create_mcp_execute_operation(_McpClient())
    with pytest.raises(PermissionError, match="consumer"):
        execute("execute", {"_contract_consumer_pack_id": "untrusted-pack"})


def test_broker_source_has_no_concrete_tool_or_service_branches() -> None:
    broker = (
        Path(__file__).parents[1]
        / "ecosystem"
        / "rumi_tool_broker_pack"
        / "runtime"
        / "broker.py"
    ).read_text(encoding="utf-8")
    forbidden = (
        "ToolRegistry",
        "ToolExecutor",
        "coding_file",
        "computer_use",
        "browser_use",
        "defaultspack",
    )
    assert all(value not in broker for value in forbidden)


def test_contract_catalog_does_not_fallback_to_legacy_registry(monkeypatch) -> None:
    def unavailable(*_args, **_kwargs):
        raise GlobalContractUnavailable("inactive v4 profile")

    monkeypatch.setattr(
        "ecosystem.defaultspack.domain.tool.catalog_contract_client._invoke",
        unavailable,
    )
    catalog = ContractToolCatalog()

    assert catalog.list_tools() == []
    assert catalog.get("legacy-only-tool") is None
    assert catalog.get_schema("legacy-only-tool") == {}
