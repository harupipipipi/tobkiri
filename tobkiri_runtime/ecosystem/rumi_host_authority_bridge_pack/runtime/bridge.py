"""Core-authority one-shot bridge for high-authority host services."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import secrets
import threading
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

from core_runtime.authority.request_store import AuthorityRequestStore
from core_runtime.host_provider_backend_v4 import (
    CapturedHostProviderV4,
    HostProviderCaptureContextV4,
    HostProviderContributionV4,
    HostProviderInvocationContextV4,
)
from core_runtime.interactive_effect_coordinator import (
    INTERACTIVE_EFFECT_COORDINATOR_CONTRACT_ID,
    INTERACTIVE_EFFECT_COORDINATOR_OPERATION_ID,
    INTERACTIVE_EFFECT_SPECS,
)
from core_runtime.paths import USER_DATA_DIR
from core_runtime.runtime_locks import NamedLock
from tobkiri_host.broker import RequestEnvelope
from tobkiri_host.models import OpaqueAuthorityRef, RequestContext
from tobkiri_host.ports import (
    InteractiveApprovalDecisionCommand,
    InteractiveApprovalGetQuery,
    InteractiveApprovalListQuery,
    InteractiveApprovalPort,
    InteractiveApprovalStatus,
    InteractiveEffectOwnerQuery,
    InteractiveEffectPort,
    InteractiveEffectPrepareCommand,
    InteractiveEffectStatus,
    OpaqueInvocationLease,
)
from tobkiri_protocol.canonical import canonical_digest

_TTL_SECONDS = 30
_LOCK = threading.RLock()
_RECEIPT_ROOT = Path(USER_DATA_DIR) / "authority" / "effect_receipts"
_DIGEST = re.compile(r"^sha256:[a-f0-9]{64}$")


@dataclass(frozen=True)
class HostAuthorityScope:
    """Host-authenticated identity used to bind an authority receipt.

    The effect request remains ordinary Pack data.  Every identity and
    activation binding in this view is extracted from the Host-generated
    envelope/context, never from the request payload.
    """

    envelope: RequestEnvelope
    caller_id: str
    caller_pack_id: str
    caller_function_id: str
    profile_id: str
    profile_revision: str
    activation_id: str
    activation_digest: str
    plan_digest: str
    profile_authority_digest: str
    workspace_id: str
    session_id: str
    security_epoch: int
    fencing_token: int
    request_digest: str
    target_principal_id: str
    target_domain_id: str


def require_authenticated_host_context(value: object) -> HostAuthorityScope:
    """Return an authenticated Host scope or fail closed.

    ``RequestEnvelope`` is intentionally the only accepted identity carrier.
    A Pack payload, a default profile, and a client-supplied object that merely
    resembles an envelope cannot satisfy this check.
    """

    envelope = value if isinstance(value, RequestEnvelope) else getattr(value, "envelope", None)
    if not isinstance(envelope, RequestEnvelope):
        raise PermissionError("Host-authenticated request envelope is required")
    context = envelope.context
    if not isinstance(context, RequestContext):
        raise PermissionError("Host-authenticated request context is invalid")
    if not isinstance(context.caller_principal, OpaqueAuthorityRef):
        raise PermissionError("Host caller principal is invalid")
    if not isinstance(envelope.target_principal, OpaqueAuthorityRef):
        raise PermissionError("Host target principal is invalid")
    if not isinstance(envelope.target_domain, OpaqueAuthorityRef):
        raise PermissionError("Host target domain is invalid")
    if not isinstance(envelope.lease, OpaqueInvocationLease):
        raise PermissionError("Host invocation lease is invalid")
    if not isinstance(envelope.payload, Mapping):
        raise PermissionError("Host envelope payload is invalid")
    if not _DIGEST.fullmatch(str(envelope.request_digest or "")):
        raise PermissionError("Host request digest is invalid")
    for field_name in (
        "activation_digest",
        "plan_digest",
        "profile_authority_digest",
        "target_backend_digest",
    ):
        if not _DIGEST.fullmatch(str(getattr(context, field_name) or "")):
            raise PermissionError(f"Host {field_name} is invalid")
    if (
        not context.request_id
        or not context.trace_id
        or not context.profile_id
        or not context.activation_id
        or not context.caller_session_id
        or not context.caller_domain_id
        or not context.handle_namespace
        or not envelope.contract_id
        or not envelope.contract_version
        or not envelope.operation_id
        or not isinstance(envelope.deadline_monotonic, (int, float))
        or isinstance(envelope.deadline_monotonic, bool)
        or not math.isfinite(envelope.deadline_monotonic)
        or envelope.deadline_monotonic <= time.monotonic()
    ):
        raise PermissionError("Host request context is incomplete or expired")

    profile_revision = _host_string(
        value,
        context,
        "profile_revision",
        context.profile_authority_digest,
    )
    if not profile_revision:
        raise PermissionError("Host profile revision is unavailable")
    caller_id = context.caller_principal.value
    caller_pack_id = _host_string(value, context, "caller_pack_id", caller_id)
    caller_function_id = _host_string(value, context, "caller_function_id", caller_id)
    if not caller_pack_id or not caller_function_id:
        raise PermissionError("Host caller binding is unavailable")
    return HostAuthorityScope(
        envelope=envelope,
        caller_id=caller_id,
        caller_pack_id=caller_pack_id,
        caller_function_id=caller_function_id,
        profile_id=context.profile_id,
        profile_revision=profile_revision,
        activation_id=context.activation_id,
        activation_digest=context.activation_digest,
        plan_digest=context.plan_digest,
        profile_authority_digest=context.profile_authority_digest,
        workspace_id=_host_string(value, context, "workspace_id"),
        session_id=context.caller_session_id,
        security_epoch=context.security_epoch,
        fencing_token=context.fencing_token,
        request_digest=envelope.request_digest,
        target_principal_id=envelope.target_principal.value,
        target_domain_id=envelope.target_domain.value,
    )


def _host_string(
    host_context: object,
    context: RequestContext,
    name: str,
    fallback: str = "",
) -> str:
    """Read optional binding metadata from the Host context only."""

    for source in (host_context, context):
        candidate = getattr(source, name, None)
        if isinstance(candidate, str) and candidate.strip():
            return candidate.strip()
    return str(fallback or "").strip()


def create_authority_operation(
    host_context: Any,
) -> Callable[[str, Mapping[str, Any]], dict[str, Any]]:
    """Create authorize/redeem operations bound to one Host context."""

    def operation(name: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        if name == "authorize":
            return _authorize(payload, host_context=host_context)
        if name == "redeem":
            return _redeem(payload, host_context=host_context)
        raise ValueError(f"unknown host authority operation: {name}")

    return operation


def _authorize(
    payload: Mapping[str, Any],
    host_context: object | None = None,
) -> dict[str, Any]:
    scope = _scope(payload, host_context)
    receipt = secrets.token_urlsafe(32)
    receipt_hash = _receipt_hash(receipt)
    now = time.time()
    record = {
        "scope": scope,
        "service_pack_id": str(payload.get("service_pack_id") or "").strip(),
        "issued_at": now,
        "expires_at": now + _TTL_SECONDS,
        "status": "pending_approval",
    }
    if not record["service_pack_id"]:
        raise ValueError("host authority service pack is required")
    with _LOCK, NamedLock(_RECEIPT_ROOT, "authority-receipts"):
        _write_receipt(receipt_hash, record)
    if bool(payload.get("approval_required", False)):
        token = str(payload.get("approval_token") or "")
        request_id = str(payload.get("approval_request_id") or "").strip()
        if not token or not request_id:
            _delete_receipt(receipt_hash)
            return {
                **_denied(scope, "approval_required"),
                "request": {
                    "principal_id": scope["caller_id"],
                    "permission_id": scope["authority"],
                    "profile_id": scope["profile_id"],
                    "resource": scope,
                    "risk_level": str(payload.get("risk") or "high"),
                },
            }
        consumed = AuthorityRequestStore().consume_one_shot(
            request_id=request_id,
            principal_id=scope["caller_id"],
            permission_id=scope["authority"],
            resource=scope,
            token=token,
        )
        if not consumed:
            _delete_receipt(receipt_hash)
            return _denied(scope, "approval_invalid_expired_or_used")
    with _LOCK, NamedLock(_RECEIPT_ROOT, "authority-receipts"):
        record["status"] = "issued"
        _write_receipt(receipt_hash, record)
    return {
        "authorized": True,
        "receipt": receipt,
        "receipt_hash": receipt_hash,
        "scope": scope,
        "service_pack_id": record["service_pack_id"],
        "expires_in_seconds": _TTL_SECONDS,
        "replay_policy": "redeem_once",
    }


def _redeem(
    payload: Mapping[str, Any],
    host_context: object | None = None,
) -> dict[str, Any]:
    receipt = str(payload.get("receipt") or "")
    if not receipt:
        raise ValueError("host authority receipt is required")
    receipt_hash = _receipt_hash(receipt)
    expected_scope = _scope(payload, host_context)
    service_pack_id = str(payload.get("service_pack_id") or "").strip()
    if not service_pack_id:
        raise ValueError("host authority service pack is required")
    now = time.time()
    with _LOCK, NamedLock(_RECEIPT_ROOT, "authority-receipts"):
        _prune(now)
        record = _read_receipt(receipt_hash)
        if record is None:
            return _denied(expected_scope, "receipt_missing_or_expired")
        if record.get("status") != "issued":
            return _denied(expected_scope, "receipt_already_redeemed")
        if record["service_pack_id"] != service_pack_id:
            return _denied(expected_scope, "receipt_service_mismatch")
        if record["scope"] != expected_scope:
            return _denied(expected_scope, "receipt_scope_mismatch")
        record["status"] = "effect_committing"
        record["redeemed_at"] = now
        _write_receipt(receipt_hash, record)
    return {
        "authorized": True,
        "redeemed": True,
        "receipt_hash": receipt_hash,
        "scope": expected_scope,
        "service_pack_id": service_pack_id,
    }


def _scope(
    payload: Mapping[str, Any],
    host_context: object | None = None,
) -> dict[str, Any]:
    host_scope = require_authenticated_host_context(host_context)
    operation = str(payload.get("operation") or "").strip()
    authority = str(payload.get("authority") or "").strip()
    arguments = payload.get("arguments")
    if not isinstance(arguments, Mapping):
        raise ValueError("host authority arguments must be an object")
    if not all((operation, authority)):
        raise ValueError("host authority scope is incomplete")
    return {
        "operation": operation,
        "authority": authority,
        "args_hash": hashlib.sha256(
            json.dumps(
                dict(arguments),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                default=str,
            ).encode("utf-8")
        ).hexdigest(),
        "caller_id": host_scope.caller_id,
        "caller_pack_id": host_scope.caller_pack_id,
        "caller_function_id": host_scope.caller_function_id,
        "profile_id": host_scope.profile_id,
        "profile_revision": host_scope.profile_revision,
        "activation_id": host_scope.activation_id,
        "activation_digest": host_scope.activation_digest,
        "plan_digest": host_scope.plan_digest,
        "profile_authority_digest": host_scope.profile_authority_digest,
        "workspace_id": host_scope.workspace_id,
        "session_id": host_scope.session_id,
        "security_epoch": host_scope.security_epoch,
        "fencing_token": host_scope.fencing_token,
        "request_digest": host_scope.request_digest,
        "target_principal_id": host_scope.target_principal_id,
        "target_domain_id": host_scope.target_domain_id,
        "replay_policy": "one_shot",
    }


def _denied(scope: Mapping[str, Any], reason: str) -> dict[str, Any]:
    return {"authorized": False, "reason": reason, "scope": dict(scope)}


def _receipt_hash(receipt: str) -> str:
    return hashlib.sha256(receipt.encode("utf-8")).hexdigest()


def _prune(now: float) -> None:
    if not _RECEIPT_ROOT.exists():
        return
    for path in _RECEIPT_ROOT.glob("*.json"):
        value = _read_json(path)
        if float((value or {}).get("expires_at") or 0) <= now:
            try:
                path.unlink()
            except FileNotFoundError:
                pass


def _receipt_path(receipt_hash: str) -> Path:
    return _RECEIPT_ROOT / f"{receipt_hash}.json"


def _read_receipt(receipt_hash: str) -> dict[str, Any] | None:
    return _read_json(_receipt_path(receipt_hash))


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return dict(value) if isinstance(value, Mapping) else None


def _write_receipt(receipt_hash: str, value: Mapping[str, Any]) -> None:
    _RECEIPT_ROOT.mkdir(parents=True, exist_ok=True)
    os.chmod(_RECEIPT_ROOT, 0o700)
    descriptor, temporary = tempfile.mkstemp(
        dir=_RECEIPT_ROOT,
        prefix=".receipt-",
        suffix=".tmp",
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(dict(value), handle, sort_keys=True, separators=(",", ":"))
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, _receipt_path(receipt_hash))
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _delete_receipt(receipt_hash: str) -> None:
    try:
        _receipt_path(receipt_hash).unlink()
    except FileNotFoundError:
        pass


# V4 Host Provider path ----------------------------------------------------
#
# The receipt-based functions above remain V3 compatibility entry points for
# the currently generated Pack artifacts.  None of the V4 factory/service
# below imports, calls, or exposes AuthorityRequestStore, receipt files, or
# redemption.  Generated contract/catalog changes that select this provider
# are intentionally coordinated separately.

_V4_FUNCTION_ID = "rumi_host_authority_bridge_pack.host-authority.interactive-approval"
_V4_CONTRACT_ID = "tobkiri.service.interactive-approval.v1"
_EFFECT_FUNCTION_ID = "rumi_host_authority_bridge_pack.host-authority.interactive-effect"
_EFFECT_CONTRACT_ID = INTERACTIVE_EFFECT_COORDINATOR_CONTRACT_ID
_EFFECT_OPERATION = INTERACTIVE_EFFECT_COORDINATOR_OPERATION_ID
_EFFECT_PACK_ID = "rumi_host_authority_bridge_pack"
_V4_GET_OPERATION = "interactive_approval.get"
_V4_LIST_OPERATION = "interactive_approval.list"
_V4_APPROVE_OPERATION = "interactive_approval.approve"
_V4_DENY_OPERATION = "interactive_approval.deny"
_V4_OPERATIONS = frozenset(
    {
        _V4_GET_OPERATION,
        _V4_LIST_OPERATION,
        _V4_APPROVE_OPERATION,
        _V4_DENY_OPERATION,
    }
)
_V4_UNTRUSTED_AUTHORITY_FIELDS = frozenset(
    {
        "approved",
        "approval",
        "approval_id",
        "approval_token",
        "authority_receipt",
        "authority_token",
        "client_token",
        "grant",
        "grant_id",
        "receipt",
        "token",
    }
)


class InteractiveApprovalBridgeV4:
    """Presentation-only Host Provider over durable approval records.

    Request creation and effect resumption belong to the Host pending-effect
    controller.  This bridge may only ask that controller's approval port for
    a redacted status or forward a locally authenticated decision.  In
    particular it deliberately retains no owner/request map: capture lifetime
    is not an authority boundary and cannot survive a Host restart.
    """

    def __init__(
        self,
        *,
        capture: HostProviderCaptureContextV4,
        bindings: Mapping[str, object],
        approval_port: InteractiveApprovalPort,
    ) -> None:
        self._profile_id = capture.profile_id
        self._plan_digest = capture.plan_digest
        self._security_epoch = capture.security_epoch
        self._activation_id = str(capture.activation.get("activation_id") or "")
        self._activation_digest = canonical_digest(dict(capture.activation))
        self._bindings = dict(bindings)
        self._approval_port = approval_port
        self._closed = False
        self._lock = threading.RLock()
        if not self._activation_id:
            raise PermissionError("interactive approval activation is unavailable")

    def close(self) -> None:
        """Fence this captured provider from further approval-port use."""

        with self._lock:
            self._closed = True

    def invoke(
        self,
        operation_id: str,
        payload: Mapping[str, Any],
        invocation: HostProviderInvocationContextV4,
    ) -> Mapping[str, Any]:
        """Run only a fixed operation against the Host-owned narrow port."""

        envelope = self._authenticated_envelope(operation_id, invocation)
        if not isinstance(payload, Mapping):
            raise PermissionError("interactive approval payload is invalid")
        _reject_v4_client_authority(payload)
        if operation_id == _V4_GET_OPERATION:
            return self._get(envelope, payload)
        if operation_id == _V4_LIST_OPERATION:
            return self._list(envelope, payload)
        if operation_id == _V4_APPROVE_OPERATION:
            return self._decide(envelope, payload, approved=True)
        if operation_id == _V4_DENY_OPERATION:
            return self._decide(envelope, payload, approved=False)
        raise PermissionError("interactive approval operation is not allowed")

    def _get(
        self,
        envelope: RequestEnvelope,
        payload: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        _require_exact_payload_keys(payload, {"request_id"})
        request_id = _payload_id(payload, "request_id")
        return _redacted_status(
            self._approval_port.get_interactive_approval(
                InteractiveApprovalGetQuery(
                    context=envelope.context,
                    request_id=request_id,
                )
            )
        )

    def _list(
        self,
        envelope: RequestEnvelope,
        payload: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        _require_exact_payload_keys(payload, set())
        return {
            "approvals": [
                _redacted_status(status)
                for status in self._approval_port.list_interactive_approvals(
                    InteractiveApprovalListQuery(context=envelope.context)
                )
            ]
        }

    def _decide(
        self,
        envelope: RequestEnvelope,
        payload: Mapping[str, Any],
        *,
        approved: bool,
    ) -> Mapping[str, Any]:
        _require_exact_payload_keys(
            payload,
            {"request_id", "confirmation_text", "ui_operator"}
            if approved
            else {"request_id", "ui_operator"},
        )
        request_id = _payload_id(payload, "request_id")
        ui_operator = payload.get("ui_operator")
        if not isinstance(ui_operator, Mapping):
            raise PermissionError("interactive approval ui_operator is required")
        actor_id = _ui_actor_id(ui_operator)
        command = InteractiveApprovalDecisionCommand(
            context=envelope.context,
            request_id=request_id,
            actor_id=actor_id,
            confirmation_text=str(payload.get("confirmation_text") or ""),
            ui_operator=dict(ui_operator),
        )
        status = (
            self._approval_port.approve_interactive_approval(command)
            if approved
            else self._approval_port.deny_interactive_approval(command)
        )
        return _redacted_status(status)

    def _authenticated_envelope(
        self,
        operation_id: str,
        invocation: HostProviderInvocationContextV4,
    ) -> RequestEnvelope:
        with self._lock:
            if self._closed:
                raise PermissionError("interactive approval provider is closed")
        if operation_id not in self._bindings:
            raise PermissionError("interactive approval operation is not captured")
        envelope = invocation.envelope
        if not isinstance(envelope, RequestEnvelope):
            raise PermissionError("interactive approval Host envelope is invalid")
        context = envelope.context
        if (
            envelope.operation_id != operation_id
            or envelope.contract_id != _V4_CONTRACT_ID
            or context.profile_id != self._profile_id
            or context.activation_id != self._activation_id
            or context.activation_digest != self._activation_digest
            or context.plan_digest != self._plan_digest
            or context.security_epoch != self._security_epoch
        ):
            raise PermissionError("interactive approval capture binding changed")
        binding = self._bindings[operation_id]
        principal_id = str(getattr(getattr(binding, "principal_ref", None), "value", ""))
        if envelope.target_principal.value != principal_id:
            raise PermissionError("interactive approval target binding changed")
        return envelope


class InteractiveApprovalBridgeFactoryV4:
    """Capture only the exact interactive-approval operations from one plan."""

    function_id = _V4_FUNCTION_ID

    def capture(
        self,
        context: HostProviderCaptureContextV4,
    ) -> CapturedHostProviderV4:
        """Bind the Host authority bridge to one captured narrow port."""

        approval_port = context.interactive_approval_port
        if approval_port is None:
            raise PermissionError("interactive approval Host port is unavailable")
        bindings = tuple(context.provider_bindings)
        if (
            not bindings
            or any(
                binding.function.function_id != self.function_id
                or binding.operation.contract_id != _V4_CONTRACT_ID
                or binding.operation.operation_id not in _V4_OPERATIONS
                for binding in bindings
            )
            or {binding.operation.operation_id for binding in bindings} != _V4_OPERATIONS
        ):
            raise PermissionError("interactive approval provider bindings are incomplete")
        bridge = InteractiveApprovalBridgeV4(
            capture=context,
            bindings={binding.operation.operation_id: binding for binding in bindings},
            approval_port=approval_port,
        )
        contributions: list[HostProviderContributionV4] = []
        for binding in bindings:
            key = (
                binding.operation.contract_id,
                binding.operation.operation_id,
                binding.principal_ref.value,
            )
            domain_id = context.domain_ids.get(key)
            if domain_id is None:
                bridge.close()
                raise PermissionError("interactive approval domain binding is unavailable")
            contributions.append(
                HostProviderContributionV4(
                    contract_id=binding.operation.contract_id,
                    contract_version=binding.operation.contract_version,
                    operation_id=binding.operation.operation_id,
                    principal_id=binding.principal_ref.value,
                    artifact_digest=binding.artifact.digest,
                    implementation_digest=binding.function.implementation_digest,
                    domain_id=domain_id,
                    invoke=bridge.invoke,
                )
            )
        return CapturedHostProviderV4(tuple(contributions), bridge.close)


class InteractiveEffectCoordinatorBridgeV4:
    """One Host Provider operation over the durable future-effect port.

    It has no executor and never invokes a local process or Git operation.
    ``prepare`` first performs the signed normal-authority prepare operation
    through the authenticated contract client.  Only its fixed Host-side
    transform is handed to the pending-effect port for the separately signed
    ``interactive_only`` execute edge.
    """

    def __init__(
        self,
        *,
        capture: HostProviderCaptureContextV4,
        binding: object,
        effect_port: InteractiveEffectPort,
    ) -> None:
        self._profile_id = capture.profile_id
        self._plan_digest = capture.plan_digest
        self._security_epoch = capture.security_epoch
        self._activation_id = str(capture.activation.get("activation_id") or "")
        self._activation_digest = canonical_digest(dict(capture.activation))
        self._binding = binding
        self._effect_port = effect_port
        self._closed = False
        self._lock = threading.RLock()
        # One coordinator call can synchronously enter the existing Broker for
        # a signed prepare or resume.  Keep it nonblocking so N outer Broker
        # workers cannot self-deadlock by all awaiting nested work.
        self._single_flight = threading.BoundedSemaphore(1)
        if not self._activation_id:
            raise PermissionError("interactive effect activation is unavailable")

    def close(self) -> None:
        """Fence use of the capture-scoped Host port after activation close."""

        with self._lock:
            self._closed = True

    def invoke(
        self,
        operation_id: str,
        payload: Mapping[str, Any],
        invocation: HostProviderInvocationContextV4,
    ) -> Mapping[str, Any]:
        """Handle only prepare/resume/status/cancel with strict payload shapes."""

        envelope = self._authenticated_envelope(operation_id, invocation)
        if not isinstance(payload, Mapping):
            raise PermissionError("interactive effect payload is invalid")
        _reject_v4_client_authority(payload)
        phase = payload.get("phase")
        if phase == "prepare":
            _require_exact_payload_keys(payload, {"phase", "effect_kind", "request"})
            return self._prepare(envelope, payload, invocation)
        if phase in {"resume", "status", "cancel"}:
            _require_exact_payload_keys(payload, {"phase", "effect_id"})
            return self._manage(envelope, str(phase), payload, invocation)
        raise PermissionError("interactive effect phase is invalid")

    def _prepare(
        self,
        envelope: RequestEnvelope,
        payload: Mapping[str, Any],
        invocation: HostProviderInvocationContextV4,
    ) -> Mapping[str, Any]:
        effect_kind = payload.get("effect_kind")
        request = payload.get("request")
        spec = INTERACTIVE_EFFECT_SPECS.get(effect_kind)
        if spec is None or not isinstance(request, Mapping):
            raise PermissionError("interactive effect prepare request is invalid")
        _reject_effect_request_authority(request)
        if not self._single_flight.acquire(blocking=False):
            raise PermissionError("interactive effect coordinator is busy")
        try:
            client = invocation.contract_client(
                allowed_contract_ids=frozenset({spec.prepare_contract_id}),
                consumer_pack_id=_EFFECT_PACK_ID,
            )
            prepared_result = client.invoke(
                spec.prepare_contract_id,
                spec.prepare_operation_id,
                dict(request),
            )
            if not isinstance(prepared_result, Mapping):
                raise PermissionError("interactive effect prepare is unavailable")
            return _redacted_effect_status(
                self._effect_port.prepare_interactive_effect(
                    InteractiveEffectPrepareCommand(
                        context=envelope.context,
                        coordinator_principal=envelope.target_principal,
                        presentation_owner_principal_id=(
                            invocation.presentation_owner_principal_id
                        ),
                        presentation_owner_session_id=(
                            invocation.presentation_owner_session_id
                        ),
                        effect_kind=effect_kind,
                        payload=dict(request),
                        prepared_result=dict(prepared_result),
                    )
                )
            )
        finally:
            self._single_flight.release()

    def _manage(
        self,
        envelope: RequestEnvelope,
        phase: str,
        payload: Mapping[str, Any],
        invocation: HostProviderInvocationContextV4,
    ) -> Mapping[str, Any]:
        query = InteractiveEffectOwnerQuery(
            context=envelope.context,
            coordinator_principal=envelope.target_principal,
            presentation_owner_principal_id=(
                invocation.presentation_owner_principal_id
            ),
            presentation_owner_session_id=invocation.presentation_owner_session_id,
            effect_id=_payload_id(payload, "effect_id"),
        )
        if phase == "status":
            return _redacted_effect_status(
                self._effect_port.get_interactive_effect(query)
            )
        if phase == "cancel":
            return _redacted_effect_status(
                self._effect_port.cancel_interactive_effect(query)
            )
        if not self._single_flight.acquire(blocking=False):
            raise PermissionError("interactive effect coordinator is busy")
        try:
            return _redacted_effect_status(
                self._effect_port.resume_interactive_effect(query)
            )
        finally:
            self._single_flight.release()

    def _authenticated_envelope(
        self,
        operation_id: str,
        invocation: HostProviderInvocationContextV4,
    ) -> RequestEnvelope:
        with self._lock:
            if self._closed:
                raise PermissionError("interactive effect provider is closed")
        envelope = invocation.envelope
        binding = self._binding
        principal_id = str(getattr(getattr(binding, "principal_ref", None), "value", ""))
        if (
            operation_id != _EFFECT_OPERATION
            or not isinstance(envelope, RequestEnvelope)
            or envelope.contract_id != _EFFECT_CONTRACT_ID
            or envelope.operation_id != _EFFECT_OPERATION
            or envelope.target_principal.value != principal_id
            or envelope.context.profile_id != self._profile_id
            or envelope.context.activation_id != self._activation_id
            or envelope.context.activation_digest != self._activation_digest
            or envelope.context.plan_digest != self._plan_digest
            or envelope.context.security_epoch != self._security_epoch
        ):
            raise PermissionError("interactive effect capture binding changed")
        return envelope


class InteractiveEffectCoordinatorFactoryV4:
    """Capture the sole interactive-effect coordinator Function identity."""

    function_id = _EFFECT_FUNCTION_ID
    requires_interactive_effect_port = True

    def capture(
        self,
        context: HostProviderCaptureContextV4,
    ) -> CapturedHostProviderV4:
        """Bind one exact coordinator operation to its late-bound Host port."""

        effect_port = context.interactive_effect_port
        bindings = tuple(context.provider_bindings)
        if (
            effect_port is None
            or len(bindings) != 1
            or bindings[0].function.function_id != self.function_id
            or bindings[0].operation.contract_id != _EFFECT_CONTRACT_ID
            or bindings[0].operation.operation_id != _EFFECT_OPERATION
        ):
            raise PermissionError("interactive effect coordinator binding is invalid")
        binding = bindings[0]
        key = (
            binding.operation.contract_id,
            binding.operation.operation_id,
            binding.principal_ref.value,
        )
        domain_id = context.domain_ids.get(key)
        if domain_id is None:
            raise PermissionError("interactive effect coordinator domain is unavailable")
        coordinator = InteractiveEffectCoordinatorBridgeV4(
            capture=context,
            binding=binding,
            effect_port=effect_port,
        )
        contribution = HostProviderContributionV4(
            contract_id=binding.operation.contract_id,
            contract_version=binding.operation.contract_version,
            operation_id=binding.operation.operation_id,
            principal_id=binding.principal_ref.value,
            artifact_digest=binding.artifact.digest,
            implementation_digest=binding.function.implementation_digest,
            domain_id=domain_id,
            invoke=coordinator.invoke,
        )
        return CapturedHostProviderV4((contribution,), coordinator.close)


def _reject_v4_client_authority(payload: Mapping[str, Any]) -> None:
    """Reject deprecated client-side authority, token, and receipt claims."""

    forbidden = _V4_UNTRUSTED_AUTHORITY_FIELDS & set(payload)
    if forbidden:
        raise PermissionError("client authority material is not accepted")


def _require_exact_payload_keys(
    payload: Mapping[str, Any],
    expected: set[str],
) -> None:
    """Reject unknown fields so a later schema cannot silently widen authority."""

    if set(payload) != expected:
        raise PermissionError("interactive approval payload fields are invalid")


def _payload_id(payload: Mapping[str, Any], field_name: str) -> str:
    """Read one bounded request identifier from a strict payload."""

    value = payload.get(field_name)
    if not isinstance(value, str) or not value or len(value) > 255:
        raise PermissionError("interactive approval request identifier is invalid")
    return value


def _ui_actor_id(ui_operator: Mapping[str, Any]) -> str:
    """Use a v2 signed principal when present; v1 uses a fixed Host label."""

    if ui_operator.get("version") == 2:
        candidate = ui_operator.get("principal_id")
        if isinstance(candidate, str) and candidate:
            return candidate
    return "ui.operator"


def _redacted_status(status: InteractiveApprovalStatus) -> dict[str, Any]:
    """Serialize only the narrow port's deliberately redacted response."""

    return {
        "request_id": status.request_id,
        "state": status.state,
        "expires_at": int(math.ceil(float(status.expires_at))),
        "typed_confirmation_required": status.typed_confirmation_required,
        "request_snapshot_digest": status.request_snapshot_digest,
        "typed_confirmation_digest": status.typed_confirmation_digest,
        "redacted_metadata": dict(status.redacted_metadata),
    }


def _redacted_effect_status(status: InteractiveEffectStatus) -> dict[str, Any]:
    """Serialize only the coordinator's deliberately redacted effect view."""

    return {
        "effect_id": status.effect_id,
        "approval_request_id": status.approval_request_id,
        "state": status.state,
        "expires_at": int(math.ceil(float(status.expires_at))),
        "redacted_metadata": dict(status.redacted_metadata),
    }


def _reject_effect_request_authority(value: object) -> None:
    """Reject nested client authority and routing claims before prepare dispatch."""

    forbidden = _V4_UNTRUSTED_AUTHORITY_FIELDS | {
        "backend",
        "backend_id",
        "domain",
        "domain_id",
        "principal",
        "principal_id",
        "provider",
        "provider_id",
        "publisher",
        "publisher_lineage",
        "scope",
        "target",
        "target_principal",
    }
    if isinstance(value, Mapping):
        for key, item in value.items():
            if not isinstance(key, str) or key.casefold() in forbidden:
                raise PermissionError("client authority material is not accepted")
            _reject_effect_request_authority(item)
    elif isinstance(value, list):
        for item in value:
            _reject_effect_request_authority(item)


HOST_PROVIDER_FACTORY = {
    _V4_FUNCTION_ID: InteractiveApprovalBridgeFactoryV4(),
    _EFFECT_FUNCTION_ID: InteractiveEffectCoordinatorFactoryV4(),
}
