from __future__ import annotations

from urllib.parse import quote

import pytest

from core_runtime.frontend_contract_routes import (
    ContractRouteError,
    resolve_contract_route,
)

pytestmark = pytest.mark.contract


class _FakeHost:
    _api_route_exact = {("GET", "/api/ui/catalog"): {}}
    _api_route_patterns = ()


def _operation(method: str, target: str) -> str:
    return f"/api/contracts/defaultspack/{quote(f'{method} {target}', safe='')}"


def test_contract_operation_resolves_only_after_method_and_route_validation() -> None:
    resolved = resolve_contract_route(
        _FakeHost(),
        "GET",
        _operation("GET", "/api/ui/catalog?include_skills=true"),
    )

    assert resolved is not None
    assert resolved.method == "GET"
    assert resolved.path == "/api/ui/catalog"
    assert resolved.query == {"include_skills": "true"}


def test_contract_operation_rejects_method_mismatch() -> None:
    with pytest.raises(ContractRouteError, match="method mismatch") as exc_info:
        resolve_contract_route(
            _FakeHost(),
            "POST",
            _operation("GET", "/api/ui/catalog"),
        )

    assert exc_info.value.code == "CONTRACT_METHOD_MISMATCH"
    assert exc_info.value.status == 405


@pytest.mark.parametrize(
    "target,code",
    [
        ("/api/ui/../health", "CONTRACT_PATH_INVALID"),
        ("/api/contracts/defaultspack/other", "CONTRACT_PATH_INVALID"),
        ("/api/not-owned/operation", "CONTRACT_OPERATION_UNKNOWN"),
    ],
)
def test_contract_operation_fails_closed_for_escape_recursion_and_unknown_route(
    target: str,
    code: str,
) -> None:
    with pytest.raises(ContractRouteError) as exc_info:
        resolve_contract_route(_FakeHost(), "GET", _operation("GET", target))

    assert exc_info.value.code == code


def test_contract_operation_has_no_family_prefix_fallback() -> None:
    class _EmptyHost:
        _api_route_exact = {}
        _api_route_patterns = ()

    with pytest.raises(ContractRouteError) as exc_info:
        resolve_contract_route(
            _EmptyHost(),
            "POST",
            _operation("POST", "/api/authority/requests/forged/approve"),
        )

    assert exc_info.value.code == "CONTRACT_OPERATION_UNKNOWN"


@pytest.mark.parametrize(
    "target",
    (
        "/api/ui/value%252F..%252Fsecret",
        "/api/ui/catalog?mode=a&mode=b",
        "/api/ui/catalog#ignored",
    ),
)
def test_contract_operation_rejects_nested_traversal_ambiguous_query_and_fragment(
    target: str,
) -> None:
    with pytest.raises(ContractRouteError):
        resolve_contract_route(_FakeHost(), "GET", _operation("GET", target))


def test_encoded_identifier_is_left_for_normal_route_matching() -> None:
    class _PatternHost:
        _api_route_exact = {}
        _api_route_patterns = (("GET", __import__("re").compile(r"^/api/company/[^/]+$"), (), {}),)

    resolved = resolve_contract_route(
        _PatternHost(),
        "GET",
        _operation("GET", "/api/company/operations%2Fcompany"),
    )
    assert resolved is not None
    assert resolved.path.endswith("operations%2Fcompany")


def test_pack_handler_does_not_rewrite_frontend_operations() -> None:
    from core_runtime.pack_api_server import PackAPIHandler

    assert not hasattr(PackAPIHandler, "_resolve_frontend_contract_target")
