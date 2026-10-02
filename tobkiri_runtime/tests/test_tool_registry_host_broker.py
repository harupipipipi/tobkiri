"""Real production capture and Broker with a selected declarative tool Pack.

This isolated Profile proves the owner edge, not ordinary Defaults or native UI.
"""

from pathlib import Path

import pytest

from core_runtime.authority.v4 import AuthorityDenied
from tests.conformance_support.host_profile import captured_host_profile
from tobkiri_host.errors import ProviderExecutionError


@pytest.mark.parametrize("selected", [True, False])
def test_production_registry_uses_only_selected_pack_data(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    selected: bool,
) -> None:
    contract = "tobkiri.resource.tool.definition.v1"
    operation = "rumi_tool_registry_pack.tool-definition-resource"
    with captured_host_profile(
        tmp_path,
        monkeypatch,
        packs=() if selected else ("rumi_default_tool_projection_pack",), edges=(),
        exclude_packs=() if selected else ("rumi_default_tools_pack",),
        exclude_callers=() if selected else ("rumi_tool_local_executor_pack.tool-executor.local",),
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
        assert len(result["definitions"]) == (149 if selected else 119)
        assert not (tmp_path / "user-data/packs/rumi_tool_registry_pack").exists()
        assert {item["pack_id"] for item in result["pack_data_sources"]} == (
            {"defaultspack", "rumi_default_tools_pack"} if selected else {"defaultspack"}
        )
        identifiers = {item["tool_id"] for item in result["definitions"]}
        assert {"artifact_file_read", "memo_note_upsert", "settings_update"} <= identifiers
        assert ("calculator" in identifiers) is selected
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
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, local_selected: bool,
) -> None:
    """Exercise real capture and nested authority without enabling legacy code."""
    from ecosystem.rumi_tool_broker_pack.runtime import broker

    factory = broker.HOST_PROVIDER_FACTORY
    routes = (
        ("shell.tauri.default", factory.function_id, broker.CONTRACT, broker.OPERATION),
        (factory.function_id, "rumi_tool_registry_pack.tool-registry.definition",
         broker.DEFINITION, "rumi_tool_registry_pack.tool-definition-resource"),
        (factory.function_id, "rumi_tool_validation_pack.tool-validation.arguments",
         broker.VALIDATE, "rumi_tool_validation_pack.tool-arguments-validate"),
        (factory.function_id, "rumi_tool_result_pack.tool-result.normalize",
         broker.NORMALIZE, "rumi_tool_result_pack.tool-result-normalize"),
    )
    local = "rumi_tool_local_executor_pack.tool-executor.local"
    if local_selected:
        routes += (
            (factory.function_id, local, broker.EXECUTE,
             "rumi_tool_local_executor_pack.tool-local-execute"),
            (local, "rumi_tool_registry_pack.tool-registry.definition", broker.DEFINITION,
             "rumi_tool_registry_pack.tool-definition-resource"),
        )
    edges = [{
        "caller_function_id": caller, "target_provider_id": target,
        "contract_id": contract, "operation_id": operation,
        "authority_mode": "profile_grant",
        "requested_scope_template": {
            "capability": "operation.invoke",
            "dimensions": {"contract": [contract], "operation": [operation]},
            "quotas": {}, "exact_request_digest": None, "opaque": False,
        },
    } for caller, target, contract, operation in routes]
    with captured_host_profile(
        tmp_path, monkeypatch,
        packs=(),
        exclude_packs=() if local_selected else (
            "rumi_tool_local_executor_pack", "rumi_default_tool_projection_pack",
        ),
        exclude_callers=(factory.function_id, local),
        edges=edges, backends=(),
    ) as (session, _store):
        session.assert_operation_ready(broker.CONTRACT, broker.OPERATION)
        request = {
            "tool_id": "file_reader", "tool_call_id": "call-1",
            "arguments": {"path": "readme.txt"}, "_session_id": "tool-composition",
        }
        with pytest.raises(ProviderExecutionError, match="provider execution failed") as failure:
            session.invoke(broker.CONTRACT, broker.OPERATION, request)
        cause = failure.value.__cause__
        if local_selected:
            # The nested executor has its own redacted boundary. Keep its
            # private cause observable to this Host-side test only.
            assert isinstance(cause, ProviderExecutionError)
            cause = cause.__cause__
        assert isinstance(cause, PermissionError)
        assert str(cause) == (
            "selected local tool operation is unavailable" if local_selected
            else "selected tool executor is unavailable"
        )
        with pytest.raises(ProviderExecutionError, match="provider execution failed") as failure:
            session.invoke(broker.CONTRACT, broker.OPERATION, {**request, "arguments": {}})
        assert isinstance(failure.value.__cause__, ValueError)
        assert str(failure.value.__cause__) == "tool arguments are invalid"
        with pytest.raises(ProviderExecutionError, match="provider execution failed") as failure:
            session.invoke(broker.CONTRACT, broker.OPERATION, {**request, "approved": True})
        assert isinstance(failure.value.__cause__, ValueError)
        assert str(failure.value.__cause__) == "tool invocation payload is invalid"
        assert not (tmp_path / "user-data/packs/rumi_tool_registry_pack").exists()
