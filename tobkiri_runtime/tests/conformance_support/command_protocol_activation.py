"""Shared release-test policy for exact captured Command Protocol routes."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from pathlib import PurePosixPath
from typing import Any
from urllib.parse import unquote


_COMMAND_NAMESPACE = "/api/command-protocol/v1"
_HIGH_RISK_ROUTE = f"{_COMMAND_NAMESPACE}/high-risk"
_HIGH_RISK_TARGET = {
    "contribution_id": "defaults.command-protocol.high-risk",
    "contract_id": "tobkiri.service.command.high-risk.v1",
    "operation_id": "high_risk_command.manage",
    "provider_id": "rumi_command_protocol_pack.high-risk-command.service",
    "function_id": "rumi_command_protocol_pack.high-risk-command.service",
}
_HIGH_RISK_PAYLOAD_KEYS = frozenset(
    {"phase", "invocation_id", "command_ref", "arguments", "presentation"}
)
_CATALOG_TARGET = {
    "contribution_id": "defaults.commands.catalog.read",
    "contract_id": "tobkiri.resource.command.catalog.v1",
    "operation_id": "command.catalog.read",
    "provider_id": "rumi_command_protocol_pack.catalog.read",
    "function_id": "rumi_command_protocol_pack.catalog.read",
}

# These aliases are conservative test policy, not production URL rewriting.
COMMAND_PROTOCOL_HTTP_CASES = (
    ("GET", "/api/command-protocol/v1", None),
    ("GET", "/api/%63ommand-protocol/v1/catalog", None),
    ("POST", "/api/command-protocol/v1/invoke", {"command_ref": "help"}),
    ("POST", "/api/command-protocol%2fv1/invoke", {}),
    ("POST", "/api/command-protocol/v1/resume", {"invocation_id": "test"}),
    ("POST", "/api/command-protocol/v1/%72esume", {}),
    ("POST", "/api/command-protocol/v1/offline", {"action": "pending"}),
    ("POST", "/api/%2563ommand-protocol/v1/offline", {}),
    (
        "POST",
        "/api/command-protocol/v1/invocations/events/query",
        {"action": "replay", "invocation_id": "test"},
    ),
    ("POST", "/api/command-protocol/v1/invocations%2fevents%2fquery", {}),
)


def is_conservative_command_protocol_alias(path: object) -> bool:
    """Recognize the namespace root/descendants after bounded decoding."""

    normalized = str(path or "").partition("?")[0]
    for _ in range(len(normalized)):
        decoded = unquote(normalized)
        if decoded == normalized:
            break
        if len(decoded) >= len(normalized):
            break
        normalized = decoded
    normalized = re.sub(r"/+", "/", normalized.replace("\\", "/")).casefold()
    return normalized == _COMMAND_NAMESPACE or normalized.startswith(f"{_COMMAND_NAMESPACE}/")


def route_pattern_exposes_command_protocol(pattern: object) -> bool:
    """Reject literals and canonical catch-alls that can match command URLs."""

    value = str(pattern or "")
    if is_conservative_command_protocol_alias(value):
        return True
    from ecosystem.defaultspack.transport.registry import compile_http_route_pattern

    try:
        compiled = compile_http_route_pattern(value)
    except re.error:
        return True
    return any(
        compiled.fullmatch(path) is not None for _method, path, _body in COMMAND_PROTOCOL_HTTP_CASES
    )


def load_current_signed_application_bindings() -> tuple[Any, ...]:
    """Check the bundled Profile's selected route map without activating a Host."""
    from ecosystem.defaultspack.defaultspack.frontend_contract_loader import (
        load_frontend_contract_bindings,
        resolve_frontend_contract_map_path,
    )
    from ecosystem.defaultspack.defaultspack.profile_runtime_composition import (
        defaultspack_profile_bundle_root,
    )
    from ecosystem.defaultspack.domain.runtime_v4 import BundledCatalog

    runtime_root = Path(__file__).resolve().parents[2]
    catalog = BundledCatalog.load(defaultspack_profile_bundle_root())
    selected = [item for item in catalog.profiles["defaults"]["packs"]
                if item.get("role") == "application"]
    if len(selected) != 1:
        raise RuntimeError("bundled Profile Application selection is ambiguous")
    application = catalog.packs[selected[0]["pack_id"]]
    pinned_digest = selected[0].get("artifact_digest")
    if pinned_digest is not None and pinned_digest != application["pack"]["artifact_digest"]:
        raise RuntimeError("bundled Profile Application artifact is stale")
    artifact_root = runtime_root / "ecosystem" / "defaultspack"
    bindings = tuple(load_frontend_contract_bindings(
        resolve_frontend_contract_map_path(application, artifact_root),
        application,
        artifact_root=artifact_root,
    ))
    _assert_only_selected_route_map(
        application, artifact_root,
        selected_path=PurePosixPath(bindings[0].artifact_path),
    )
    return bindings


def load_captured_application_bindings(
    catalog: Any,
    active: Any,
    artifact_root: Path,
) -> tuple[Any, ...]:
    """Use startup's Application selection and route-map path resolver."""

    from ecosystem.defaultspack.defaultspack.frontend_contract_loader import (
        load_frontend_contract_bindings,
        resolve_frontend_contract_map_path,
    )
    from ecosystem.defaultspack.defaultspack.desktop_app import (
        _active_application_manifest,
    )

    application = _active_application_manifest(catalog, active)
    map_path = resolve_frontend_contract_map_path(application, artifact_root)
    bindings = tuple(
        load_frontend_contract_bindings(
            map_path,
            application,
            artifact_root=artifact_root,
        )
    )
    _assert_only_selected_route_map(
        application,
        artifact_root,
        selected_path=PurePosixPath(bindings[0].artifact_path),
    )
    return bindings


def _assert_only_selected_route_map(
    application: Any,
    artifact_root: Path,
    *,
    selected_path: PurePosixPath,
) -> None:
    """Fail closed on any additional signed route-map document or type."""

    discovered: set[PurePosixPath] = set()
    for artifact in application.get("artifacts", ()):
        if not isinstance(artifact, dict) or artifact.get("kind") != "asset":
            continue
        relative = PurePosixPath(str(artifact.get("path") or ""))
        if (
            relative.is_absolute()
            or not relative.parts
            or any(part in {"", ".", ".."} for part in relative.parts)
        ):
            raise RuntimeError("signed Application asset path is unsafe")
        path = artifact_root.joinpath(*relative.parts)
        try:
            raw = path.read_bytes()
            document = json.loads(raw)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            continue
        schema = document.get("schema") if isinstance(document, dict) else None
        is_route_map = isinstance(document, dict) and (
            isinstance(document.get("routes"), list)
            or (isinstance(schema, str) and schema.startswith("io.tobkiri.frontend-contract-map."))
        )
        if not is_route_map:
            continue
        expected_digest = artifact.get("digest")
        actual_digest = "sha256:" + hashlib.sha256(raw).hexdigest()
        if expected_digest != actual_digest:
            raise RuntimeError("signed Application route-map digest is stale")
        discovered.add(relative)
    if discovered != {selected_path}:
        raise RuntimeError("unknown signed Application route-map type or path")


def command_protocol_binding_findings(
    bindings: tuple[Any, ...],
) -> list[dict[str, object]]:
    """Return Command Protocol bindings outside the captured V4 adapter."""

    findings: list[dict[str, object]] = []
    for binding in bindings:
        path = getattr(binding, "path", None)
        if not route_pattern_exposes_command_protocol(path):
            continue
        targets = tuple(getattr(binding, "targets", ()))
        exact_high_risk = (
            getattr(binding, "method", "").upper() == "POST"
            and path == _HIGH_RISK_ROUTE
            and getattr(binding, "presentation", None) == "broker_result"
            and len(targets) == 1
            and all(
                getattr(targets[0], field, None) == expected
                for field, expected in _HIGH_RISK_TARGET.items()
            )
            and getattr(targets[0], "allowed_payload_keys", None) == _HIGH_RISK_PAYLOAD_KEYS
        )
        exact_catalog = (
            getattr(binding, "method", "").upper() == "GET"
            and path == f"{_COMMAND_NAMESPACE}/catalog"
            and getattr(binding, "presentation", None) == "broker_result"
            and len(targets) == 1
            and all(
                getattr(targets[0], field, None) == expected
                for field, expected in _CATALOG_TARGET.items()
            )
            and getattr(targets[0], "allowed_payload_keys", None) == frozenset()
        )
        if exact_high_risk or exact_catalog:
            continue
        findings.append(
            {
                "method": getattr(binding, "method", None),
                "route": path,
                "artifact_path": getattr(binding, "artifact_path", None),
                "artifact_digest": getattr(binding, "artifact_digest", None),
            }
        )
    return findings


def file_snapshot(path: Path) -> bytes | None:
    """Return exact file bytes, or None when the mutation target is absent."""

    return path.read_bytes() if path.exists() else None


__all__ = [
    "COMMAND_PROTOCOL_HTTP_CASES",
    "command_protocol_binding_findings",
    "file_snapshot",
    "is_conservative_command_protocol_alias",
    "load_captured_application_bindings",
    "load_current_signed_application_bindings",
    "route_pattern_exposes_command_protocol",
]
