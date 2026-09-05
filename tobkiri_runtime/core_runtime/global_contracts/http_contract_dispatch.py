"""Generic, fail-closed dispatch for captured HTTP contract bindings.

The Host owns this parser and never lets a web client name a handler
directly.  Application packs contribute already-validated bindings through
composition; this module only enforces the generic HTTP and capture fences.
"""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any, Mapping
from urllib.parse import parse_qs, unquote, urlsplit

from tobkiri_protocol.errors import ProtocolError
from tobkiri_protocol.ids import validate_canonical_id


HTTP_CONTRACT_ROUTE_PREFIX = "/api/contracts/"
_CONTRACT_CONTEXT_FIELDS = (
    "profile_id",
    "profile_revision",
    "activation_id",
    "plan_digest",
)
_NAMESPACE_RE = re.compile(r"^[a-z][a-z0-9]*(?:[._-][a-z0-9]+)*$")
_HTTP_METHODS = frozenset({"GET", "POST", "PUT", "PATCH", "DELETE"})


class HTTPContractRouteError(ValueError):
    """Raised when a canonical HTTP contract operation is invalid."""

    def __init__(self, code: str, message: str, status: int = 404) -> None:
        super().__init__(message)
        self.code = code
        self.status = status


@dataclass(frozen=True)
class ResolvedHTTPContractRoute:
    """A validated Host route and the query values bound to it."""

    method: str
    path: str
    query: dict[str, str]


@dataclass(frozen=True)
class HTTPContractTarget:
    """One exact captured contribution available through a HTTP binding."""

    contribution_id: str
    contract_id: str
    operation_id: str
    provider_id: str
    function_id: str
    allowed_payload_keys: frozenset[str] = frozenset()
    owner_pack_id: str = ""
    artifact_digest: str = ""


@dataclass(frozen=True)
class HTTPContractBinding:
    """One exact HTTP route and the contributions captured for it."""

    method: str
    path: str
    presentation: str
    targets: tuple[HTTPContractTarget, ...]
    application_id: str = ""
    route_namespace: str = ""
    artifact_path: str = ""
    artifact_digest: str = ""
    profile_id: str = ""
    profile_revision: str = ""
    activation_id: str = ""
    plan_digest: str = ""


@dataclass(frozen=True)
class HTTPCapabilitySnapshot:
    """Finite targets and catalog identity captured for an HTTP capability UI."""

    catalog_hash: str
    targets: tuple[HTTPContractTarget, ...]


def contract_binding_map(
    bindings: tuple[HTTPContractBinding, ...],
) -> dict[tuple[str, str], HTTPContractBinding]:
    """Build an exact route map, rejecting ambiguous Host ownership."""

    result: dict[tuple[str, str], HTTPContractBinding] = {}
    for binding in bindings:
        key = (binding.method.upper(), binding.path)
        if key in result:
            raise HTTPContractRouteError(
                "CONTRACT_OPERATION_DUPLICATE",
                "HTTP contract operation is duplicated",
                500,
            )
        result[key] = binding
    return result


def is_contract_route_path(path: str) -> bool:
    """Return whether *path* is in the canonical HTTP contract namespace."""

    return str(path or "").startswith(HTTP_CONTRACT_ROUTE_PREFIX)


def contract_route_prefix(namespace: str | None = None) -> str:
    """Return the canonical endpoint prefix for one verified namespace."""

    if namespace is None:
        return HTTP_CONTRACT_ROUTE_PREFIX
    return f"{HTTP_CONTRACT_ROUTE_PREFIX}{_validate_route_namespace(namespace)}/"


def resolve_contract_route(
    server: Any,
    method: str,
    request_path: str,
    *,
    namespace: str | None = None,
) -> ResolvedHTTPContractRoute | None:
    """Resolve one opaque HTTP contract operation against captured bindings.

    ``None`` means that the request is outside the contract namespace.  A
    malformed request inside that namespace is always rejected rather than
    falling through to a route table maintained by an application.
    """

    request_value = str(request_path or "")
    if namespace is None:
        if not request_value.startswith(HTTP_CONTRACT_ROUTE_PREFIX):
            return None
        remainder = request_value[len(HTTP_CONTRACT_ROUTE_PREFIX) :]
        requested_namespace, separator, token = remainder.partition("/")
        if not separator:
            raise HTTPContractRouteError(
                "CONTRACT_OPERATION_INVALID", "Invalid contract operation", 400
            )
        requested_namespace = _validate_route_namespace(requested_namespace)
        prefix = f"{HTTP_CONTRACT_ROUTE_PREFIX}{requested_namespace}/"
    else:
        requested_namespace = _validate_route_namespace(namespace)
        prefix = contract_route_prefix(requested_namespace)
        if not request_value.startswith(prefix):
            return None
        token = request_value[len(prefix) :]
    if not token or "/" in token:
        raise HTTPContractRouteError(
            "CONTRACT_OPERATION_INVALID", "Invalid contract operation", 400
        )
    try:
        decoded = unquote(token)
    except Exception as error:  # pragma: no cover - urllib defensive branch
        raise HTTPContractRouteError(
            "CONTRACT_OPERATION_INVALID", "Invalid contract operation", 400
        ) from error
    if " " not in decoded:
        raise HTTPContractRouteError(
            "CONTRACT_OPERATION_INVALID", "Invalid contract operation", 400
        )
    operation_method, encoded_target = decoded.split(" ", 1)
    operation_method = operation_method.upper().strip()
    request_method = str(method or "").upper().strip()
    if operation_method != request_method:
        raise HTTPContractRouteError(
            "CONTRACT_METHOD_MISMATCH", "Contract operation method mismatch", 405
        )
    if operation_method not in _HTTP_METHODS:
        raise HTTPContractRouteError(
            "CONTRACT_METHOD_UNSUPPORTED",
            "Unsupported contract operation method",
            405,
        )

    parsed = urlsplit(encoded_target)
    if parsed.scheme or parsed.netloc or parsed.fragment or not _safe_target_path(parsed.path):
        raise HTTPContractRouteError(
            "CONTRACT_PATH_INVALID", "Invalid contract target path", 400
        )
    if not _registered_target(
        server,
        operation_method,
        parsed.path,
        namespace=requested_namespace,
    ):
        raise HTTPContractRouteError(
            "CONTRACT_OPERATION_UNKNOWN", "Unknown contract operation", 404
        )
    parsed_query = parse_qs(parsed.query, keep_blank_values=True)
    if any(len(values) != 1 for values in parsed_query.values()):
        raise HTTPContractRouteError(
            "CONTRACT_QUERY_INVALID", "Invalid contract target query", 400
        )
    return ResolvedHTTPContractRoute(
        operation_method,
        parsed.path,
        {key: values[0] for key, values in parsed_query.items() if values},
    )


def _registered_target(
    server: Any,
    method: str,
    path: str,
    *,
    namespace: str,
) -> bool:
    """Check the immutable Host route table without invoking a handler."""

    routes = getattr(server, "_contract_routes", None)
    if not isinstance(routes, Mapping):
        return False
    metadata = routes.get((method, path))
    if not isinstance(metadata, HTTPContractBinding):
        return False
    if (
        not metadata.route_namespace
        or not metadata.application_id
        or metadata.route_namespace != namespace
    ):
        return False
    _assert_binding_context_current(server, metadata)
    return True


def _assert_binding_context_current(server: Any, binding: HTTPContractBinding) -> None:
    """Reject a binding whose captured activation no longer matches the Host."""

    expected = {field: getattr(binding, field, "") for field in _CONTRACT_CONTEXT_FIELDS}
    if not any(expected.values()):
        return
    if not all(expected.values()):
        raise HTTPContractRouteError(
            "CONTRACT_MAP_STALE",
            "HTTP contract activation identity is incomplete",
            409,
        )
    session = getattr(server, "_dispatch_session", None)
    if session is None or any(
        getattr(session, field, None) != value for field, value in expected.items()
    ):
        raise HTTPContractRouteError(
            "CONTRACT_MAP_STALE",
            "HTTP contract activation identity is stale",
            409,
        )


def _safe_target_path(path: str) -> bool:
    if not path.startswith("/api/"):
        return False
    if path.startswith(HTTP_CONTRACT_ROUTE_PREFIX) or "\x00" in path or "\\" in path:
        return False
    if "//" in path or any(part in {".", ".."} for part in path.split("/")):
        return False
    decoded = path
    for _ in range(3):
        decoded = unquote(decoded)
        if (
            "\x00" in decoded
            or "\\" in decoded
            or "//" in decoded
            or any(part in {".", ".."} for part in decoded.split("/"))
        ):
            return False
    return True


def _validate_route_namespace(value: str) -> str:
    normalized = str(value or "").strip()
    if _NAMESPACE_RE.fullmatch(normalized) is None:
        raise HTTPContractRouteError("CONTRACT_PACK_INVALID", "Invalid contract pack", 400)
    try:
        return validate_canonical_id(normalized, field="contract pack")
    except ProtocolError as error:
        raise HTTPContractRouteError(
            "CONTRACT_PACK_INVALID", "Invalid contract pack", 400
        ) from error


__all__ = [
    "HTTP_CONTRACT_ROUTE_PREFIX",
    "HTTPContractBinding",
    "HTTPCapabilitySnapshot",
    "HTTPContractRouteError",
    "HTTPContractTarget",
    "ResolvedHTTPContractRoute",
    "contract_binding_map",
    "contract_route_prefix",
    "is_contract_route_path",
    "resolve_contract_route",
]
