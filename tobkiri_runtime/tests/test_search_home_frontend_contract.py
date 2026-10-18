from __future__ import annotations

from urllib.parse import quote

import pytest

from core_runtime.global_contracts.http_contract_dispatch import (
    HTTPContractBinding,
    HTTPContractRouteError as ContractRouteError,
    HTTPContractTarget,
    resolve_contract_route,
)

pytestmark = pytest.mark.contract


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
