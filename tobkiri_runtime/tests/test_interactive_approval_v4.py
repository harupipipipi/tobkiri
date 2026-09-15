"""Focused adversarial coverage for the Host-owned v4 approval port."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, replace

import pytest

from core_runtime.authority.ui_operator import sign_ui_operator
from core_runtime.authority.v4 import (
    ApprovalRecord,
    AuthorityDenied,
    AuthorityScope,
    GrantLifetime,
    GrantRecord,
    InteractiveApprovalDecision,
)
from core_runtime.host_contract import bind_host_contract
from tests.conformance_support.host_contract import host_contract
from tests.test_authority_v4_lifecycle import _Harness, _digest
from tests.test_tobkiri_host_authority_v4_adapter import _Principals, _context
from tobkiri_host.authority_v4 import AuthorityV4Adapter
from tobkiri_host.models import OpaqueAuthorityRef
from tobkiri_host.ports import (
    InteractiveApprovalDecisionCommand,
    InteractiveApprovalGetQuery,
    InteractiveApprovalListQuery,
    InteractiveApprovalRequestCommand,
)

_CONTRACT = host_contract(
    profile_id="profile-1",
    values={"panel_bootstrap_secret": "interactive-approval-test-secret-" + "x" * 32},
)


def _adapter(harness: _Harness) -> AuthorityV4Adapter:
    return AuthorityV4Adapter(
        harness.kernel,
        _Principals(harness.caller, harness.target),
    )


def _scope(
    harness: _Harness,
    *,
    request_digest: str,
    invocation_owner_id: str = "owner-1",
) -> dict[str, object]:
    """Build a scope with the exact bindings required for an approval."""

    value = harness.scope.to_dict()
    value["dimensions"] = {
        **value["dimensions"],
        "invocation_owner_id": [invocation_owner_id],
        "caller_session_id": ["session-caller"],
        "plan_digest": [_digest("plan")],
    }
    value["exact_request_digest"] = request_digest
    value["opaque"] = True
    return value


def _request_command(
    harness: _Harness,
    *,
    request_id: str = "interactive-request-1",
    request_digest: str | None = None,
    phrase: str | None = "APPROVE",
    scope: dict[str, object] | None = None,
) -> InteractiveApprovalRequestCommand:
    digest = request_digest or _digest(request_id)
    return InteractiveApprovalRequestCommand(
        context=_context(harness, request_id=request_id),
        target_principal=OpaqueAuthorityRef(harness.target.principal_id),
        request_digest=digest,
        base_scope=scope or _scope(harness, request_digest=digest),
        invocation_owner_id="owner-1",
        presentation_owner_principal_id=harness.caller.principal_id,
        presentation_owner_session_id="session-caller",
        caller_publisher_lineage="publisher.caller",
        target_publisher_lineage="publisher.target",
        expires_at=harness.clock() + 60,
        redacted_metadata={
            "action": "restart",
            "summary": "Restart local host",
            **({"confirmation_phrase": phrase} if phrase is not None else {}),
        },
        typed_confirmation_phrase=phrase,
    )


def _wire_digest(value: str) -> str:
    """Return the exact untagged digest required by native UI-operator v3."""

    assert value.startswith("sha256:")
    return value.removeprefix("sha256:")


def _operator(
    harness: _Harness,
    request_id: str,
    *,
    action: str,
    nonce: str,
    request_snapshot_digest: str | None = None,
    typed_confirmation_digest: str | None = None,
) -> dict[str, object]:
    request, _state = harness.kernel.interactive_approval(request_id)
    expected_confirmation_digest = (
        _wire_digest(request.typed_confirmation_digest)
        if action == "approve" and request.typed_confirmation_digest is not None
        else None
    )
    return sign_ui_operator(
        request_id,
        nonce=nonce,
        decision=action,
        request_snapshot_digest=request_snapshot_digest or _wire_digest(request.digest),
        typed_confirmation_digest=(
            expected_confirmation_digest
            if typed_confirmation_digest is None
            else typed_confirmation_digest
        ),
    )


def _decision_command(
    harness: _Harness,
    request_id: str,
    *,
    phrase: str = "APPROVE",
    nonce: str = "interactive-operator-nonce",
    action: str = "approve",
    request_snapshot_digest: str | None = None,
    typed_confirmation_digest: str | None = None,
) -> InteractiveApprovalDecisionCommand:
    return InteractiveApprovalDecisionCommand(
        context=_context(harness, request_id=request_id),
        request_id=request_id,
        actor_id="user-1",
        confirmation_text=phrase,
        ui_operator=_operator(
            harness,
            request_id,
            action=action,
            nonce=nonce,
            request_snapshot_digest=request_snapshot_digest,
            typed_confirmation_digest=typed_confirmation_digest,
        ),
    )


def test_approve_mints_only_one_shot_authority_and_returns_no_material(
    tmp_path,
) -> None:
    harness = _Harness(tmp_path)
    adapter = _adapter(harness)
    with bind_host_contract(_CONTRACT):
        pending = adapter.request_interactive_approval(_request_command(harness))
        approved = adapter.approve_interactive_approval(
            _decision_command(harness, pending.request_id)
        )

    assert pending.state == "pending"
    assert approved.state == "approved"
    assert approved.typed_confirmation_required is True
    request, _state = harness.kernel.interactive_approval(pending.request_id)
    assert asdict(approved) == {
        "request_id": "interactive-request-1",
        "state": "approved",
        "expires_at": 1060.0,
        "typed_confirmation_required": True,
        "request_snapshot_digest": _wire_digest(request.digest),
        "typed_confirmation_digest": _wire_digest(
            str(request.typed_confirmation_digest)
        ),
        "redacted_metadata": {
            "action": "restart",
            "confirmation_phrase": "APPROVE",
            "summary": "Restart local host",
        },
    }
    decision = harness.store.get_interactive_approval_decision(pending.request_id)
    assert decision is not None
    assert decision.typed_confirmation_verified is True
    approval = harness.store.get_approval(str(decision.approval_id))
    grant = harness.store.get_grant(str(decision.grant_id))
    assert approval is not None and grant is not None
    assert grant.lifetime.value == "one_shot"
    assert grant.max_uses == 1
    assert grant.session_id == "session-caller"
    assert grant.scope.exact_request_digest == _digest("interactive-request-1")


def test_approve_rejects_wrong_phrase_even_though_decision_has_no_boolean_input(
    tmp_path,
) -> None:
    harness = _Harness(tmp_path)
    adapter = _adapter(harness)
    with bind_host_contract(_CONTRACT):
        pending = adapter.request_interactive_approval(_request_command(harness))
        with pytest.raises(AuthorityDenied, match="typed confirmation"):
            adapter.approve_interactive_approval(
                _decision_command(harness, pending.request_id, phrase="approve")
            )

    assert adapter.interactive_approval_status(pending.request_id).state == "pending"
    assert harness.store.get_interactive_approval_decision(pending.request_id) is None


@pytest.mark.parametrize(
    (
        "operator_action",
        "request_snapshot_digest",
        "typed_confirmation_digest",
        "match",
    ),
    [
        ("deny", None, None, "decision mismatch"),
        ("approve", "0" * 64, None, "request snapshot mismatch"),
        ("approve", None, "f" * 64, "confirmation mismatch"),
    ],
)
def test_approve_rejects_each_tampered_v3_ui_operator_binding(
    tmp_path,
    operator_action: str,
    request_snapshot_digest: str | None,
    typed_confirmation_digest: str | None,
    match: str,
) -> None:
    """The signed native proof cannot be replayed into a different decision."""

    harness = _Harness(tmp_path)
    adapter = _adapter(harness)
    with bind_host_contract(_CONTRACT):
        pending = adapter.request_interactive_approval(_request_command(harness))
        with pytest.raises(AuthorityDenied, match=match):
            adapter.approve_interactive_approval(
                _decision_command(
                    harness,
                    pending.request_id,
                    action=operator_action,
                    request_snapshot_digest=request_snapshot_digest,
                    typed_confirmation_digest=typed_confirmation_digest,
                )
            )

    assert adapter.interactive_approval_status(pending.request_id).state == "pending"
    assert harness.store.get_interactive_approval_decision(pending.request_id) is None


def test_failed_confirmation_does_not_consume_proof_but_settlement_does(
    tmp_path,
) -> None:
    """A transient local failure may retry, while a settled request rejects reuse."""

    harness = _Harness(tmp_path)
    adapter = _adapter(harness)
    with bind_host_contract(_CONTRACT):
        pending = adapter.request_interactive_approval(_request_command(harness))
        command = _decision_command(
            harness,
            pending.request_id,
            phrase="wrong phrase",
            nonce="interactive-retry-proof",
        )
        with pytest.raises(AuthorityDenied, match="typed confirmation"):
            adapter.approve_interactive_approval(command)

        approved = adapter.approve_interactive_approval(
            replace(command, confirmation_text="APPROVE")
        )
        assert approved.state == "approved"
        with pytest.raises(AuthorityDenied, match="unavailable"):
            adapter.approve_interactive_approval(command)


def test_typed_confirmation_requires_the_exact_display_phrase_in_metadata(
    tmp_path,
) -> None:
    """The UI may display the phrase only when it matches the stored digest."""

    harness = _Harness(tmp_path)
    adapter = _adapter(harness)
    command = replace(
        _request_command(harness),
        redacted_metadata={"action": "restart", "summary": "Restart local host"},
    )
    with bind_host_contract(_CONTRACT):
        with pytest.raises(AuthorityDenied, match="confirmation display"):
            adapter.request_interactive_approval(command)

    assert (
        harness.store.get_interactive_approval_request(command.context.request_id)
        is None
    )


def test_authenticated_owner_queries_are_redacted_and_foreign_access_fails(
    tmp_path,
) -> None:
    """Presentation reads bind the durable owner principal and session exactly."""

    harness = _Harness(tmp_path)
    adapter = _adapter(harness)
    with bind_host_contract(_CONTRACT):
        pending = adapter.request_interactive_approval(_request_command(harness))
        query = InteractiveApprovalGetQuery(
            context=_context(harness, request_id=pending.request_id),
            request_id=pending.request_id,
        )
        assert adapter.get_interactive_approval(query).state == "pending"
        listed = adapter.list_interactive_approvals(
            InteractiveApprovalListQuery(context=query.context, state="pending")
        )

    assert [status.request_id for status in listed] == [pending.request_id]
    foreign = replace(
        query.context,
        caller_principal=OpaqueAuthorityRef(harness.target.principal_id),
    )
    with pytest.raises(AuthorityDenied, match="unavailable"):
        adapter.get_interactive_approval(
            InteractiveApprovalGetQuery(context=foreign, request_id=pending.request_id)
        )


def test_kernel_does_not_trust_typed_confirmation_boolean(tmp_path) -> None:
    """A forged audit assertion cannot replace the actual confirmation phrase."""

    harness = _Harness(tmp_path)
    adapter = _adapter(harness)
    pending = adapter.request_interactive_approval(_request_command(harness))
    request, _state = harness.kernel.interactive_approval(pending.request_id)
    decided_at = harness.clock()
    decision = InteractiveApprovalDecision(
        decision_id=request.request_id,
        request_id=request.request_id,
        request_snapshot_digest=request.digest,
        decision="approved",
        actor_id="user-1",
        decided_at=decided_at,
        security_epoch=request.security_epoch,
        ui_operator_digest=_digest("forged-ui-proof"),
        typed_confirmation_verified=True,
        approval_id="interactive-approval-forged",
        grant_id="interactive-grant-forged",
    )
    approval = ApprovalRecord(
        approval_id="interactive-approval-forged",
        snapshot_digest=request.digest,
        actor_id="user-1",
        decision="approved",
        decided_at=decided_at,
        caller=request.caller,
        target=request.target,
        profile_id=request.profile_id,
        effect_bundle_digest=request.base_scope.digest,
        security_epoch=request.security_epoch,
    )
    grant = GrantRecord(
        grant_id="interactive-grant-forged",
        caller=request.caller,
        target=request.target,
        profile_id=request.profile_id,
        activation_id=request.activation_id,
        profile_authority_digest=request.profile_authority_digest,
        caller_publisher_lineage=request.caller_publisher_lineage,
        target_publisher_lineage=request.target_publisher_lineage,
        scope=request.base_scope,
        lifetime=GrantLifetime.ONE_SHOT,
        security_epoch=request.security_epoch,
        approval_id=approval.approval_id,
        issued_at=decided_at,
        expires_at=request.expires_at,
        max_uses=1,
        session_id=request.caller_session_id,
    )

    with pytest.raises(AuthorityDenied, match="typed confirmation"):
        harness.kernel.settle_interactive_approval(
            decision,
            approval=approval,
            grant=grant,
            confirmation_text=None,
        )
    assert harness.store.get_interactive_approval_decision(request.request_id) is None


def test_request_rejects_scope_without_exact_owner_session_and_plan_bindings(
    tmp_path,
) -> None:
    harness = _Harness(tmp_path)
    adapter = _adapter(harness)
    digest = _digest("interactive-bad-scope")
    scope = _scope(harness, request_digest=digest)
    del scope["dimensions"]["plan_digest"]  # type: ignore[index]

    with pytest.raises(AuthorityDenied, match="plan_digest"):
        adapter.request_interactive_approval(
            _request_command(
                harness,
                request_id="interactive-bad-scope",
                request_digest=digest,
                scope=scope,
            )
        )


def test_settlement_reresolves_session_domain_before_minting_authority(
    tmp_path,
) -> None:
    harness = _Harness(tmp_path)
    adapter = _adapter(harness)
    with bind_host_contract(_CONTRACT):
        pending = adapter.request_interactive_approval(_request_command(harness))
        harness.store.transition_domain(
            harness.caller_domain.domain_id,
            expected_boot_epoch=harness.caller_domain.boot_epoch,
            expected_state=harness.caller_domain.state,
            new_state=type(harness.caller_domain.state).DRAINING,
        )
        with pytest.raises(AuthorityDenied, match="unavailable"):
            adapter.approve_interactive_approval(
                _decision_command(harness, pending.request_id)
            )

    assert harness.store.get_interactive_approval_decision(pending.request_id) is None


def test_approve_and_deny_race_has_exactly_one_durable_winner(tmp_path) -> None:
    harness = _Harness(tmp_path)
    adapter = _adapter(harness)
    request = _request_command(
        harness,
        request_id="interactive-race-1",
        phrase=None,
    )
    with bind_host_contract(_CONTRACT):
        adapter.request_interactive_approval(request)

    def settle(action: str) -> str:
        with bind_host_contract(_CONTRACT):
            command = _decision_command(
                harness,
                request.context.request_id,
                nonce=f"interactive-race-{action}",
                action=action,
            )
            try:
                if action == "approve":
                    return adapter.approve_interactive_approval(command).state
                return adapter.deny_interactive_approval(command).state
            except AuthorityDenied:
                return "lost"

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(settle, ("approve", "deny")))

    assert outcomes.count("lost") == 1
    assert {outcome for outcome in outcomes if outcome != "lost"} <= {
        "approved",
        "denied",
    }
    stored = harness.store.get_interactive_approval_decision(request.context.request_id)
    assert stored is not None


def test_generic_record_insertion_cannot_bypass_interactive_state_machine(
    tmp_path,
) -> None:
    harness = _Harness(tmp_path)
    adapter = _adapter(harness)
    pending = adapter.request_interactive_approval(_request_command(harness))
    request = harness.store.get_interactive_approval_request(pending.request_id)
    assert request is not None

    with pytest.raises(ValueError, match="dedicated state machine"):
        harness.store.put_record(request)
    with pytest.raises(ValueError, match="dedicated state machine"):
        harness.store.put_records_atomically((request,))
    assert isinstance(
        AuthorityScope.from_dict(request.base_scope.to_dict()), AuthorityScope
    )
