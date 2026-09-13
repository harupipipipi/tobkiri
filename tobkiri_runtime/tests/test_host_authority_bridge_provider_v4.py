"""V4 coverage for the presentation-only interactive approval bridge."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any, Mapping

import pytest

from core_runtime.host_provider_backend_v4 import HostProviderCaptureContextV4
from ecosystem.rumi_host_authority_bridge_pack.runtime import bridge
from tobkiri_host.broker import RequestEnvelope
from tobkiri_host.models import OpaqueAuthorityRef, RequestContext
from tobkiri_host.ports import (
    InteractiveApprovalDecisionCommand,
    InteractiveApprovalGetQuery,
    InteractiveApprovalListQuery,
    InteractiveApprovalStatus,
    OpaqueInvocationLease,
)
from tobkiri_protocol.canonical import canonical_digest

_FUNCTION_ID = "rumi_host_authority_bridge_pack.host-authority.interactive-approval"
_CONTRACT_ID = "tobkiri.service.interactive-approval.v1"


class _ApprovalPort:
    """Durable-record fake exposing only context-bound presentation queries."""

    def __init__(self) -> None:
        self._records: dict[str, tuple[RequestContext, InteractiveApprovalStatus]] = {}
        self.decisions: list[tuple[str, InteractiveApprovalDecisionCommand]] = []

    def seed(
        self,
        context: RequestContext,
        *,
        request_id: str,
        state: str = "pending",
    ) -> InteractiveApprovalStatus:
        """Seed a request as if the Host pending-effect controller created it."""

        status = InteractiveApprovalStatus(
            request_id=request_id,
            state=state,
            expires_at=123_456.0,
            typed_confirmation_required=True,
            request_snapshot_digest="a" * 64,
            typed_confirmation_digest="b" * 64,
            redacted_metadata={"summary": "Publish branch"},
        )
        self._records[request_id] = (context, status)
        return status

    def get_interactive_approval(
        self,
        query: InteractiveApprovalGetQuery,
    ) -> InteractiveApprovalStatus:
        """Return a record only if the Host context owns its durable record."""

        record = self._records.get(query.request_id)
        if record is None or _owner_key(record[0]) != _owner_key(query.context):
            raise PermissionError("interactive approval request is unavailable")
        return record[1]

    def list_interactive_approvals(
        self,
        query: InteractiveApprovalListQuery,
    ) -> tuple[InteractiveApprovalStatus, ...]:
        """Return only durable records owned by the authenticated context."""

        return tuple(
            status
            for _, (owner, status) in sorted(self._records.items())
            if _owner_key(owner) == _owner_key(query.context)
            and (query.state is None or status.state == query.state)
        )

    def approve_interactive_approval(
        self,
        command: InteractiveApprovalDecisionCommand,
    ) -> InteractiveApprovalStatus:
        """Settle an owned request while retaining the authenticated context."""

        return self._settle("approve", command, "approved")

    def deny_interactive_approval(
        self,
        command: InteractiveApprovalDecisionCommand,
    ) -> InteractiveApprovalStatus:
        """Deny an owned request while retaining the authenticated context."""

        return self._settle("deny", command, "denied")

    def _settle(
        self,
        kind: str,
        command: InteractiveApprovalDecisionCommand,
        state: str,
    ) -> InteractiveApprovalStatus:
        record = self._records.get(command.request_id)
        if record is None or _owner_key(record[0]) != _owner_key(command.context):
            raise PermissionError("interactive approval request is unavailable")
        _, status = record
        next_status = InteractiveApprovalStatus(
            request_id=status.request_id,
            state=state,
            expires_at=status.expires_at,
            typed_confirmation_required=status.typed_confirmation_required,
            request_snapshot_digest=status.request_snapshot_digest,
            typed_confirmation_digest=status.typed_confirmation_digest,
            redacted_metadata=status.redacted_metadata,
        )
        self._records[command.request_id] = (record[0], next_status)
        self.decisions.append((kind, command))
        return next_status


class _Invocation:
    def __init__(self, envelope: RequestEnvelope) -> None:
        self.envelope = envelope

    def contract_client(self, **_kwargs: Any) -> object:
        raise AssertionError("approval presentation has no nested Pack dependencies")


def _owner_key(context: RequestContext) -> tuple[str, str, str, str, int]:
    """Mirror the durable ownership inputs required by this test fake."""

    return (
        context.caller_principal.value,
        context.profile_id,
        context.activation_id,
        context.plan_digest,
        context.security_epoch,
    )


def _binding(operation_id: str, principal_id: str = "bridge-principal") -> Any:
    return SimpleNamespace(
        function=SimpleNamespace(
            function_id=_FUNCTION_ID,
            implementation_digest=canonical_digest({"implementation": operation_id}),
        ),
        operation=SimpleNamespace(
            contract_id=_CONTRACT_ID,
            contract_version="1.0.0",
            operation_id=operation_id,
        ),
        principal_ref=OpaqueAuthorityRef(principal_id),
        artifact=SimpleNamespace(
            digest=canonical_digest({"artifact": operation_id}),
            publisher_lineage="publisher.bridge",
        ),
    )


def _capture_provider(port: _ApprovalPort) -> Any:
    bindings = tuple(_binding(operation) for operation in sorted(bridge._V4_OPERATIONS))
    domains = {
        (
            item.operation.contract_id,
            item.operation.operation_id,
            item.principal_ref.value,
        ): f"domain.bridge.{index}"
        for index, item in enumerate(bindings)
    }
    activation = {"activation_id": "activation:interactive-approval"}
    return bridge.HOST_PROVIDER_FACTORY[_FUNCTION_ID].capture(
        HostProviderCaptureContextV4(
            profile_id="profile-1",
            plan_digest=canonical_digest({"plan": "interactive-approval"}),
            security_epoch=7,
            activation=activation,
            state_root=Path("/tmp/interactive-approval-bridge"),
            provider_bindings=bindings,
            catalog_bindings=bindings,
            domain_ids=domains,
            interactive_approval_port=port,
        )
    )


def _capture(port: _ApprovalPort) -> dict[str, Any]:
    captured = _capture_provider(port)
    return {item.operation_id: item for item in captured.contributions}


def _context(
    *,
    request_id: str = "interactive-request-1",
    caller: str = "caller-principal",
) -> RequestContext:
    activation = {"activation_id": "activation:interactive-approval"}
    return RequestContext(
        request_id=request_id,
        trace_id="trace-1",
        caller_principal=OpaqueAuthorityRef(caller),
        profile_id="profile-1",
        activation_id="activation:interactive-approval",
        activation_digest=canonical_digest(activation),
        plan_digest=canonical_digest({"plan": "interactive-approval"}),
        security_epoch=7,
        caller_session_id="session-caller",
        caller_domain_id="domain-caller",
        caller_boot_epoch=1,
        target_domain_id="domain-bridge",
        target_boot_epoch=1,
        target_backend_digest=canonical_digest({"backend": "bridge"}),
        profile_authority_digest=canonical_digest({"authority": "profile"}),
        fencing_token=3,
        handle_namespace="namespace-bridge",
    )


def _envelope(
    operation_id: str,
    *,
    request_id: str = "interactive-request-1",
    caller: str = "caller-principal",
) -> RequestEnvelope:
    return RequestEnvelope(
        context=_context(request_id=request_id, caller=caller),
        target_principal=OpaqueAuthorityRef("bridge-principal"),
        target_domain=OpaqueAuthorityRef("domain-bridge"),
        contract_id=_CONTRACT_ID,
        contract_version="1.0.0",
        operation_id=operation_id,
        payload={},
        request_digest=canonical_digest({"request_id": request_id, "operation": operation_id}),
        deadline_monotonic=10_000_000.0,
        lease=OpaqueInvocationLease(b"opaque-lease"),
        idempotency_key=None,
    )


def _invoke(
    contributions: Mapping[str, Any],
    operation_id: str,
    payload: Mapping[str, Any],
    *,
    request_id: str = "interactive-request-1",
    caller: str = "caller-principal",
) -> Mapping[str, Any]:
    return contributions[operation_id].invoke(
        operation_id,
        payload,
        _Invocation(_envelope(operation_id, request_id=request_id, caller=caller)),
    )


def _seed(port: _ApprovalPort, request_id: str = "interactive-request-1") -> None:
    port.seed(_context(request_id=request_id), request_id=request_id)


def test_factory_is_separate_presentation_contract_with_no_request_operation() -> None:
    port = _ApprovalPort()
    contributions = _capture(port)

    factory = bridge.HOST_PROVIDER_FACTORY[_FUNCTION_ID]
    assert factory.function_id == _FUNCTION_ID
    assert set(contributions) == {
        bridge._V4_GET_OPERATION,
        bridge._V4_LIST_OPERATION,
        bridge._V4_APPROVE_OPERATION,
        bridge._V4_DENY_OPERATION,
    }
    assert bridge._V4_CONTRACT_ID == _CONTRACT_ID
    assert "request" not in bridge._V4_OPERATIONS
    assert "authorize" not in factory.function_id


def test_factory_rejects_a_partial_or_legacy_operation_set() -> None:
    port = _ApprovalPort()
    binding = _binding(bridge._V4_GET_OPERATION)

    with pytest.raises(PermissionError, match="bindings are incomplete"):
        bridge.HOST_PROVIDER_FACTORY[_FUNCTION_ID].capture(
            HostProviderCaptureContextV4(
                profile_id="profile-1",
                plan_digest=canonical_digest({"plan": "interactive-approval"}),
                security_epoch=7,
                activation={"activation_id": "activation:interactive-approval"},
                state_root=Path("/tmp/interactive-approval-bridge"),
                provider_bindings=(binding,),
                catalog_bindings=(binding,),
                domain_ids={},
                interactive_approval_port=port,
            )
        )


def test_get_and_list_survive_provider_recapture_via_durable_host_owner_filter() -> None:
    port = _ApprovalPort()
    _seed(port)
    first = _capture_provider(port)
    first_contributions = {item.operation_id: item for item in first.contributions}

    original = _invoke(
        first_contributions,
        bridge._V4_GET_OPERATION,
        {"request_id": "interactive-request-1"},
    )
    first.close()
    second = _capture_provider(port)
    contributions = {item.operation_id: item for item in second.contributions}
    after_recapture = _invoke(
        contributions,
        bridge._V4_GET_OPERATION,
        {"request_id": "interactive-request-1"},
    )
    listed = _invoke(contributions, bridge._V4_LIST_OPERATION, {})

    assert after_recapture == original
    assert listed == {"approvals": [original]}
    with pytest.raises(PermissionError, match="unavailable"):
        _invoke(
            contributions,
            bridge._V4_GET_OPERATION,
            {"request_id": "interactive-request-1"},
            caller="foreign-principal",
        )


def test_decisions_forward_authenticated_context_and_return_only_redacted_status() -> None:
    port = _ApprovalPort()
    _seed(port)
    contributions = _capture(port)
    approved = _invoke(
        contributions,
        bridge._V4_APPROVE_OPERATION,
        {
            "request_id": "interactive-request-1",
            "confirmation_text": "APPROVE",
            "ui_operator": {"version": 1, "signature": "signed-by-host"},
        },
    )

    decision_kind, command = port.decisions[-1]
    assert decision_kind == "approve"
    assert command.context == _context()
    assert command.actor_id == "ui.operator"
    assert approved["state"] == "approved"
    assert set(approved) == {
        "request_id",
        "state",
        "expires_at",
        "typed_confirmation_required",
        "request_snapshot_digest",
        "typed_confirmation_digest",
        "redacted_metadata",
    }
    assert isinstance(approved["expires_at"], int)
    assert not {
        "grant",
        "grant_id",
        "receipt",
        "scope",
        "token",
        "raw_payload",
    } & set(approved)


def test_deny_uses_the_minimal_wire_payload_without_confirmation_text() -> None:
    port = _ApprovalPort()
    _seed(port)
    contributions = _capture(port)

    denied = _invoke(
        contributions,
        bridge._V4_DENY_OPERATION,
        {
            "request_id": "interactive-request-1",
            "ui_operator": {"version": 1, "signature": "signed-by-host"},
        },
    )

    decision_kind, command = port.decisions[-1]
    assert decision_kind == "deny"
    assert command.confirmation_text == ""
    assert denied["state"] == "denied"


@pytest.mark.parametrize(
    ("operation_id", "payload"),
    [
        (bridge._V4_LIST_OPERATION, {"token": "forged"}),
        (
            bridge._V4_GET_OPERATION,
            {"request_id": "interactive-request-1", "grant": "x"},
        ),
        (
            bridge._V4_APPROVE_OPERATION,
            {
                "request_id": "interactive-request-1",
                "confirmation_text": "APPROVE",
                "ui_operator": {"version": 1},
                "approved": True,
            },
        ),
    ],
)
def test_client_authority_material_and_extra_fields_are_rejected(
    operation_id: str,
    payload: Mapping[str, Any],
) -> None:
    port = _ApprovalPort()
    _seed(port)
    contributions = _capture(port)

    with pytest.raises(PermissionError, match="client authority"):
        _invoke(contributions, operation_id, payload)


def test_unknown_payload_fields_and_deny_confirmation_are_rejected() -> None:
    port = _ApprovalPort()
    _seed(port)
    contributions = _capture(port)

    with pytest.raises(PermissionError, match="payload fields"):
        _invoke(contributions, bridge._V4_LIST_OPERATION, {"unexpected": True})
    with pytest.raises(PermissionError, match="payload fields"):
        _invoke(
            contributions,
            bridge._V4_DENY_OPERATION,
            {
                "request_id": "interactive-request-1",
                "confirmation_text": "not permitted",
                "ui_operator": {"version": 1},
            },
        )


def test_v4_presentation_never_uses_legacy_request_store_or_receipts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    port = _ApprovalPort()
    _seed(port)
    contributions = _capture(port)

    class _LegacyStore:
        def __init__(self) -> None:
            pytest.fail("V4 presentation must not construct AuthorityRequestStore")

    monkeypatch.setattr(bridge, "AuthorityRequestStore", _LegacyStore)
    result = _invoke(contributions, bridge._V4_LIST_OPERATION, {})
    assert result["approvals"][0]["state"] == "pending"
    assert callable(bridge.create_authority_operation)
