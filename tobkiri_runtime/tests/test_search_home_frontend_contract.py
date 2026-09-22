from __future__ import annotations

from urllib.parse import quote

import pytest

from core_runtime.frontend_contract_routes import ContractRouteError, resolve_contract_route

pytestmark = pytest.mark.contract


SEARCH_HOME_ROUTES = {
    ("GET", "/api/models"): {"approval_required": False},
    ("GET", "/api/settings"): {"approval_required": False},
    ("GET", "/api/route-state"): {"approval_required": False},
    ("POST", "/api/route"): {"approval_required": False},
    ("POST", "/api/answer"): {"approval_required": False},
    ("POST", "/api/settings/model"): {"approval_required": False},
    ("POST", "/api/route-state"): {"approval_required": False},
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
        pack_id="search_home_pack",
        route_families=(),
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
            pack_id="search_home_pack",
            route_families=(),
        )
    assert exc_info.value.code == code


def test_search_home_operation_requires_host_approval_when_declared() -> None:
    class ApprovalHost:
        _contract_routes = {
            ("POST", "/api/answer"): {"approval_required": True},
        }

    with pytest.raises(ContractRouteError) as exc_info:
        resolve_contract_route(
            ApprovalHost(),
            "POST",
            _operation("POST", "/api/answer"),
            pack_id="search_home_pack",
            route_families=(),
        )
    assert exc_info.value.code == "CONTRACT_APPROVAL_REQUIRED"
    assert exc_info.value.status == 403


def test_search_home_approval_requires_server_side_checker() -> None:
    class ApprovedHost:
        _contract_routes = {
            ("POST", "/api/answer"): {"approval_required": True},
        }

        @staticmethod
        def _contract_approval_check(method: str, path: str) -> bool:
            return method == "POST" and path == "/api/answer"

    resolved = resolve_contract_route(
        ApprovedHost(),
        "POST",
        _operation("POST", "/api/answer"),
        pack_id="search_home_pack",
        route_families=(),
    )
    assert resolved is not None
    assert resolved.path == "/api/answer"


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
