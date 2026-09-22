"""Host-owned frontend contract route resolution.

The web application is only allowed to address the canonical contract
endpoint.  This module decodes the opaque operation token used by that
endpoint and validates the resolved implementation route before the normal
HTTP dispatcher sees it.  The implementation route remains Host-owned; the
frontend never gets to select an arbitrary URL or handler.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re
from typing import Any, Mapping
from urllib.parse import parse_qs, unquote, urlsplit


DEFAULT_CONTRACT_PACK_ID = "defaultspack"
CONTRACT_ROUTE_PREFIX = "/api/contracts/defaultspack/"
_PACK_ID_RE = re.compile(r"^[a-z][a-z0-9]*(?:[._-][a-z0-9]+)*$")


class ContractRouteError(ValueError):
    """Raised when a canonical frontend operation cannot be resolved."""

    def __init__(self, code: str, message: str, status: int = 404) -> None:
        super().__init__(message)
        self.code = code
        self.status = status


@dataclass(frozen=True)
class ResolvedContractRoute:
    """A validated implementation target and its query projection."""

    method: str
    path: str
    query: dict[str, str]


@dataclass(frozen=True)
class FrontendContractTarget:
    """One exact committed contribution mapped to a Broker operation."""

    contribution_id: str
    contract_id: str
    operation_id: str
    provider_id: str
    function_id: str
    allowed_payload_keys: frozenset[str] = frozenset()
    owner_pack_id: str = DEFAULT_CONTRACT_PACK_ID
    artifact_digest: str = ""


@dataclass(frozen=True)
class FrontendContractBinding:
    """One exact frontend route and its selected contribution targets."""

    method: str
    path: str
    presentation: str
    targets: tuple[FrontendContractTarget, ...]


def load_frontend_contract_bindings(
    map_path: Path,
    application_manifest: Mapping[str, Any],
) -> tuple[FrontendContractBinding, ...]:
    """Load the digest-pinned application map without discovery or fallback."""

    expected_artifact = next(
        (
            artifact
            for artifact in application_manifest.get("artifacts", ())
            if isinstance(artifact, Mapping)
            and artifact.get("path") == "defaultspack/frontend_contract_map.v4.json"
            and artifact.get("kind") == "asset"
        ),
        None,
    )
    if expected_artifact is None:
        raise ContractRouteError(
            "CONTRACT_MAP_UNAVAILABLE", "Frontend contract map is not committed", 500
        )
    raw = map_path.read_bytes()
    actual_digest = "sha256:" + hashlib.sha256(raw).hexdigest()
    if actual_digest != expected_artifact.get("digest"):
        raise ContractRouteError("CONTRACT_MAP_STALE", "Frontend contract map digest changed", 500)
    try:
        document = json.loads(raw)
    except (OSError, json.JSONDecodeError) as error:
        raise ContractRouteError(
            "CONTRACT_MAP_INVALID", "Frontend contract map is invalid", 500
        ) from error
    if not isinstance(document, dict) or set(document) != {
        "schema",
        "pack_id",
        "routes",
    }:
        raise ContractRouteError(
            "CONTRACT_MAP_INVALID", "Frontend contract map fields are invalid", 500
        )
    if (
        document.get("schema") != "io.tobkiri.frontend-contract-map.v4"
        or document.get("pack_id") != "defaultspack"
        or not isinstance(document.get("routes"), list)
    ):
        raise ContractRouteError(
            "CONTRACT_MAP_INVALID", "Frontend contract map identity is invalid", 500
        )
    bindings: list[FrontendContractBinding] = []
    for route in document["routes"]:
        if not isinstance(route, dict) or set(route) != {
            "method",
            "path",
            "presentation",
            "targets",
        }:
            raise ContractRouteError(
                "CONTRACT_MAP_INVALID", "Frontend route fields are invalid", 500
            )
        targets: list[FrontendContractTarget] = []
        if not isinstance(route["targets"], list) or not route["targets"]:
            raise ContractRouteError("CONTRACT_MAP_INVALID", "Frontend route has no targets", 500)
        for target in route["targets"]:
            if not isinstance(target, dict) or set(target) != {
                "contribution_id",
                "contract_id",
                "operation_id",
                "provider_id",
                "function_id",
                "allowed_payload_keys",
            }:
                raise ContractRouteError(
                    "CONTRACT_MAP_INVALID", "Frontend target fields are invalid", 500
                )
            allowed = target["allowed_payload_keys"]
            if not isinstance(allowed, list) or any(
                not isinstance(key, str) or not key for key in allowed
            ):
                raise ContractRouteError(
                    "CONTRACT_MAP_INVALID", "Frontend target payload is invalid", 500
                )
            targets.append(
                FrontendContractTarget(
                    contribution_id=str(target["contribution_id"]),
                    contract_id=str(target["contract_id"]),
                    operation_id=str(target["operation_id"]),
                    provider_id=str(target["provider_id"]),
                    function_id=str(target["function_id"]),
                    allowed_payload_keys=frozenset(allowed),
                )
            )
        bindings.append(
            FrontendContractBinding(
                method=str(route["method"]).upper(),
                path=str(route["path"]),
                presentation=str(route["presentation"]),
                targets=tuple(targets),
            )
        )
    contract_binding_map(tuple(bindings))
    return tuple(bindings)


def contract_binding_map(
    bindings: tuple[FrontendContractBinding, ...],
) -> dict[tuple[str, str], FrontendContractBinding]:
    """Build an exact route map, rejecting ambiguous Host ownership."""

    result: dict[tuple[str, str], FrontendContractBinding] = {}
    for binding in bindings:
        key = (binding.method.upper(), binding.path)
        if key in result:
            raise ContractRouteError(
                "CONTRACT_OPERATION_DUPLICATE",
                "Frontend contract operation is duplicated",
                500,
            )
        result[key] = binding
    return result


def is_contract_route_path(path: str) -> bool:
    """Return whether *path* is the canonical frontend contract endpoint."""

    return str(path or "").startswith(CONTRACT_ROUTE_PREFIX)


def contract_route_prefix(pack_id: str = DEFAULT_CONTRACT_PACK_ID) -> str:
    """Return the canonical endpoint prefix for one verified frontend pack."""

    normalized = str(pack_id or "").strip()
    if not _PACK_ID_RE.fullmatch(normalized):
        raise ContractRouteError("CONTRACT_PACK_INVALID", "Invalid contract pack", 400)
    return f"/api/contracts/{normalized}/"


def _safe_target_path(path: str) -> bool:
    if not path.startswith("/api/"):
        return False
    if path.startswith("/api/contracts/") or "\x00" in path or "\\" in path:
        return False
    if "//" in path:
        return False
    segments = path.split("/")
    if any(segment in {".", ".."} for segment in segments):
        return False
    # A percent-encoded slash is retained for the normal route matcher.  This
    # preserves identifiers such as ``operations%2Fcompany`` while the
    # dispatcher's existing ``_is_safe_path_param`` rejects traversal tokens.
    decoded = path
    for _ in range(3):
        decoded = unquote(decoded)
        decoded_segments = decoded.split("/")
        if "\x00" in decoded or "\\" in decoded or "//" in decoded:
            return False
        if any(segment in {".", ".."} for segment in decoded_segments):
            return False
    return True


def _registered_target(
    server: Any,
    method: str,
    path: str,
    *,
    families: tuple[str, ...] | None = None,
) -> bool:
    """Check the live Host route tables without invoking a handler."""

    contract_routes = getattr(server, "_contract_routes", None)
    if isinstance(contract_routes, Mapping):
        route_key = (method, path)
        if route_key in contract_routes:
            metadata = contract_routes[route_key]
            requires_approval = isinstance(metadata, Mapping) and bool(
                metadata.get("approval_required")
            )
            if requires_approval:
                approval_check = getattr(server, "_contract_approval_check", None)
                approved = bool(approval_check(method, path)) if callable(approval_check) else False
                if not approved:
                    raise ContractRouteError(
                        "CONTRACT_APPROVAL_REQUIRED",
                        "Contract operation requires Host approval",
                        403,
                    )
            return True
        # A pack-provided contract map is authoritative.  Do not fall back to
        # another pack's route registry or the coarse defaultspack families.
        return False

    exact = getattr(server, "_api_route_exact", {})
    if isinstance(exact, Mapping) and (method, path) in exact:
        return True
    for entry in getattr(server, "_api_route_patterns", ()) or ():
        try:
            route_method, pattern, _param_names, _route_entry = entry
        except (TypeError, ValueError):
            continue
        if str(route_method).upper() == method and pattern.match(path) is not None:
            return True

    # Canonical frontend admission is map-only.  The historical Defaultspack
    # route registry included direct Flow/executor handlers and must never be
    # consulted as a production fallback.
    return False


def resolve_contract_route(
    server: Any,
    method: str,
    request_path: str,
    *,
    pack_id: str = DEFAULT_CONTRACT_PACK_ID,
    route_families: tuple[str, ...] | None = None,
) -> ResolvedContractRoute | None:
    """Decode and validate a canonical operation token.

    ``None`` means the request is not a canonical contract request.  Invalid
    canonical requests raise :class:`ContractRouteError` and must be returned
    to the caller as a closed error rather than falling through to a legacy
    route.
    """

    prefix = contract_route_prefix(pack_id)
    if not str(request_path or "").startswith(prefix):
        return None
    token = str(request_path)[len(prefix) :]
    if not token or "/" in token:
        raise ContractRouteError("CONTRACT_OPERATION_INVALID", "Invalid contract operation", 400)
    try:
        decoded = unquote(token)
    except Exception as exc:  # pragma: no cover - urllib is defensive here
        raise ContractRouteError(
            "CONTRACT_OPERATION_INVALID", "Invalid contract operation", 400
        ) from exc
    if " " not in decoded:
        raise ContractRouteError("CONTRACT_OPERATION_INVALID", "Invalid contract operation", 400)
    encoded_method, encoded_target = decoded.split(" ", 1)
    operation_method = encoded_method.upper().strip()
    request_method = str(method or "").upper().strip()
    if operation_method != request_method:
        raise ContractRouteError(
            "CONTRACT_METHOD_MISMATCH", "Contract operation method mismatch", 405
        )
    if operation_method not in {"GET", "POST", "PUT", "PATCH", "DELETE"}:
        raise ContractRouteError(
            "CONTRACT_METHOD_UNSUPPORTED", "Unsupported contract operation method", 405
        )

    parsed = urlsplit(encoded_target)
    if parsed.scheme or parsed.netloc or parsed.fragment:
        raise ContractRouteError("CONTRACT_PATH_INVALID", "Invalid contract target path", 400)
    target_path = parsed.path
    if not _safe_target_path(target_path):
        raise ContractRouteError("CONTRACT_PATH_INVALID", "Invalid contract target path", 400)
    if not _registered_target(
        server,
        operation_method,
        target_path,
        families=route_families,
    ):
        raise ContractRouteError(
            "CONTRACT_OPERATION_UNKNOWN", "Unknown frontend contract operation", 404
        )
    parsed_query = parse_qs(parsed.query, keep_blank_values=True)
    if any(len(values) != 1 for values in parsed_query.values()):
        raise ContractRouteError("CONTRACT_QUERY_INVALID", "Invalid contract target query", 400)
    query = {key: values[0] for key, values in parsed_query.items() if values}
    return ResolvedContractRoute(operation_method, target_path, query)


__all__ = [
    "CONTRACT_ROUTE_PREFIX",
    "ContractRouteError",
    "FrontendContractBinding",
    "FrontendContractTarget",
    "ResolvedContractRoute",
    "contract_binding_map",
    "contract_route_prefix",
    "is_contract_route_path",
    "load_frontend_contract_bindings",
    "resolve_contract_route",
]
