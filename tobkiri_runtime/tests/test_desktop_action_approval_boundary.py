"""Approval-boundary regression for the production desktop action route.

The captured Profile, ResolvedPlan bindings, Host Broker, Authority
kernel/store, PendingEffect controller, interactive-approval bridge, and the
legacy ``host_contract_adapter`` route are the real production objects.  The
only test double is the PackVM backend transport at the provider execution
boundary: it executes the real desktop service artifact and records a
sentinel only after the Broker's final authority recheck submits work, so
every denial below is proven to happen before provider execution.

The legacy adapter-facing identifiers ``rumi.action.desktop.host.v1`` /
``desktop.accessibility.action`` are exercised through the real adapter and
prove fail-closed at the captured-edge boundary; the packaged v4 control
operation ``rumi_desktop_host_service_pack.desktop-host-control`` is the only
executable edge the Profile admits.

The PendingEffect coordinator is the production ``PendingEffectController``
class wired over the session's real persistence and approval ports: the
finite ``interactive_effect.manage`` kind set intentionally has no desktop
kind, and its routes require Host-extension execute targets while the desktop
provider is a PackVM artifact.

This test does not claim macOS Accessibility/TCC, any native OS permission,
or PackVM sandbox acceptance.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
import secrets
import time
from typing import Any, Mapping

import pytest

from core_runtime.authority.ui_operator import sign_ui_operator
from core_runtime.authority.v4 import AuthorityDenied, AuthorityScope
from core_runtime.global_contract_dispatch import invoke_global_contract
from core_runtime.interactive_effect_coordinator import _effect_scope
from ecosystem.rumi_default_tools_pack.domain.tool import (
    host_contract_adapter as adapter,
)
from ecosystem.rumi_desktop_host_service_pack.runtime.service import (
    create_desktop_control,
)
from tests.conformance_support.host_profile import captured_host_profile
from tests.test_production_frontend_contract_http import (
    _ShellPolicyPackVmBackend,
)
from tobkiri_host.broker import PreparedInvocationSnapshot, RequestEnvelope
from tobkiri_host.effects import ProviderOutcome
from tobkiri_host.errors import (
    AuthorizationError,
    BackendUnavailableError,
    ProviderExecutionError,
    ResolutionError,
)
from tobkiri_host.interactive_effects import (
    PendingEffectController,
    PendingEffectError,
    PendingEffectState,
)
from tobkiri_host.models import (
    InvocationFrame,
    OpaqueAuthorityRef,
    RequestContext,
)
from tobkiri_host.ports import InteractiveApprovalGrantAttestation


pytestmark = pytest.mark.contract

_DESKTOP_PACK = "rumi_desktop_host_service_pack"
_DESKTOP_PROVIDER = "rumi_desktop_host_service_pack.desktop-host.control"
_DESKTOP_CONTRACT = "tobkiri.action.desktop.host.v1"
_DESKTOP_OPERATION = "rumi_desktop_host_service_pack.desktop-host-control"
_DESKTOP_SERVICE_OPERATION = "desktop.accessibility.action"
_DESKTOP_HOST_FUNCTION = "computer.semantic_action"
_LEGACY_CONTRACT = "rumi.action.desktop.host.v1"
_LEGACY_OPERATION = "desktop.accessibility.action"
_APPROVAL_CONTRACT = "tobkiri.service.interactive-approval.v1"
_APPROVAL_GET = "interactive_approval.get"
_APPROVAL_APPROVE = "interactive_approval.approve"
_SHELL_CALLER = "shell.tauri.default"
_SHELL_PUBLISHER_LINEAGE = "tobkiri.repository"
_OWNER = "session.desktop-owner"
_FOREIGN = "session.desktop-foreign"
_CONFIRMATION = "EXECUTE"
_EFFECT_TTL_SECONDS = 300.0

_DESKTOP_EDGE = {
    "caller_function_id": _SHELL_CALLER,
    "target_provider_id": _DESKTOP_PROVIDER,
    "contract_id": _DESKTOP_CONTRACT,
    "operation_id": _DESKTOP_OPERATION,
    "authority_mode": "interactive_only",
    "requested_scope_template": {
        "capability": "operation.invoke",
        "dimensions": {
            "contract": [_DESKTOP_CONTRACT],
            "operation": [_DESKTOP_OPERATION],
        },
        "quotas": {},
        "exact_request_digest": None,
        "opaque": False,
    },
}

_DESKTOP_OBSERVE_EDGE = {
    "caller_function_id": _SHELL_CALLER,
    "target_provider_id": "rumi_desktop_host_service_pack.desktop-host.observe",
    "contract_id": "tobkiri.resource.desktop.host.v1",
    "operation_id": "rumi_desktop_host_service_pack.desktop-host-observe",
    "authority_mode": "profile_grant",
    "requested_scope_template": {
        "capability": "operation.invoke",
        "dimensions": {
            "contract": ["tobkiri.resource.desktop.host.v1"],
            "operation": ["rumi_desktop_host_service_pack.desktop-host-observe"],
        },
        "quotas": {},
        "exact_request_digest": None,
        "opaque": False,
    },
}

_PRESENTATION_METADATA = {
    "action": "Desktop accessibility action",
    "summary": "Perform the requested desktop accessibility action.",
    "detail": "Intent: press the Save button",
    "confirmation_phrase": _CONFIRMATION,
}


def _desktop_action_payload() -> dict[str, Any]:
    """Return one JSON-shaped desktop control payload for the exact edge."""

    return {
        "operation": _DESKTOP_SERVICE_OPERATION,
        "arguments": {
            "app": "Editor",
            "element_id": "AX-SAVE",
            "intent": "press the Save button",
        },
    }


class _DesktopPackVmBackend(_ShellPolicyPackVmBackend):
    """Test transport executing the sealed desktop service artifact.

    The production Broker, Authority, materialization, and envelope checks
    are unchanged.  ``dispatches`` is the execution sentinel: it increments
    only inside the provider boundary the Broker submits to after its final
    authority recheck, so a flat counter proves a denial preceded execution.
    """

    _PACK_ID = _DESKTOP_PACK
    _FUNCTION_ID = _DESKTOP_PROVIDER
    _CONTRACT_ID = _DESKTOP_CONTRACT
    _OPERATION_ID = _DESKTOP_OPERATION

    def __init__(self) -> None:
        super().__init__()
        self.dispatches = 0
        self.intents: list[dict[str, Any]] = []
        self.request_digests: list[str] = []
        self._service = create_desktop_control()

    def invoke(self, request: object) -> ProviderOutcome:
        """Record the boundary reach, then run the real desktop service."""

        if (
            not isinstance(request, RequestEnvelope)
            or request.target_domain.value != self._target_domain_id
            or request.contract_id != self._CONTRACT_ID
            or request.operation_id != self._OPERATION_ID
        ):
            raise BackendUnavailableError("test PackVM envelope is invalid")
        payload = dict(request.payload)
        arguments = payload.get("arguments")
        if (
            payload.get("operation") != _DESKTOP_SERVICE_OPERATION
            or not isinstance(arguments, Mapping)
        ):
            raise BackendUnavailableError("test PackVM payload is invalid")
        self.dispatches += 1
        self.request_digests.append(request.request_digest)
        intent = self._service.invoke(
            _DESKTOP_SERVICE_OPERATION,
            dict(arguments),
            context={"reason": "desktop approval boundary test"},
        )
        self.intents.append(dict(intent))
        return ProviderOutcome(dict(intent))


@dataclass(frozen=True)
class _DesktopEffect:
    """One durable Host-owned effect plus its immutable approval binding."""

    controller: PendingEffectController
    context: RequestContext
    snapshot: PreparedInvocationSnapshot
    scope_dict: Mapping[str, Any]
    effect_id: str
    approval_request_id: str
    invocation_owner_id: str
    target_principal: str
    target_publisher_lineage: str
    expires_at: float


@pytest.fixture
def desktop_session(tmp_path, monkeypatch):
    """Capture one production session with the desktop control edge."""

    backend = _DesktopPackVmBackend()
    with captured_host_profile(
        tmp_path,
        monkeypatch,
        packs=(_DESKTOP_PACK,),
        edges=(_DESKTOP_EDGE,),
        backends=(backend,),
    ) as (session, store):
        yield session, store, backend


def _prepare_effect(session, session_id: str) -> _DesktopEffect:
    """Prepare the exact desktop invocation and open its approval request.

    This mirrors the production coordinator wiring: ``context_for`` derives
    the Shell caller from the captured edge, ``broker.prepare`` freezes the
    exact snapshot, the committed edge ceiling is narrowed by the canonical
    ``_effect_scope`` helper, and the production ``PendingEffectController``
    persists the durable record and opens the Host approval request.
    """

    context = session.context_for(
        _DESKTOP_CONTRACT, _DESKTOP_OPERATION, session_id
    )
    payload = _desktop_action_payload()
    prepared = session.broker.prepare(
        InvocationFrame(
            contract_id=_DESKTOP_CONTRACT,
            version_range=None,
            operation_id=_DESKTOP_OPERATION,
            payload=payload,
        ),
        context,
    )
    ceiling = AuthorityScope.from_dict(
        session.effect_scope_for(
            _DESKTOP_CONTRACT, _DESKTOP_OPERATION, payload, context
        )
    )
    invocation_owner_id = "interactive-effect-owner." + secrets.token_hex(16)
    scope = _effect_scope(
        ceiling,
        request_digest=prepared.request_digest,
        invocation_owner_id=invocation_owner_id,
        caller_session_id=context.caller_session_id,
        plan_digest=context.plan_digest,
    )
    controller = PendingEffectController(
        persistence=session.authority_control,
        approvals=session.authority_control,
        coordinator_principal=context.caller_principal,
        coordinator_publisher_lineage=_SHELL_PUBLISHER_LINEAGE,
    )
    expires_at = time.time() + _EFFECT_TTL_SECONDS
    status = controller.prepare(
        prepared=prepared,
        context=context,
        effect_scope=scope.to_dict(),
        invocation_owner_id=invocation_owner_id,
        presentation_owner_principal_id=context.caller_principal.value,
        presentation_owner_session_id=context.caller_session_id,
        presentation_metadata=_PRESENTATION_METADATA,
        expires_at=expires_at,
        typed_confirmation_phrase=_CONFIRMATION,
    )
    assert status.state is PendingEffectState.APPROVAL_PENDING
    snapshot = prepared.to_snapshot()
    return _DesktopEffect(
        controller=controller,
        context=context,
        snapshot=snapshot,
        scope_dict=scope.to_dict(),
        effect_id=status.effect_id,
        approval_request_id=status.approval_request_id,
        invocation_owner_id=invocation_owner_id,
        target_principal=str(snapshot.binding_fingerprint["target_principal"]),
        target_publisher_lineage=str(
            snapshot.binding_fingerprint["artifact"]["publisher_lineage"]
        ),
        expires_at=expires_at,
    )


def _invoke_prepared(
    session,
    effect: _DesktopEffect,
    *,
    context: RequestContext | None = None,
    scope: Mapping[str, Any] | None = None,
    snapshot: PreparedInvocationSnapshot | None = None,
) -> Mapping[str, Any]:
    """Replay one durable receipt through the real Broker boundary."""

    return session.broker.invoke_prepared(
        snapshot if snapshot is not None else effect.snapshot,
        context if context is not None else effect.context,
        scope if scope is not None else effect.scope_dict,
        execute_not_after_wall=effect.expires_at,
    )


def _resume(session, effect: _DesktopEffect):
    """Resume through the production controller and its single Broker."""

    return effect.controller.resume(effect.effect_id, session.broker)


def _approval_view(session, request_id: str, session_id: str) -> Mapping[str, Any]:
    """Read the redacted approval view through the production bridge."""

    return session.invoke(
        _APPROVAL_CONTRACT,
        _APPROVAL_GET,
        {"request_id": request_id, "_session_id": session_id},
    )


def _approve(session, request_id: str, session_id: str) -> Mapping[str, Any]:
    """Settle the approval via typed confirmation and signed UI operator."""

    view = _approval_view(session, request_id, session_id)
    return session.invoke(
        _APPROVAL_CONTRACT,
        _APPROVAL_APPROVE,
        {
            "request_id": request_id,
            "confirmation_text": _CONFIRMATION,
            "ui_operator": sign_ui_operator(
                request_id,
                nonce="desktop-approval-" + secrets.token_hex(8),
                decision="approve",
                request_snapshot_digest=view["request_snapshot_digest"],
                typed_confirmation_digest=view["typed_confirmation_digest"],
            ),
            "_session_id": session_id,
        },
    )


def _assert_allow_once(
    session,
    effect: _DesktopEffect,
    *,
    context: RequestContext | None = None,
    scope: Mapping[str, Any] | None = None,
) -> None:
    """Assert the approved one-shot Grant equals the immutable effect."""

    session.authority_control.assert_interactive_approval_grant(
        InteractiveApprovalGrantAttestation(
            request_id=effect.approval_request_id,
            context=context if context is not None else effect.context,
            target_principal=OpaqueAuthorityRef(effect.target_principal),
            request_digest=effect.snapshot.request_digest,
            base_scope=(
                scope if scope is not None else effect.scope_dict
            ),
            invocation_owner_id=effect.invocation_owner_id,
            caller_publisher_lineage=_SHELL_PUBLISHER_LINEAGE,
            target_publisher_lineage=effect.target_publisher_lineage,
            expires_at=effect.expires_at,
        )
    )


def test_desktop_allow_once_executes_once_and_rejects_receipt_replay(
    desktop_session,
) -> None:
    """One canonical approval executes once; its receipt never replays."""

    session, _store, backend = desktop_session
    effect = _prepare_effect(session, _OWNER)

    # No profile Grant exists for the interactive_only edge, so every form
    # of execution is denied before the approval decision exists.
    with pytest.raises(AuthorizationError):
        _invoke_prepared(session, effect)
    with pytest.raises(PendingEffectError):
        _resume(session, effect)
    with pytest.raises(AuthorizationError):
        session.invoke(
            _DESKTOP_CONTRACT,
            _DESKTOP_OPERATION,
            {**_desktop_action_payload(), "_session_id": _OWNER},
        )
    assert backend.dispatches == 0

    view = _approval_view(session, effect.approval_request_id, _OWNER)
    assert view["state"] == "pending"
    assert view["typed_confirmation_required"] is True
    assert view["max_uses"] == 1
    assert view["remaining_uses"] == 1
    assert view["target_principal_id"] == effect.target_principal
    assert (
        view["base_scope"]["exact_request_digest"]
        == effect.snapshot.request_digest
    )
    assert (
        view["redacted_metadata"]["confirmation_phrase"] == _CONFIRMATION
    )

    settled = _approve(session, effect.approval_request_id, _OWNER)
    assert settled["state"] == "approved"
    _assert_allow_once(session, effect)

    status = _resume(session, effect)
    assert status.state is PendingEffectState.SUCCEEDED
    assert backend.dispatches == 1
    assert backend.request_digests == [effect.snapshot.request_digest]
    intent = backend.intents[0]
    assert intent["type"] == "host_intent"
    assert intent["operation"] == "host.intent.execute"
    assert intent["host_function_id"] == _DESKTOP_HOST_FUNCTION
    assert intent["args"]["intent"] == "press the Save button"

    # The same receipt cannot mint a second execution: the durable state is
    # terminal and the one-shot Grant is consumed.
    with pytest.raises(PendingEffectError):
        _resume(session, effect)
    with pytest.raises(AuthorizationError):
        _invoke_prepared(session, effect)
    # A settled request cannot be approved again, and the same request
    # identity cannot be reopened for a second Grant.
    with pytest.raises(ProviderExecutionError):
        session.invoke(
            _APPROVAL_CONTRACT,
            _APPROVAL_APPROVE,
            {
                "request_id": effect.approval_request_id,
                "confirmation_text": _CONFIRMATION,
                "ui_operator": sign_ui_operator(
                    effect.approval_request_id,
                    nonce="desktop-reapproval",
                    decision="approve",
                    request_snapshot_digest=view["request_snapshot_digest"],
                    typed_confirmation_digest=(
                        view["typed_confirmation_digest"]
                    ),
                ),
                "_session_id": _OWNER,
            },
        )
    with pytest.raises(AuthorityDenied):
        _assert_allow_once(session, effect)
    assert backend.dispatches == 1
    assert backend.intents == [intent]


def test_desktop_allow_once_rejects_mutated_scope(desktop_session) -> None:
    """A mutated scope, payload, or attestation never reaches execution."""

    session, _store, backend = desktop_session
    effect = _prepare_effect(session, _OWNER)
    _approve(session, effect.approval_request_id, _OWNER)

    dimensions = dict(effect.scope_dict["dimensions"])
    # Dropping an exact binding dimension widens the scope beyond the Grant.
    widened = dict(effect.scope_dict)
    widened["dimensions"] = {
        name: values
        for name, values in dimensions.items()
        if name != "caller_session_id"
    }
    # A changed dimension value is outside the approved scope.
    shifted = dict(effect.scope_dict)
    shifted["dimensions"] = {
        **dimensions,
        "caller_session_id": ["session.forged"],
    }
    # A different request digest can never equal the approved request.
    other_digest = dict(effect.scope_dict)
    other_digest["exact_request_digest"] = "sha256:" + "0" * 64
    for scope in (widened, shifted, other_digest):
        with pytest.raises(AuthorizationError):
            _invoke_prepared(session, effect, scope=scope)
    # Mutating the frozen Broker snapshot changes the request digest.
    forged_snapshot = replace(
        effect.snapshot,
        normalized_payload={
            "operation": _DESKTOP_SERVICE_OPERATION,
            "arguments": {"intent": "delete everything"},
        },
    )
    with pytest.raises(AuthorizationError):
        _invoke_prepared(session, effect, snapshot=forged_snapshot)
    # The Host attestation also binds the complete immutable scope.
    with pytest.raises(AuthorityDenied):
        _assert_allow_once(session, effect, scope=shifted)
    assert backend.dispatches == 0

    status = _resume(session, effect)
    assert status.state is PendingEffectState.SUCCEEDED
    assert backend.dispatches == 1


def test_desktop_allow_once_rejects_foreign_profile_and_caller(
    desktop_session, tmp_path, monkeypatch
) -> None:
    """A foreign caller or Profile cannot reuse the Host-owned approval."""

    session, _store, backend = desktop_session
    effect = _prepare_effect(session, _OWNER)
    _approve(session, effect.approval_request_id, _OWNER)
    view = _approval_view(session, effect.approval_request_id, _OWNER)

    foreign_context = session.context_for(
        _DESKTOP_CONTRACT, _DESKTOP_OPERATION, _FOREIGN
    )
    # A different caller session changes the durable context fingerprint.
    with pytest.raises(AuthorizationError):
        _invoke_prepared(session, effect, context=foreign_context)
    # Even the same owner with a fresh Host request cannot replay the
    # frozen receipt: request_id participates in the context fingerprint.
    with pytest.raises(AuthorizationError):
        _invoke_prepared(
            session,
            effect,
            context=session.context_for(
                _DESKTOP_CONTRACT, _DESKTOP_OPERATION, _OWNER
            ),
        )
    # A foreign presenter cannot observe or settle the approval, even with
    # a correctly signed UI-operator proof for the real request snapshot.
    with pytest.raises(ProviderExecutionError):
        _approval_view(session, effect.approval_request_id, _FOREIGN)
    with pytest.raises(ProviderExecutionError):
        session.invoke(
            _APPROVAL_CONTRACT,
            _APPROVAL_APPROVE,
            {
                "request_id": effect.approval_request_id,
                "confirmation_text": _CONFIRMATION,
                "ui_operator": sign_ui_operator(
                    effect.approval_request_id,
                    nonce="desktop-foreign-approve",
                    decision="approve",
                    request_snapshot_digest=view["request_snapshot_digest"],
                    typed_confirmation_digest=(
                        view["typed_confirmation_digest"]
                    ),
                ),
                "_session_id": _FOREIGN,
            },
        )
    # The Host attestation rejects the foreign caller's domain binding.
    with pytest.raises(AuthorityDenied):
        _assert_allow_once(session, effect, context=foreign_context)
    assert backend.dispatches == 0

    # A context captured under a different Profile/activation/Plan cannot
    # replay the effect or satisfy the approval attestation.  The extra
    # observe edge gives the foreign capture a different resolved Plan.
    foreign_backend = _DesktopPackVmBackend()
    with captured_host_profile(
        tmp_path / "foreign-profile",
        monkeypatch,
        packs=(_DESKTOP_PACK,),
        edges=(_DESKTOP_EDGE, _DESKTOP_OBSERVE_EDGE),
        backends=(foreign_backend,),
    ) as (other_session, _other_store):
        other_context = other_session.context_for(
            _DESKTOP_CONTRACT, _DESKTOP_OPERATION, _FOREIGN
        )
        with pytest.raises(AuthorizationError):
            _invoke_prepared(session, effect, context=other_context)
        with pytest.raises(AuthorityDenied):
            _assert_allow_once(session, effect, context=other_context)
    assert backend.dispatches == 0
    assert foreign_backend.dispatches == 0

    status = _resume(session, effect)
    assert status.state is PendingEffectState.SUCCEEDED
    assert backend.dispatches == 1


def test_desktop_action_rejects_boolean_only_approval_context(
    desktop_session,
) -> None:
    """A bare boolean is client material, never Host approval authority."""

    session, _store, backend = desktop_session
    effect = _prepare_effect(session, _OWNER)

    # Client authority fields and missing decision fields are rejected at
    # Contract schema validation or inside the bridge, always before the
    # desktop Provider boundary.
    for operation, payload in (
        (_APPROVAL_GET, {"request_id": effect.approval_request_id,
                         "approved": True}),
        (_APPROVAL_APPROVE, {"request_id": effect.approval_request_id,
                             "approved": True}),
        (_APPROVAL_APPROVE, {"request_id": effect.approval_request_id,
                             "approval_token": "forged"}),
        (_APPROVAL_APPROVE, {"request_id": effect.approval_request_id}),
        (_APPROVAL_APPROVE, {"request_id": effect.approval_request_id,
                             "confirmation_text": _CONFIRMATION,
                             "ui_operator": True}),
    ):
        with pytest.raises((ResolutionError, ProviderExecutionError)):
            session.invoke(
                _APPROVAL_CONTRACT,
                operation,
                {**payload, "_session_id": _OWNER},
            )
    # A schema-valid envelope whose only "approval" is a boolean inside the
    # operator slot still fails the signed UI-operator binding in the Host.
    with pytest.raises(ProviderExecutionError):
        session.invoke(
            _APPROVAL_CONTRACT,
            _APPROVAL_APPROVE,
            {
                "request_id": effect.approval_request_id,
                "confirmation_text": _CONFIRMATION,
                "ui_operator": {"approved": True},
                "_session_id": _OWNER,
            },
        )
    # No client boolean reaches the desktop Provider: the interactive_only
    # edge has no direct Grant, so every form is denied before execution.
    for payload in (
        {**_desktop_action_payload(), "approved": True},
        {"approved": True},
        {
            **_desktop_action_payload(),
            "approval_token": "forged",
            "viewer_host_approved": True,
        },
    ):
        with pytest.raises(AuthorizationError):
            session.invoke(
                _DESKTOP_CONTRACT,
                _DESKTOP_OPERATION,
                {**payload, "_session_id": _OWNER},
            )
    assert backend.dispatches == 0

    # The untouched approval still completes through the signed path.
    _approve(session, effect.approval_request_id, _OWNER)
    _assert_allow_once(session, effect)
    status = _resume(session, effect)
    assert status.state is PendingEffectState.SUCCEEDED
    assert backend.dispatches == 1


def test_legacy_desktop_route_fails_closed_at_captured_edge(
    desktop_session, monkeypatch
) -> None:
    """The real adapter cannot route legacy identifiers to execution.

    ``rumi.action.desktop.host.v1`` / ``desktop.accessibility.action`` is
    the adapter-facing surface; the captured v4 session admits only the
    packaged contract edge, so the legacy route is denied at edge selection
    before any Provider boundary, with client authority fields stripped.
    """

    session, _store, backend = desktop_session

    class _Container:
        def get_or_none(self, name: str) -> object | None:
            return session if name == "v4_dispatch_session" else None

    monkeypatch.setattr(adapter, "get_container", lambda: _Container())
    with pytest.raises(AuthorityDenied):
        adapter.run_host_contract_action(
            "computer.semantic_action",
            {
                "app": "Editor",
                "intent": "press the Save button",
                "approved": True,
                "approval_token": "forged",
                "_session_id": _OWNER,
            },
            source_function_id="computer_semantic_action",
        )
    with pytest.raises(AuthorityDenied):
        invoke_global_contract(
            session,
            _LEGACY_CONTRACT,
            _LEGACY_OPERATION,
            {**_desktop_action_payload(), "_session_id": _OWNER},
        )
    assert backend.dispatches == 0
