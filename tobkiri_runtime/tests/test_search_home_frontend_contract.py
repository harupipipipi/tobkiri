from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import quote

import pytest

from core_runtime.global_contracts.http_contract_dispatch import (
    HTTPContractBinding,
    HTTPContractRouteError as ContractRouteError,
    HTTPContractTarget,
    resolve_contract_route,
)

pytestmark = pytest.mark.contract


RUNTIME_ROOT = Path(__file__).resolve().parents[1]


def test_search_surface_uses_the_shared_defaults_contract_map() -> None:
    """Search selects the same model owners and native Gateway as Defaults."""
    document = json.loads(
        (
            RUNTIME_ROOT / "ecosystem" / "defaultspack" / "defaultspack"
            / "frontend_contract_map.v4.json"
        ).read_text(encoding="utf-8")
    )
    assert document["pack_id"] == "defaultspack"
    routes = {(route["method"], route["path"]): route for route in document["routes"]}
    expected = {
        ("POST", "/api/search/answer"): (
            "defaults.search.answer",
            "tobkiri.service.ai.generate.v1",
            "rumi_ai_gateway_pack.ai-gateway.generate",
            "rumi_ai_gateway_pack.ai-gateway.generate",
            "rumi_ai_gateway_pack.ai-gateway.generate",
        ),
        ("POST", "/api/ai/models/search"): (
            "defaults.ui.model-search.read",
            "tobkiri.resource.ui.model-search.v1",
            "tobkiri_ui_settings_pack.model-search",
            "tobkiri.ui.model-search.read",
            "tobkiri.ui.model-search.read",
        ),
        ("GET", "/api/ui/model-state"): (
            "defaults.ui.model-state.read",
            "tobkiri.resource.ui.model-state.v1",
            "tobkiri_ui_settings_pack.model-state-read",
            "tobkiri.ui.model-state.read",
            "tobkiri.ui.model-state.read",
        ),
        ("PUT", "/api/ui/model-state"): (
            "defaults.ui.model-state.write",
            "tobkiri.action.ui.model-state.v1",
            "tobkiri_ui_settings_pack.model-state-write",
            "tobkiri.ui.model-state.write",
            "tobkiri.ui.model-state.write",
        ),
    }
    for operation, identity in expected.items():
        targets = routes[operation]["targets"]
        assert len(targets) == 1
        assert tuple(
            targets[0][key]
            for key in (
                "contribution_id", "contract_id", "operation_id",
                "provider_id", "function_id",
            )
        ) == identity
    answer_route = routes[("POST", "/api/search/answer")]
    assert answer_route["presentation"] == "search_answer"
    assert set(answer_route["targets"][0]["allowed_payload_keys"]) == {"input", "model"}
    assert ("POST", "/api/answer") not in routes
    assert ("POST", "/api/route") not in routes


def test_search_gateway_target_is_a_declared_native_host_operation() -> None:
    """Search must not depend on the legacy bridge or a conversation PackVM."""
    document = json.loads(
        (
            RUNTIME_ROOT / "ecosystem" / "rumi_ai_gateway_pack"
            / "executables.v4.json"
        ).read_text(encoding="utf-8")
    )
    variants = [
        variant for variant in document["variants"]
        if variant["function_id"] == "rumi_ai_gateway_pack.ai-gateway.generate"
    ]
    assert len(variants) == 1
    assert variants[0]["execution_kind"] == "host_extension"
    assert variants[0]["backend"] == "tobkiri.python-host-v4"
    assert (
        variants[0]["operations"][0]["contract_id"],
        variants[0]["operations"][0]["operation_id"],
    ) == (
        "tobkiri.service.ai.generate.v1",
        "rumi_ai_gateway_pack.ai-gateway.generate",
    )


SEARCH_HOME_ROUTES = {
    (method, path): HTTPContractBinding(
        method=method,
        path=path,
        presentation="search_home_result",
        targets=(
            HTTPContractTarget(
                contribution_id=f"search-home.{method.lower()}.{path[5:].replace('/', '.')}",
                contract_id="search-home.ui.v1",
                operation_id=path.removeprefix("/api/").replace("/", "."),
                provider_id="search-home.desktop",
                function_id="search-home.desktop",
            ),
        ),
        application_id="search_home_pack",
        route_namespace="search_home_pack",
    )
    for method, path in (
        ("GET", "/api/models"),
        ("GET", "/api/settings"),
        ("GET", "/api/route-state"),
        ("POST", "/api/route"),
        ("POST", "/api/answer"),
        ("POST", "/api/settings/model"),
        ("POST", "/api/route-state"),
    )
}


class _SearchHomeHost:
    _contract_routes = SEARCH_HOME_ROUTES


def _operation(method: str, target: str) -> str:
    return f"/api/contracts/search_home_pack/{quote(f'{method} {target}', safe='')}"


def test_search_home_operation_resolves_exact_route_and_query() -> None:
    resolved = resolve_contract_route(
        _SearchHomeHost(),
        "GET",
        _operation("GET", "/api/route-state?source=restart"),
        namespace="search_home_pack",
    )

    assert resolved is not None
    assert resolved.method == "GET"
    assert resolved.path == "/api/route-state"
    assert resolved.query == {"source": "restart"}


@pytest.mark.parametrize(
    "method,target,code",
    [
        ("GET", "/api/answer", "CONTRACT_OPERATION_UNKNOWN"),
        ("GET", "/api/route/../answer", "CONTRACT_PATH_INVALID"),
        ("GET", "https://evil.example/api/models", "CONTRACT_PATH_INVALID"),
        ("GET", "/api/context", "CONTRACT_OPERATION_UNKNOWN"),
        ("POST", "/api/contracts/search_home_pack/other", "CONTRACT_PATH_INVALID"),
    ],
)
def test_search_home_unknown_or_escaped_operation_fails_closed(
    method: str,
    target: str,
    code: str,
) -> None:
    with pytest.raises(ContractRouteError) as exc_info:
        resolve_contract_route(
            _SearchHomeHost(),
            method,
            _operation(method, target),
            namespace="search_home_pack",
        )
    assert exc_info.value.code == code


def test_search_home_operation_requires_a_canonical_application_binding() -> None:
    class UnboundHost:
        _contract_routes = {("POST", "/api/answer"): object()}

    with pytest.raises(ContractRouteError) as exc_info:
        resolve_contract_route(
            UnboundHost(),
            "POST",
            _operation("POST", "/api/answer"),
            namespace="search_home_pack",
        )
    assert exc_info.value.code == "CONTRACT_OPERATION_UNKNOWN"


def test_search_home_handler_uses_contract_map_before_legacy_dispatch(tmp_path) -> None:
    from ecosystem.search_home_pack import desktop_app

    handler_type = desktop_app._make_handler(tmp_path)
    handler = object.__new__(handler_type)
    responses: list[tuple[dict[str, object], object]] = []
    handler._json_response = lambda payload, status=None: responses.append((payload, status))

    assert (
        handler._resolve_contract_path(
            "GET",
            _operation("GET", "/api/models"),
        )
        == "/api/models"
    )
    assert (
        handler._resolve_contract_path(
            "GET",
            _operation("GET", "/api/context"),
        )
        is None
    )
    assert responses[0][0]["error"]["code"] == "CONTRACT_OPERATION_UNKNOWN"
