"""Shared release-test policy for keeping Command Protocol transport dark."""

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

# These aliases are conservative test policy, not production URL rewriting.
COMMAND_PROTOCOL_HTTP_CASES = (
    ("GET", "/api/command-protocol/v1", None),
    ("GET", "/api/command-protocol/v1/catalog", None),
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
    """Use the production capture loader for the verified bundled Application."""

    from ecosystem.defaultspack.defaultspack.runtime_composition import (
        defaultspack_runtime_capture_inputs,
    )
    from ecosystem.defaultspack.domain.runtime_v4 import BundledCatalog

    runtime_root = Path(__file__).resolve().parents[2]
    capture = defaultspack_runtime_capture_inputs()
    catalog = BundledCatalog.load(capture.bundle_root)
    bindings = tuple(capture.contract_bindings)
    application_ids = {binding.application_id for binding in bindings}
    if len(application_ids) != 1:
        raise RuntimeError("production Application route identity is ambiguous")
    application = catalog.packs[next(iter(application_ids))]
    _assert_only_selected_route_map(
        application,
        runtime_root / "ecosystem" / "defaultspack",
        selected_path=PurePosixPath(bindings[0].artifact_path),
    )
    return tuple(bindings)


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
        if exact_high_risk:
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
