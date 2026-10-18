from __future__ import annotations

from urllib.parse import quote

import pytest

from core_runtime.global_contracts.http_contract_dispatch import (
    HTTPContractBinding,
    HTTPContractRouteError as ContractRouteError,
    HTTPContractTarget,
    contract_binding_map,
    resolve_contract_route,
)

pytestmark = pytest.mark.contract


class _FakeHost:
    _contract_routes = {
        ("GET", "/api/ui/catalog"): HTTPContractBinding(
            method="GET",
            path="/api/ui/catalog",
            presentation="broker_result",
            targets=(
                HTTPContractTarget(
                    contribution_id="test.catalog",
                    contract_id="test.catalog.v1",
                    operation_id="read",
                    provider_id="test.catalog",
                    function_id="test.catalog",
                ),
            ),
            application_id="test.application",
            route_namespace="defaultspack",
        )
    }


def _operation(method: str, target: str) -> str:
    return f"/api/contracts/defaultspack/{quote(f'{method} {target}', safe='')}"


@pytest.mark.parametrize(
    "method,path",
    [
        ("GET", "/api/runtime-surface/profiles"),
        ("GET", "/api/runtime-surface/operation-status"),
        ("POST", "/api/runtime-surface/profile-change/resolve"),
        ("POST", "/api/runtime-surface/profile-change/review"),
        ("POST", "/api/runtime-surface/profile-change/approve"),
        ("POST", "/api/runtime-surface/profile-change/activate"),
    ],
)
def test_host_profile_ceremony_resolves_before_application_execution(
    method: str, path: str,
) -> None:
    """Real bootstrap bindings must pass the canonical namespace parser."""
    from types import SimpleNamespace

    from ecosystem.defaultspack.defaultspack.runtime_surface_targets import (
        host_profile_control_bindings,
    )

    host = SimpleNamespace(
        _contract_routes=contract_binding_map(host_profile_control_bindings())
    )
    resolved = resolve_contract_route(host, method, _operation(method, path))

    assert resolved is not None
    assert (resolved.method, resolved.path) == (method, path)
    binding = host._contract_routes[(method, path)]
    assert not any(
        getattr(binding, field)
        for field in ("profile_id", "profile_revision", "activation_id", "plan_digest")
    )
    with pytest.raises(ContractRouteError) as denied:
        resolve_contract_route(
            host,
            method,
            f"/api/contracts/other/{quote(f'{method} {path}', safe='')}",
        )
    assert denied.value.code == "CONTRACT_OPERATION_UNKNOWN"


@pytest.mark.parametrize(
    "method,path",
    [
        ("GET", "/api/runtime-surface/profile"),
        ("GET", "/api/settings"),
        ("POST", "/api/authority/approve"),
    ],
)
def test_host_profile_namespace_does_not_expose_runtime_or_approval_routes(
    method: str, path: str,
) -> None:
    from types import SimpleNamespace

    from ecosystem.defaultspack.defaultspack.runtime_surface_targets import (
        host_profile_control_bindings,
    )

    host = SimpleNamespace(
        _contract_routes=contract_binding_map(host_profile_control_bindings())
    )
    with pytest.raises(ContractRouteError) as denied:
        resolve_contract_route(host, method, _operation(method, path))
    assert denied.value.code == "CONTRACT_OPERATION_UNKNOWN"


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
        _contract_routes = {}
        _api_route_exact = {
            ("POST", "/api/authority/requests/forged/approve"): {},
        }
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
        _contract_routes = {
            ("GET", "/api/company/operations%2Fcompany"): HTTPContractBinding(
                method="GET",
                path="/api/company/operations%2Fcompany",
                presentation="broker_result",
                targets=(
                    HTTPContractTarget(
                        contribution_id="test.encoded",
                        contract_id="test.encoded.v1",
                        operation_id="read",
                        provider_id="test.encoded",
                        function_id="test.encoded",
                    ),
                ),
                application_id="test.application",
                route_namespace="defaultspack",
            )
        }

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
