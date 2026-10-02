"""Owned schema selection checks captured metadata without executing a tool."""

from copy import deepcopy

import pytest

from ecosystem.rumi_tool_registry_pack.runtime.selection import EXECUTE, LOCAL
from tests.test_pack_architecture_wave6 import _definition
from tests.test_tool_registry_host import registry_host as _registry_host


@pytest.fixture
def registry_host(tmp_path):
    return _registry_host.__wrapped__(tmp_path)


def _selected(registry_host):
    invoke, client = registry_host
    definition = _definition()
    definition["execution"] = {
        "kind": "local",
        "contract_id": LOCAL,
        "provider_instance_id": "fixture.read",
        "operation": "fixture.read",
    }
    invoke("manage", {"operation": "save", "definition": definition, "expected_revision": 0})
    client.routes[EXECUTE] = [
        {
            "function_id": "rumi_tool_local_executor_pack.tool-executor.local",
            "operation_id": "rumi_tool_local_executor_pack.tool-local-execute",
            "provider_instance_id": "tool-executor.local",
            "backend_id": "test",
        }
    ]
    client.routes[LOCAL] = [
        {
            "function_id": "fixture.read",
            "operation_id": "fixture.read",
            "provider_instance_id": "fixture.read",
            "backend_id": "test",
        }
    ]
    return invoke, client


def test_selected_schema_has_owner_hash_and_never_invokes_execution(registry_host):
    invoke, client = _selected(registry_host)
    selected = invoke("definition", {"operation": "select", "selection": {"mode": "auto"}})
    owned = invoke("definition", {"operation": "resolve", "tool_id": "sample.read"})
    assert selected["definitions"] == {"sample.read": owned["definition"]["definition_hash"]}
    assert selected["tools"][0]["function"]["parameters"] == owned["definition"]["input_schema"]
    assert client.calls == []
    assert invoke("definition", {"operation": "select", "selection": {"mode": "none"}}) == {
        "tools": [],
        "definitions": {},
    }


@pytest.mark.parametrize("change", ["missing", "ambiguous", "backend", "operation", "provider"])
def test_unavailable_route_is_filtered_and_explicit_selection_is_denied(registry_host, change):
    invoke, client = _selected(registry_host)
    route = client.routes[LOCAL][0]
    if change == "missing":
        client.routes[LOCAL] = []
    elif change == "ambiguous":
        client.routes[LOCAL].append(deepcopy(route))
    elif change == "backend":
        route["backend_unavailable_reason"] = "not registered"
    elif change == "operation":
        route["operation_id"] = "other.read"
    else:
        route["function_id"] = route["provider_instance_id"] = "other.read"
    assert (
        invoke("definition", {"operation": "select", "selection": {"mode": "auto"}})["tools"] == []
    )
    with pytest.raises(PermissionError):
        invoke(
            "definition",
            {
                "operation": "select",
                "selection": {
                    "mode": "manual",
                    "include": ["sample.read"],
                },
            },
        )
    assert client.calls == []


def test_alias_exclusion_and_must_use_do_not_create_an_execution_route(registry_host):
    invoke, client = _selected(registry_host)
    invoke(
        "manage",
        {
            "operation": "alias",
            "alias": "short",
            "target_tool_id": "sample.read",
            "expected_revision": 1,
        },
    )
    selection = {"mode": "manual", "include": ["short"], "exclude": ["sample.read"]}
    assert invoke("definition", {"operation": "select", "selection": selection})["tools"] == []
    with pytest.raises(PermissionError):
        invoke("definition", {"operation": "select", "selection": {**selection, "must_use": True}})
    assert client.calls == []
