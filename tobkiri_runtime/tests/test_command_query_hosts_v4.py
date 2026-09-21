from __future__ import annotations

from types import SimpleNamespace

import pytest

from ecosystem.rumi_command_protocol_pack.runtime import datasource, state


def _capture(factory, function_id, contract_id, operation_id, tmp_path):
    binding = SimpleNamespace(
        function=SimpleNamespace(function_id=function_id, implementation_digest="impl"),
        operation=SimpleNamespace(
            contract_id=contract_id,
            operation_id=operation_id,
            contract_version="1.0.0",
        ),
        principal_ref=SimpleNamespace(value="provider-principal"),
        artifact=SimpleNamespace(digest="artifact"),
    )
    context = SimpleNamespace(
        profile_id="defaults",
        user_data_root=tmp_path,
        provider_bindings=(binding,),
        domain_ids={(contract_id, operation_id, "provider-principal"): "domain"},
    )
    return factory.capture(context).contributions[0]


class _Client:
    def __init__(self) -> None:
        self.calls = []

    def invoke(self, contract_id, operation_id, payload):
        self.calls.append((contract_id, operation_id, payload))
        if contract_id == state.MODEL_STATE_CONTRACT:
            return {
                "namespace": "captured",
                "revision": 7,
                "values": {"deepthink_enabled": True},
            }
        if contract_id == datasource.MODEL_CONTRACT:
            return {
                "revision": 4,
                "profiles": [
                    {
                        "model_profile_id": "model-a",
                        "display_name": "Model A",
                        "model_id": "vendor/model-a",
                        "enabled": True,
                        "metadata": {"provider_connection_id": "provider-a"},
                    }
                ],
            }
        return {
            "revision": 9,
            "providers": [
                {
                    "provider_instance_id": "provider-a",
                    "display_name": "Provider A",
                    "enabled": True,
                }
            ],
        }


class _Invocation:
    presentation_owner_principal_id = "app-principal"
    presentation_owner_session_id = "session-1"

    def __init__(self) -> None:
        self.assertions = 0
        self.client = _Client()
        self.scopes = []

    def assert_current(self):
        self.assertions += 1

    def contract_client(self, **scope):
        self.scopes.append(scope)
        return self.client


def test_state_query_reads_the_profile_bound_model_state_owner(tmp_path):
    contribution = _capture(
        state.CommandStateHostFactoryV4(),
        state.FUNCTION_ID,
        state.CONTRACT_ID,
        state.OPERATION_ID,
        tmp_path,
    )
    invocation = _Invocation()
    result = contribution.invoke(
        state.OPERATION_ID,
        {"profile_id": "defaults", "state_refs": [state.STATE_REF]},
        invocation,
    )
    assert result["states"] == [{
        "state_ref": state.STATE_REF,
        "value": True,
        "revision": 7,
        "freshness": "authoritative",
    }]
    assert invocation.assertions == 2
    assert invocation.scopes == [{
        "allowed_contract_ids": frozenset({state.MODEL_STATE_CONTRACT}),
        "consumer_pack_id": "rumi_command_protocol_pack",
    }]
    assert invocation.client.calls == [(
        state.MODEL_STATE_CONTRACT,
        state.MODEL_STATE_OPERATION,
        {"profile_id": "defaults"},
    )]


def test_state_query_rejects_unknown_state_without_calling_owner(tmp_path):
    contribution = _capture(
        state.CommandStateHostFactoryV4(), state.FUNCTION_ID, state.CONTRACT_ID,
        state.OPERATION_ID, tmp_path,
    )
    invocation = _Invocation()
    with pytest.raises(PermissionError, match="references are invalid"):
        contribution.invoke(
            state.OPERATION_ID,
            {"profile_id": "defaults", "state_refs": ["attacker:state"]},
            invocation,
        )
    assert invocation.client.calls == []


@pytest.mark.parametrize(
    ("datasource_ref", "expected_value"),
    [
        (datasource.MODEL_DATASOURCE, "model-a"),
        (datasource.PROVIDER_DATASOURCE, "provider-a"),
    ],
)
def test_datasource_query_joins_redacted_canonical_registry_reads(
    tmp_path, datasource_ref, expected_value,
):
    contribution = _capture(
        datasource.CommandDatasourceHostFactoryV4(),
        datasource.FUNCTION_ID,
        datasource.CONTRACT_ID,
        datasource.OPERATION_ID,
        tmp_path,
    )
    invocation = _Invocation()
    payload = {
        "profile_id": "defaults",
        "datasource_ref": datasource_ref,
        "query": "",
        "cursor": "0",
        "limit": 25,
        "selected_values": [],
    }
    first = contribution.invoke(datasource.OPERATION_ID, payload, invocation)
    second = contribution.invoke(datasource.OPERATION_ID, payload, invocation)
    assert first == second
    assert first["items"][0]["value"] == expected_value
    assert first["revision"]
    assert first["request_id"]
    assert "credential" not in str(first).lower()
    assert invocation.scopes[0] == {
        "allowed_contract_ids": frozenset(
            {datasource.MODEL_CONTRACT, datasource.PROVIDER_CONTRACT}
        ),
        "consumer_pack_id": "rumi_command_protocol_pack",
        "include_credentials": False,
    }


def test_datasource_query_rejects_aliases_and_unbounded_fields(tmp_path):
    contribution = _capture(
        datasource.CommandDatasourceHostFactoryV4(),
        datasource.FUNCTION_ID,
        datasource.CONTRACT_ID,
        datasource.OPERATION_ID,
        tmp_path,
    )
    for payload in (
        {"profile_id": "defaults", "datasource_ref": "tobkiri:models.resolved"},
        {
            "profile_id": "defaults",
            "datasource_ref": datasource.MODEL_DATASOURCE,
            "frontend_action": "run_anything",
        },
    ):
        with pytest.raises(PermissionError):
            contribution.invoke(datasource.OPERATION_ID, payload, _Invocation())
