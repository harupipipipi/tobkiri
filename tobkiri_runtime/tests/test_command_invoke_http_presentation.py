from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from core_runtime.global_contracts.http_contract_dispatch import HTTPContractTarget
from ecosystem.defaultspack.defaultspack.http_surface_presentation import (
    DefaultspackHTTPPresentation,
)


TARGET = (
    "defaults.commands.invoke",
    "tobkiri.action.command.invoke.v1",
    "command.invoke",
    "rumi_command_protocol_pack.command.invoke",
    "rumi_command_protocol_pack.command.invoke",
)
STATE_TARGET = (
    "defaults.commands.state.query",
    "tobkiri.resource.command.state.v1",
    "command.state.query",
    "rumi_command_protocol_pack.command.state",
    "rumi_command_protocol_pack.command.state",
)
DATASOURCE_TARGET = (
    "defaults.commands.datasource.query",
    "tobkiri.resource.command.datasource.v1",
    "command.datasource.query",
    "rumi_command_protocol_pack.command.datasource",
    "rumi_command_protocol_pack.command.datasource",
)


def test_command_invoke_route_selects_only_the_ordinary_provider() -> None:
    root = Path(__file__).resolve().parents[1]
    contract_map = json.loads(
        (
            root
            / "ecosystem"
            / "defaultspack"
            / "defaultspack"
            / "frontend_contract_map.v4.json"
        ).read_text(encoding="utf-8")
    )
    routes = [
        route
        for route in contract_map["routes"]
        if route["method"] == "POST"
        and route["path"] == "/api/command-protocol/v1/invoke"
    ]
    assert len(routes) == 1
    assert len(routes[0]["targets"]) == 1
    target = routes[0]["targets"][0]
    assert tuple(target[key] for key in (
        "contribution_id",
        "contract_id",
        "operation_id",
        "provider_id",
        "function_id",
    )) == TARGET
    assert "rumi_command_protocol_pack.high-risk-command.service" not in json.dumps(
        target, sort_keys=True
    )


def test_command_invoke_payload_is_bound_to_the_captured_profile() -> None:
    assertions = []
    session = SimpleNamespace(
        profile_id="defaults",
        assert_current=lambda: assertions.append("current"),
    )
    payload = {
        "command_ref": "defaultspack:help",
        "args": {},
        "invocation_id": "help-1",
        "mode": "chat",
    }
    normalized = DefaultspackHTTPPresentation().normalize_payload(
        HTTPContractTarget(*TARGET),
        payload,
        session=session,
        workspace_binding_resolver=None,
    )
    assert normalized == {**payload, "profile_id": "defaults"}
    assert assertions == ["current"]


def test_command_query_routes_are_exact_and_profile_bound() -> None:
    root = Path(__file__).resolve().parents[1]
    contract_map = json.loads((
        root / "ecosystem/defaultspack/defaultspack/frontend_contract_map.v4.json"
    ).read_text(encoding="utf-8"))
    expected = {
        "/api/command-protocol/v1/states/query": (STATE_TARGET, {"state_refs"}),
        "/api/command-protocol/v1/datasources/query": (
            DATASOURCE_TARGET,
            {"datasource_ref", "query", "cursor", "limit", "selected_values", "request_id"},
        ),
    }
    session = SimpleNamespace(profile_id="defaults", assert_current=lambda: None)
    for path, (identity, payload_keys) in expected.items():
        routes = [route for route in contract_map["routes"] if route["path"] == path]
        assert len(routes) == 1
        target = routes[0]["targets"][0]
        assert tuple(target[key] for key in (
            "contribution_id", "contract_id", "operation_id", "provider_id", "function_id",
        )) == identity
        assert set(target["allowed_payload_keys"]) == payload_keys
        payload = ({"state_refs": []} if identity == STATE_TARGET
                   else {"datasource_ref": "tobkiri:model_catalog"})
        normalized = DefaultspackHTTPPresentation().normalize_payload(
            HTTPContractTarget(*identity), payload, session=session,
            workspace_binding_resolver=None,
        )
        assert normalized == {**payload, "profile_id": "defaults"}
