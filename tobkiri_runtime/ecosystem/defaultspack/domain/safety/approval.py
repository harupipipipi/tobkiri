from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import threading
import time
import uuid
from dataclasses import asdict, dataclass
from typing import Any

from .approval_state_json import (
    clear_approval_state_mirrors,
    load_approval_state_requests,
    normalize_request as normalize_json_approval_request,
    refresh_approval_state_mirrors,
)
from .approval_store import get_approval_store, persist_runtime_secret_for_broker
from ..host_bridge.viewer_broker_client import ViewerBrokerClient
from core_runtime.host_contract import host_contract_value


_TOKEN_VERSION = "v1"
_DEFAULT_EXPIRES_IN_SECONDS = 300
_RUNTIME_SECRET = (
    host_contract_value("approval_runtime_secret")
    or get_approval_store().get_or_create_runtime_secret()
)
persist_runtime_secret_for_broker(_RUNTIME_SECRET)
_LOCK = threading.RLock()
_DEBUG_RESUME_HANDLES: dict[str, dict[str, Any]] = {}
_REQUESTS: dict[str, "ApprovalRequest"] = {}
_USED_TOKEN_IDS: set[str] = set()

_ARG_HASH_IGNORE_KEYS = {
    "approval_token",
    "approved",
    "computer_use_haze_sequence_id",
    "computer_use_sequence_id",
    "_headers",
    "_method",
    "_raw_body",
    "_raw_body_base64",
}


@dataclass
class ApprovalRequest:
    request_id: str
    operation: str
    risk_level: str
    args_hash: str
    details: dict[str, Any]
    created_at: int
    expires_at: int
    status: str = "pending"
    decision_at: int | None = None
    debug_session_id: str = ""
    lease_epoch: int = 0
    debug_run_id: str = ""
    workspace_identity_digest: str = ""
    pack_id: str = ""
    profile_id: str = ""
    conversation_id: str = ""
    operation_owner: str = ""


@dataclass
class ApprovalDecision:
    request_id: str
    status: str
    approved: bool
    token: str = ""
    expires_at: int | None = None
    reason: str = ""


@dataclass
class TokenVerification:
    valid: bool
    code: str = ""
    message: str = ""
    request_id: str = ""


def _request_from_mapping(value: dict[str, Any] | None) -> ApprovalRequest | None:
    if not value:
        return None
    return ApprovalRequest(
        request_id=str(value.get("request_id") or ""),
        operation=str(value.get("operation") or ""),
        risk_level=str(value.get("risk_level") or "high"),
        args_hash=str(value.get("args_hash") or ""),
        details=dict(value.get("details") or {}),
        created_at=int(value.get("created_at") or 0),
        expires_at=int(value.get("expires_at") or 0),
        status=str(value.get("status") or "pending"),
        decision_at=value.get("decision_at"),
        debug_session_id=str(value.get("debug_session_id") or ""),
        lease_epoch=int(value.get("lease_epoch") or 0),
        debug_run_id=str(value.get("debug_run_id") or ""),
        workspace_identity_digest=str(value.get("workspace_identity_digest") or ""),
        pack_id=str(value.get("pack_id") or ""),
        profile_id=str(value.get("profile_id") or ""),
        conversation_id=str(value.get("conversation_id") or ""),
        operation_owner=str(value.get("operation_owner") or ""),
    )


def _now() -> int:
    return int(time.time())


def _b64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64url_decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _canonicalize(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            str(key): _canonicalize(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
            if str(key) not in _ARG_HASH_IGNORE_KEYS
        }
    if isinstance(value, (list, tuple)):
        return [_canonicalize(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def hash_arguments(args: dict[str, Any] | None) -> str:
    canonical = json.dumps(
        _canonicalize(args or {}),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def display_summary(operation: str, args: dict[str, Any] | None) -> str:
    args = args or {}
    if operation.startswith("file."):
        return f"{operation}: {args.get('path') or args.get('snapshot_id') or '<workspace>'}"
    if operation.startswith("terminal."):
        return f"{operation}: {args.get('command') or '<command>'}"
    if operation.startswith("git."):
        target = args.get("branch") or args.get("remote") or args.get("message") or "<repository>"
        return f"{operation}: {target}"
    return operation


def _request_payload(request: ApprovalRequest) -> dict[str, Any]:
    payload = asdict(request)
    payload["display_summary"] = display_summary(request.operation, request.details)
    return payload


def _stored_approval_payloads() -> list[dict[str, Any]]:
    payloads: list[dict[str, Any]] = []
    for item in get_approval_store().list_requests(include_expired=True, limit=500):
        request = _request_from_mapping(item)
        if request is not None:
            payloads.append(_request_payload(request))
    return payloads


def _refresh_approval_state_mirrors_from_store() -> None:
    refresh_approval_state_mirrors(_stored_approval_payloads())


def _request_is_visible(
    request: dict[str, Any],
    status: str | None,
    *,
    include_expired: bool,
    now: int,
) -> bool:
    if status and str(request.get("status") or "") != str(status):
        return False
    if not include_expired and int(request.get("expires_at") or 0) < now:
        return False
    return True


def _active_debug_binding(details: dict[str, Any]) -> dict[str, Any]:
    """Snapshot the active Launcher lease when a request is created.

    Failure to read the broker leaves the request outside every debug session.
    Such a request may still be handled by the native interactive UI, but the
    debug CLI cannot discover or sign it.
    """
    try:
        broker = ViewerBrokerClient.from_environment()
        if not broker.available():
            return {}
        response = broker.debug_approval_status()
    except Exception:
        return {}
    status = response.get("status") if isinstance(response, dict) else None
    if not isinstance(status, dict) or status.get("state") != "active":
        return {}
    required = {
        "debug_session_id": str(status.get("session_id") or ""),
        "lease_epoch": int(status.get("lease_epoch") or 0),
        "debug_run_id": str(status.get("run_id") or ""),
        "workspace_identity_digest": str(status.get("workspace_digest") or ""),
        "pack_id": str(status.get("pack_id") or ""),
        "profile_id": str(status.get("profile_id") or ""),
    }
    if (
        not required["debug_session_id"]
        or required["lease_epoch"] <= 0
        or not required["debug_run_id"]
        or len(required["workspace_identity_digest"]) != 64
        or not required["pack_id"]
        or not required["profile_id"]
    ):
        return {}
    declared_pack = str(details.get("pack_id") or details.get("owner_pack") or "")
    declared_profile = str(details.get("profile_id") or "")
    if declared_pack and declared_pack != required["pack_id"]:
        return {}
    if declared_profile and declared_profile != required["profile_id"]:
        return {}
    return {
        **required,
        "conversation_id": str(
            details.get("conversation_owner")
            or details.get("conversation_id")
            or details.get("profile_id")
            or required["profile_id"]
        ),
        "operation_owner": str(
            details.get("operation_owner")
            or details.get("owner_pack")
            or details.get("pack_id")
            or required["pack_id"]
        ),
    }


def create_approval_request(
    operation: str,
    risk_level: str,
    args: dict[str, Any] | None = None,
    *,
    expires_in: int = _DEFAULT_EXPIRES_IN_SECONDS,
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    now = _now()
    request_details = dict(details or {})
    debug_binding = _active_debug_binding(request_details)
    request = ApprovalRequest(
        request_id="apr_" + uuid.uuid4().hex,
        operation=str(operation),
        risk_level=str(risk_level or "high"),
        args_hash=hash_arguments(args if args is not None else (details or {})),
        details=request_details,
        created_at=now,
        expires_at=now + max(1, int(expires_in or _DEFAULT_EXPIRES_IN_SECONDS)),
        **debug_binding,
    )
    with _LOCK:
        _REQUESTS[request.request_id] = request
        get_approval_store().save_request(request)
        _refresh_approval_state_mirrors_from_store()
    payload = _request_payload(request)
    payload["display_summary"] = display_summary(operation, args or details or {})
    return payload


def deny(request_id: str, reason: str = "") -> dict[str, Any]:
    with _LOCK:
        request = _REQUESTS.get(str(request_id)) or _request_from_mapping(
            get_approval_store().get_request(str(request_id))
        )
        if request is None:
            return asdict(
                ApprovalDecision(
                    str(request_id), "missing", False, reason="approval request not found"
                )
            )
        now = _now()
        if request.expires_at < now and request.status == "pending":
            get_approval_store().settle_request(
                request.request_id,
                "expired",
                allowed_statuses=("pending",),
                decision_at=now,
            )
            request.status = "expired"
            request.decision_at = now
            _REQUESTS[request.request_id] = request
            _refresh_approval_state_mirrors_from_store()
            return asdict(
                ApprovalDecision(
                    request.request_id,
                    request.status,
                    False,
                    reason="approval request expired",
                )
            )
        settled, latest = get_approval_store().settle_request(
            request.request_id,
            "denied",
            allowed_statuses=("pending",),
            decision_at=now,
        )
        if not settled:
            latest_request = _request_from_mapping(latest)
            status = latest_request.status if latest_request else request.status
            return asdict(
                ApprovalDecision(
                    request.request_id,
                    status,
                    False,
                    reason=f"approval request already settled as '{status}'",
                )
            )
        request.status = "denied"
        request.decision_at = now
        _REQUESTS[request.request_id] = request
        _refresh_approval_state_mirrors_from_store()
        return asdict(ApprovalDecision(request.request_id, request.status, False, reason=reason))


def mark_obsolete(request_id: str, reason: str = "") -> dict[str, Any]:
    with _LOCK:
        request = _REQUESTS.get(str(request_id)) or _request_from_mapping(
            get_approval_store().get_request(str(request_id))
        )
        if request is None:
            return asdict(
                ApprovalDecision(
                    str(request_id), "missing", False, reason="approval request not found"
                )
            )
        if request.status == "consumed":
            return asdict(
                ApprovalDecision(
                    request.request_id,
                    request.status,
                    False,
                    reason="approval request already consumed",
                )
            )
        if request.status == "denied":
            return asdict(
                ApprovalDecision(
                    request.request_id, request.status, False, reason="approval request denied"
                )
            )
        if request.status == "obsolete":
            return asdict(
                ApprovalDecision(request.request_id, request.status, False, reason=reason)
            )
        if request.status not in {"pending", "approved", "expired"}:
            return asdict(
                ApprovalDecision(
                    request.request_id,
                    request.status,
                    False,
                    reason="approval request cannot be obsoleted from status '{}'".format(
                        request.status
                    ),
                )
            )
        now = _now()
        details = request.details if isinstance(request.details, dict) else {}
        if reason:
            request.details = {**details, "obsolete_reason": str(reason)}
        settled, latest = get_approval_store().settle_request(
            request.request_id,
            "obsolete",
            allowed_statuses=("pending", "approved", "expired"),
            decision_at=now,
        )
        if not settled:
            latest_request = _request_from_mapping(latest)
            status = latest_request.status if latest_request else request.status
            return asdict(
                ApprovalDecision(
                    request.request_id,
                    status,
                    False,
                    reason=f"approval request already settled as '{status}'",
                )
            )
        request.status = "obsolete"
        request.decision_at = now
        _REQUESTS[request.request_id] = request
        get_approval_store().save_request(request)
        _refresh_approval_state_mirrors_from_store()
        return asdict(ApprovalDecision(request.request_id, request.status, False, reason=reason))


def approve(
    request_id: str,
    *,
    debug_operator: dict[str, Any] | None = None,
) -> dict[str, Any]:
    with _LOCK:
        request = _REQUESTS.get(str(request_id)) or _request_from_mapping(
            get_approval_store().get_request(str(request_id))
        )
        now = _now()
        if request is None:
            return asdict(
                ApprovalDecision(
                    str(request_id), "missing", False, reason="approval request not found"
                )
            )
        if request.status == "consumed":
            return asdict(
                ApprovalDecision(
                    request.request_id,
                    request.status,
                    False,
                    reason="approval request already consumed",
                )
            )
        if request.status == "denied":
            return asdict(
                ApprovalDecision(
                    request.request_id, request.status, False, reason="approval request denied"
                )
            )
        if request.status == "approved" and isinstance(debug_operator, dict):
            details = request.details if isinstance(request.details, dict) else {}
            token = issue_execution_token(
                request.request_id,
                request.args_hash,
                expires_at=request.expires_at,
                operation=request.operation,
                function_id=str(details.get("function_id") or details.get("action") or ""),
                pack_id=str(details.get("pack_id") or ""),
                conversation_id=str(details.get("conversation_id") or ""),
                scope_digest=str(details.get("approval_scope_digest") or ""),
                debug_binding=debug_operator,
            )
            return asdict(
                ApprovalDecision(
                    request.request_id,
                    request.status,
                    True,
                    token=token,
                    expires_at=request.expires_at,
                    reason="idempotent debug approval retry",
                )
            )
        if request.expires_at < now:
            request.status = "expired"
            request.decision_at = now
            _REQUESTS[request.request_id] = request
            get_approval_store().save_request(request)
            _refresh_approval_state_mirrors_from_store()
            return asdict(
                ApprovalDecision(
                    request.request_id, request.status, False, reason="approval request expired"
                )
            )
        if request.status != "pending":
            return asdict(
                ApprovalDecision(
                    request.request_id,
                    request.status,
                    False,
                    reason=f"approval request already settled as '{request.status}'",
                )
            )
        settled, latest = get_approval_store().settle_request(
            request.request_id,
            "approved",
            allowed_statuses=("pending",),
            decision_at=now,
        )
        if not settled:
            latest_request = _request_from_mapping(latest)
            status = latest_request.status if latest_request else request.status
            return asdict(
                ApprovalDecision(
                    request.request_id,
                    status,
                    False,
                    reason=f"approval request already settled as '{status}'",
                )
            )
        request.status = "approved"
        request.decision_at = now
        _REQUESTS[request.request_id] = request
        _refresh_approval_state_mirrors_from_store()
        details = request.details if isinstance(request.details, dict) else {}
        token = issue_execution_token(
            request.request_id,
            request.args_hash,
            expires_at=request.expires_at,
            operation=request.operation,
            function_id=str(details.get("function_id") or details.get("action") or ""),
            pack_id=str(details.get("pack_id") or ""),
            conversation_id=str(details.get("conversation_id") or ""),
            scope_digest=str(details.get("approval_scope_digest") or ""),
            debug_binding=debug_operator,
        )
        return asdict(
            ApprovalDecision(
                request.request_id,
                request.status,
                True,
                token=token,
                expires_at=request.expires_at,
            )
        )

def register_debug_resume_handle(
    request_id: str,
    token: str,
    *,
    operator: dict[str, Any],
) -> str:
    handle = "resume_" + uuid.uuid4().hex
    with _LOCK:
        _DEBUG_RESUME_HANDLES[handle] = {
            "request_id": str(request_id),
            "token": str(token),
            "lease_epoch": int(operator.get("lease_epoch") or 0),
            "expires_at": int(operator.get("expires_at") or 0),
        }
    return handle


def resolve_debug_resume_handle(handle: str, request_id: str) -> str:
    with _LOCK:
        record = _DEBUG_RESUME_HANDLES.get(str(handle))
        if not isinstance(record, dict):
            return ""
        if (
            str(record.get("request_id") or "") != str(request_id)
            or int(record.get("expires_at") or 0) <= _now()
        ):
            return ""
        return str(record.get("token") or "")


def approve_with_extended_expiry(
    request_id: str,
    *,
    expires_in: int = _DEFAULT_EXPIRES_IN_SECONDS,
) -> dict[str, Any]:
    """Approve or refresh a request while extending its token window.

    Unlike ``approve()``, this helper may refresh an ``expired`` request. Callers
    must enforce their own policy before using it; the scheduler uses it only
    after matching the scheduled auto-approval allowlist and conversation
    scope.
    """
    with _LOCK:
        request = _REQUESTS.get(str(request_id)) or _request_from_mapping(
            get_approval_store().get_request(str(request_id))
        )
        now = _now()
        if request is None:
            return asdict(
                ApprovalDecision(
                    str(request_id), "missing", False, reason="approval request not found"
                )
            )
        if request.status == "consumed":
            return asdict(
                ApprovalDecision(
                    request.request_id,
                    request.status,
                    False,
                    reason="approval request already consumed",
                )
            )
        if request.status == "denied":
            return asdict(
                ApprovalDecision(
                    request.request_id, request.status, False, reason="approval request denied"
                )
            )
        if request.status not in {"pending", "approved", "expired"}:
            return asdict(
                ApprovalDecision(
                    request.request_id,
                    request.status,
                    False,
                    reason="approval request cannot be extended from status '{}'".format(
                        request.status
                    ),
                )
            )
        try:
            extension_seconds = int(expires_in or _DEFAULT_EXPIRES_IN_SECONDS)
        except Exception:
            extension_seconds = _DEFAULT_EXPIRES_IN_SECONDS
        request.status = "approved"
        request.decision_at = now
        request.expires_at = max(int(request.expires_at or 0), now + max(1, extension_seconds))
        _REQUESTS[request.request_id] = request
        get_approval_store().save_request(request)
        _refresh_approval_state_mirrors_from_store()
        details = request.details if isinstance(request.details, dict) else {}
        token = issue_execution_token(
            request.request_id,
            request.args_hash,
            expires_at=request.expires_at,
            operation=request.operation,
            function_id=str(details.get("function_id") or details.get("action") or ""),
            pack_id=str(details.get("pack_id") or ""),
            conversation_id=str(details.get("conversation_id") or ""),
            scope_digest=str(details.get("approval_scope_digest") or ""),
        )
        return asdict(
            ApprovalDecision(
                request.request_id,
                request.status,
                True,
                token=token,
                expires_at=request.expires_at,
            )
        )


def issue_execution_token(
    request_id: str,
    args_hash: str,
    *,
    expires_at: int | None = None,
    operation: str = "",
    function_id: str = "",
    pack_id: str = "",
    conversation_id: str = "",
    scope_digest: str = "",
    debug_binding: dict[str, Any] | None = None,
) -> str:
    payload = {
        "version": _TOKEN_VERSION,
        "jti": "tok_" + uuid.uuid4().hex,
        "request_id": str(request_id),
        "args_hash": str(args_hash),
        "expires_at": int(expires_at or (_now() + _DEFAULT_EXPIRES_IN_SECONDS)),
    }
    if operation:
        payload["operation"] = str(operation)
    if function_id:
        payload["function_id"] = str(function_id)
    if pack_id:
        payload["pack_id"] = str(pack_id)
    if conversation_id:
        payload["conversation_id"] = str(conversation_id)
    if scope_digest:
        payload["scope_digest"] = str(scope_digest)
    if isinstance(debug_binding, dict):
        payload["debug_lease_epoch"] = int(debug_binding.get("lease_epoch") or 0)
        payload["debug_session_id"] = str(debug_binding.get("session_id") or "")
        payload["debug_run_id"] = str(debug_binding.get("run_id") or "")
    body = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    encoded = _b64url_encode(body)
    signature = hmac.new(
        _RUNTIME_SECRET.encode("utf-8"),
        encoded.encode("ascii"),
        hashlib.sha256,
    ).digest()
    return encoded + "." + _b64url_encode(signature)


def verify_execution_token(
    token: str,
    operation: str,
    args_hash: str,
    *,
    consume: bool = True,
    pack_id: str = "",
    conversation_id: str = "",
    scope_digest: str = "",
) -> TokenVerification:
    token = str(token or "")
    if "." not in token:
        return TokenVerification(False, "APPROVAL_TOKEN_MISSING", "approval token is required")
    encoded, supplied_signature = token.rsplit(".", 1)
    expected_signature = _b64url_encode(
        hmac.new(_RUNTIME_SECRET.encode("utf-8"), encoded.encode("ascii"), hashlib.sha256).digest()
    )
    if not hmac.compare_digest(supplied_signature, expected_signature):
        return TokenVerification(
            False, "APPROVAL_SIGNATURE_INVALID", "approval token signature is invalid"
        )
    try:
        payload = json.loads(_b64url_decode(encoded).decode("utf-8"))
    except Exception:
        return TokenVerification(
            False, "APPROVAL_TOKEN_INVALID", "approval token payload is invalid"
        )
    if payload.get("version") != _TOKEN_VERSION:
        return TokenVerification(
            False, "APPROVAL_TOKEN_INVALID", "approval token version is invalid"
        )
    if int(payload.get("expires_at") or 0) < _now():
        return TokenVerification(False, "APPROVAL_EXPIRED", "approval token expired")
    token_operation = str(payload.get("operation") or "")
    if token_operation and token_operation != str(operation):
        return TokenVerification(
            False,
            "APPROVAL_OPERATION_MISMATCH",
            "approval token operation mismatch",
        )
    expected_pack_id = str(pack_id or "")
    token_pack_id = str(payload.get("pack_id") or "")
    # Scope checks stay strict for real execution paths that pass an expected
    # pack/conversation, but status probes may omit them when they only need to
    # inspect whether a one-shot token is still valid or already consumed.
    if expected_pack_id and token_pack_id != expected_pack_id:
        return TokenVerification(False, "APPROVAL_PACK_MISMATCH", "approval token pack mismatch")
    expected_conversation_id = str(conversation_id or "")
    token_conversation_id = str(payload.get("conversation_id") or "")
    if expected_conversation_id and token_conversation_id != expected_conversation_id:
        return TokenVerification(
            False,
            "APPROVAL_CONVERSATION_MISMATCH",
            "approval token conversation mismatch",
        )
    if str(payload.get("args_hash") or "") != str(args_hash):
        return TokenVerification(
            False,
            "APPROVAL_ARGUMENTS_CHANGED",
            "approval token does not match request arguments",
        )
    expected_scope_digest = str(scope_digest or "")
    token_scope_digest = str(payload.get("scope_digest") or "")
    if expected_scope_digest and token_scope_digest != expected_scope_digest:
        return TokenVerification(
            False,
            "APPROVAL_SCOPE_MISMATCH",
            "approval token scope mismatch",
        )
    request_id = str(payload.get("request_id") or "")
    jti = str(payload.get("jti") or "")
    debug_lease_epoch = int(payload.get("debug_lease_epoch") or 0)
    if debug_lease_epoch and consume:
        try:
            from domain.host_bridge.viewer_broker_client import ViewerBrokerClient

            broker = ViewerBrokerClient.from_environment()
            result = broker.consume_debug_execution(
                request_id=request_id,
                lease_epoch=debug_lease_epoch,
                execution_jti=jti,
            )
        except Exception:
            return TokenVerification(
                False,
                "DEBUG_APPROVAL_REVOKED",
                "Launcher debug approval was revoked or is unavailable",
                request_id,
            )
        if result.get("ok") is not True or result.get("consumed") is not True:
            return TokenVerification(
                False,
                "DEBUG_APPROVAL_REVOKED",
                "Launcher debug approval was revoked or is unavailable",
                request_id,
            )
    with _LOCK:
        if jti in _USED_TOKEN_IDS or get_approval_store().is_token_used(jti):
            return TokenVerification(
                False,
                "APPROVAL_TOKEN_USED",
                "approval token has already been used",
                request_id,
            )
        request = _REQUESTS.get(request_id) or _request_from_mapping(
            get_approval_store().get_request(request_id)
        )
        if request is None:
            return TokenVerification(
                False, "APPROVAL_REQUEST_MISSING", "approval request is missing"
            )
        if request.operation != operation:
            return TokenVerification(
                False,
                "APPROVAL_OPERATION_MISMATCH",
                "approval token operation mismatch",
                request_id,
            )
        if request.status == "consumed":
            return TokenVerification(
                False,
                "APPROVAL_TOKEN_USED",
                "approval token has already been used",
                request_id,
            )
        if request.status != "approved":
            return TokenVerification(
                False,
                "APPROVAL_NOT_APPROVED",
                "approval request is not approved",
                request_id,
            )
        details = request.details if isinstance(request.details, dict) else {}
        request_scope_digest = str(details.get("approval_scope_digest") or "")
        if request_scope_digest and token_scope_digest != request_scope_digest:
            return TokenVerification(
                False,
                "APPROVAL_SCOPE_MISMATCH",
                "approval token scope mismatch",
                request_id,
            )
        if consume:
            inserted = get_approval_store().mark_token_used(jti, request_id, operation, args_hash)
            if not inserted:
                return TokenVerification(
                    False,
                    "APPROVAL_TOKEN_USED",
                    "approval token has already been used",
                    request_id,
                )
            _USED_TOKEN_IDS.add(jti)
            request.status = "consumed"
            request.decision_at = _now()
            _REQUESTS[request_id] = request
            _refresh_approval_state_mirrors_from_store()
    return TokenVerification(True, request_id=request_id)


def get_approval_request(request_id: str) -> dict[str, Any] | None:
    """Return a stored approval request as a plain dict, or ``None`` if missing.

    Used by the approval-followup replay path to recover the approved arguments
    deterministically from the stored ``details["arguments"]`` payload.
    """
    if not request_id:
        return None
    with _LOCK:
        request = _REQUESTS.get(str(request_id)) or _request_from_mapping(
            get_approval_store().get_request(str(request_id))
        )
        if request is None:
            return None
        now = _now()
        if request.status == "pending" and request.expires_at < now:
            settled, latest = get_approval_store().settle_request(
                request.request_id,
                "expired",
                allowed_statuses=("pending",),
                decision_at=now,
            )
            request = (
                request
                if settled
                else (_request_from_mapping(latest) or request)
            )
            if settled:
                request.status = "expired"
                request.decision_at = now
            _REQUESTS[request.request_id] = request
            _refresh_approval_state_mirrors_from_store()
        return asdict(request)


def list_approval_requests(
    status: str | None = None,
    *,
    include_expired: bool = True,
    limit: int = 100,
    debug_binding: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    limit = max(1, min(500, int(limit or 100)))
    requests = get_approval_store().list_requests(include_expired=True, limit=500)
    sqlite_by_id: dict[str, dict[str, Any]] = {}
    now = _now()
    expired_any = False
    for item in requests:
        request = _request_from_mapping(item)
        if request is None:
            continue
        if request.status == "pending" and request.expires_at < now:
            request.status = "expired"
            request.decision_at = now
            get_approval_store().save_request(request)
            expired_any = True
        sqlite_by_id[request.request_id] = _request_payload(request)
    if expired_any:
        _refresh_approval_state_mirrors_from_store()

    merged: dict[str, dict[str, Any]] = {}
    for item in load_approval_state_requests():
        request = normalize_json_approval_request(item)
        if request is None:
            continue
        if request.get("status") == "pending" and int(request.get("expires_at") or 0) < now:
            request["status"] = "expired"
            request["decision_at"] = now
        request["display_summary"] = display_summary(
            str(request.get("operation") or ""),
            request.get("details") if isinstance(request.get("details"), dict) else {},
        )
        merged[request["request_id"]] = request
    merged.update(sqlite_by_id)

    result = [
        request
        for request in merged.values()
        if _request_is_visible(request, status, include_expired=include_expired, now=now)
    ]
    if isinstance(debug_binding, dict):
        expected = {
            key: str(debug_binding.get(key) or "")
            for key in (
                "debug_session_id",
                "lease_epoch",
                "debug_run_id",
                "workspace_identity_digest",
                "pack_id",
                "profile_id",
            )
        }
        result = [
            request
            for request in result
            if all(str(request.get(key) or "") == value for key, value in expected.items())
        ]
    result.sort(
        key=lambda item: (int(item.get("created_at") or 0), str(item.get("request_id") or "")),
        reverse=True,
    )
    result = result[:limit]
    return result


def reset_approval_state_for_tests() -> None:
    with _LOCK:
        _REQUESTS.clear()
        _USED_TOKEN_IDS.clear()
        get_approval_store().clear()
        clear_approval_state_mirrors()
