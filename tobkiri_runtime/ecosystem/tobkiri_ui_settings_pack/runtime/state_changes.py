"""Captured model-state and recovery-diagnostic owner operations."""

from __future__ import annotations

import hashlib
import json
import re
from copy import deepcopy
from pathlib import Path
from typing import Any, Mapping

from core_runtime.host_provider_backend_v4 import (
    CapturedHostProviderV4,
    HostProviderCaptureContextV4,
    HostProviderContributionV4,
    HostProviderInvocationContextV4,
)
from ecosystem.tobkiri_ui_settings_pack.runtime.store import FrontendSettingsStore
from tobkiri_protocol.canonical import canonical_digest

PACK_ID = "tobkiri_ui_settings_pack"
MODEL_READ_FUNCTION = "tobkiri.ui.model-state.read"
MODEL_WRITE_FUNCTION = "tobkiri.ui.model-state.write"
DIAGNOSTIC_READ_FUNCTION = "tobkiri.ui.recovery-diagnostic.read"
DIAGNOSTIC_WRITE_FUNCTION = "tobkiri.ui.recovery-diagnostic.write"
MODEL_CONTRACT = "tobkiri.resource.ui.model-state.v1"
MODEL_ACTION = "tobkiri.action.ui.model-state.v1"
DIAGNOSTIC_CONTRACT = "tobkiri.resource.ui.recovery-diagnostic.v1"
DIAGNOSTIC_ACTION = "tobkiri.action.ui.recovery-diagnostic.v1"
MODEL_READ_OPERATION = "tobkiri_ui_settings_pack.model-state-read"
MODEL_WRITE_OPERATION = "tobkiri_ui_settings_pack.model-state-write"
DIAGNOSTIC_READ_OPERATION = "tobkiri_ui_settings_pack.recovery-diagnostic-read"
DIAGNOSTIC_WRITE_OPERATION = "tobkiri_ui_settings_pack.recovery-diagnostic-write"

_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{7,255}\Z")
_THINKING = frozenset({"none", "low", "medium", "high", "xhigh"})
_DIAGNOSTIC_FIELDS = frozenset({
    "schema_version", "event_id", "session_id", "source", "category",
    "level", "message", "fingerprint", "context_id", "privacy_mode", "detail",
})
_DETAIL_FIELDS = frozenset({
    "error_name", "error_code", "route", "line", "column", "stack",
    "component_stack", "reason_type", "http_status", "frame_count",
})
_SECRET_PATTERNS = (
    (r"\b(?:https?|wss?|file)://[^\s<>'\"\])}]+", "[url]"),
    (r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", "[email]"),
    (r"\b(?:authorization|cookie|set-cookie)\s*[:=]\s*[^\r\n,;]+", "[secret]"),
    (r"\b(?:bearer|basic)\s+[A-Za-z0-9._~+/=-]{8,}", "[secret]"),
    (r"\b(?:api[_-]?key|token|secret|password|credential)\b\s*[:=]\s*[^\s,;]+", "[secret]"),
    (r"(?:/Users/|/home/|/var/|/tmp/|/private/|/etc/)[^\s):\]}]+", "[path]"),
    (r"\b(?:[A-Fa-f0-9]{40,}|[A-Za-z0-9+/=_-]{80,})\b", "[opaque]"),
)


def _namespace(kind: str, profile_id: str, caller: str) -> str:
    return canonical_digest({"kind": kind, "profile_id": profile_id, "caller": caller})


def _text(value: Any, *, limit: int) -> str:
    if not isinstance(value, str):
        raise ValueError("text value is invalid")
    text = re.sub(r"[\x00-\x1f\x7f]+", " ", value)
    for pattern, replacement in _SECRET_PATTERNS:
        text = re.sub(pattern, replacement, text, flags=re.IGNORECASE)
    return re.sub(r"\s+", " ", text).strip()[:limit]


def _model_value(kind: Any, value: Any) -> tuple[str, Any]:
    if kind == "preferred_model":
        if not isinstance(value, str) or not value.strip() or len(value) > 256:
            raise ValueError("preferred model is invalid")
        return kind, value.strip()
    if kind == "thinking_level":
        if value not in _THINKING:
            raise ValueError("thinking level is invalid")
        return kind, value
    if kind == "deepthink_enabled":
        if type(value) is not bool:
            raise ValueError("deepthink state is invalid")
        return kind, value
    raise ValueError("model mutation kind is unsupported")


def _diagnostic(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) - _DIAGNOSTIC_FIELDS:
        raise ValueError("diagnostic fields are invalid")
    if value.get("schema_version") != "rumi.client_diagnostic.v2":
        raise ValueError("diagnostic schema is invalid")
    if value.get("privacy_mode") != "standard":
        raise PermissionError("diagnostic reporting is disabled")
    detail = value.get("detail", {})
    if not isinstance(detail, dict) or set(detail) - _DETAIL_FIELDS:
        raise ValueError("diagnostic detail is invalid")
    normalized_detail: dict[str, Any] = {}
    for key, item in detail.items():
        if key in {"line", "column", "http_status", "frame_count"}:
            if type(item) is not int or item < 0 or item > 10_000_000:
                raise ValueError("diagnostic numeric detail is invalid")
            normalized_detail[key] = item
        else:
            normalized_detail[key] = _text(item, limit=1200 if "stack" in key else 240)
    identifiers = {}
    for key in ("event_id", "session_id", "fingerprint", "context_id"):
        raw = value.get(key)
        if raw is not None:
            if not isinstance(raw, str) or not raw:
                raise ValueError("diagnostic identifier is invalid")
            identifiers[key] = hashlib.sha256(raw.encode()).hexdigest()[:24]
    if not all(key in identifiers for key in ("event_id", "session_id", "fingerprint")):
        raise ValueError("diagnostic identifiers are required")
    result = {
        "schema_version": "rumi.client_diagnostic.v2",
        **identifiers,
        "source": _text(value.get("source", "webapp"), limit=80),
        "category": _text(value.get("category", "frontend"), limit=80),
        "level": _text(value.get("level", "error"), limit=24),
        "message": _text(value.get("message", ""), limit=320),
        "detail": normalized_detail,
    }
    if not result["message"]:
        raise ValueError("diagnostic message is required")
    if len(json.dumps(result, ensure_ascii=False).encode()) > 8192:
        raise ValueError("diagnostic is too large")
    return result


class StateChangesHostFactoryV4:
    """Capture finite state operations and their Host-authenticated caller."""

    def __init__(self, function_id: str) -> None:
        self.function_id = function_id

    def capture(self, context: HostProviderCaptureContextV4) -> CapturedHostProviderV4:
        expected = {
            MODEL_READ_FUNCTION: (MODEL_CONTRACT, MODEL_READ_OPERATION),
            MODEL_WRITE_FUNCTION: (MODEL_ACTION, MODEL_WRITE_OPERATION),
            DIAGNOSTIC_READ_FUNCTION: (DIAGNOSTIC_CONTRACT, DIAGNOSTIC_READ_OPERATION),
            DIAGNOSTIC_WRITE_FUNCTION: (DIAGNOSTIC_ACTION, DIAGNOSTIC_WRITE_OPERATION),
        }[self.function_id]
        if not context.profile_id or context.user_data_root is None or len(context.provider_bindings) != 1:
            raise PermissionError("state operation capture is incomplete")
        binding = context.provider_bindings[0]
        operation = binding.operation
        if binding.function.function_id != self.function_id or (
            operation.contract_id, operation.operation_id
        ) != expected:
            raise PermissionError("state operation binding is invalid")
        domain_id = context.domain_ids.get((operation.contract_id, operation.operation_id, binding.principal_ref.value))
        if domain_id is None:
            raise PermissionError("state operation domain is unavailable")
        store = FrontendSettingsStore(
            context.user_data_root / "defaultspack" / "shared" / "frontend_settings.json"
        )

        def invoke(operation_id: str, payload: Mapping[str, Any], invocation: HostProviderInvocationContextV4) -> Mapping[str, Any]:
            invocation.assert_current()
            caller = invocation.presentation_owner_principal_id
            session = invocation.presentation_owner_session_id
            if not caller or not session or payload.get("profile_id") != context.profile_id:
                raise PermissionError("state operation caller is unavailable")
            kind = "model" if operation_id in {MODEL_READ_OPERATION, MODEL_WRITE_OPERATION} else "diagnostic"
            namespace = _namespace(kind, context.profile_id, caller)
            state_ref = f"tobkiri.ui.{kind}:{namespace}"
            if operation_id in {MODEL_READ_OPERATION, DIAGNOSTIC_READ_OPERATION}:
                if set(payload) - {"profile_id", "_session_id"}:
                    raise PermissionError("state read request is invalid")
                snapshot = store.read_snapshot()
                revisions = snapshot.get("_settings_state_revisions", {})
                revision = revisions.get(state_ref, 0) if isinstance(revisions, dict) else 0
                result: dict[str, Any] = {"namespace": namespace, "revision": revision}
                if operation_id == MODEL_READ_OPERATION:
                    models = snapshot.get("models", {})
                    models = models if isinstance(models, dict) else {}
                    result["values"] = {
                        "preferred_model": str(models.get("preferred_model") or "stub/default"),
                        "thinking_level": str(models.get("thinking_level") or "medium"),
                        "deepthink_enabled": bool(models.get("deepthink_enabled", False)),
                    }
                else:
                    events = snapshot.get("recovery_diagnostics", {}).get(namespace, [])
                    result["record_count"] = len(events) if isinstance(events, list) else 0
                return result
            allowed = {"profile_id", "expected_revision", "mutation_id", "_session_id"}
            allowed |= {"kind", "value"} if operation_id == MODEL_WRITE_OPERATION else {"diagnostic"}
            if set(payload) - allowed or not {"expected_revision", "mutation_id"} <= set(payload):
                raise PermissionError("state write request is invalid")
            expected_revision = payload["expected_revision"]
            mutation_id = payload["mutation_id"]
            if type(expected_revision) is not int or expected_revision < 0 or not isinstance(mutation_id, str) or _ID.fullmatch(mutation_id) is None:
                raise ValueError("state mutation control is invalid")
            if operation_id == MODEL_WRITE_OPERATION:
                model_kind, model_value = _model_value(payload.get("kind"), payload.get("value"))
                normalized = {"kind": model_kind, "value": model_value}
            elif operation_id == DIAGNOSTIC_WRITE_OPERATION:
                normalized = {"diagnostic": _diagnostic(payload.get("diagnostic"))}
            else:
                raise PermissionError("state write operation is invalid")
            fingerprint = canonical_digest(normalized)
            receipt = canonical_digest({"namespace": namespace, "mutation_id": mutation_id, "input": fingerprint})

            def mutate(document: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
                if operation_id == MODEL_WRITE_OPERATION:
                    models = document.get("models", {})
                    models = dict(models) if isinstance(models, dict) else {}
                    models[model_kind] = model_value
                    if model_kind == "preferred_model":
                        models["main_model"] = model_value
                        slots = models.get("model_slots", {})
                        models["model_slots"] = {**(slots if isinstance(slots, dict) else {}), "main": model_value}
                    document["models"] = models
                    result = {"kind": model_kind, "value": model_value}
                else:
                    all_events = document.get("recovery_diagnostics", {})
                    all_events = dict(all_events) if isinstance(all_events, dict) else {}
                    events = all_events.get(namespace, [])
                    events = list(events) if isinstance(events, list) else []
                    events.append(normalized["diagnostic"])
                    all_events[namespace] = events[-64:]
                    document["recovery_diagnostics"] = all_events
                    result = {"recorded": True, "diagnostic_id": f"diag_{normalized['diagnostic']['event_id']}"}
                return document, {**result, "namespace": namespace, "mutation_id": mutation_id, "receipt": receipt}

            result = store.mutate_state(
                state_ref, mutate, expected_revision=expected_revision,
                idempotency_key=mutation_id, request_fingerprint=fingerprint,
            )
            return {**deepcopy(result), "caller_session_digest": hashlib.sha256(session.encode()).hexdigest()}

        return CapturedHostProviderV4((HostProviderContributionV4(
            contract_id=operation.contract_id,
            contract_version=operation.contract_version,
            operation_id=operation.operation_id,
            principal_id=binding.principal_ref.value,
            artifact_digest=binding.artifact.digest,
            implementation_digest=binding.function.implementation_digest,
            domain_id=domain_id,
            invoke=invoke,
        ),), lambda: None)


HOST_PROVIDER_FACTORY = {
    function_id: StateChangesHostFactoryV4(function_id)
    for function_id in (
        MODEL_READ_FUNCTION, MODEL_WRITE_FUNCTION,
        DIAGNOSTIC_READ_FUNCTION, DIAGNOSTIC_WRITE_FUNCTION,
    )
}
