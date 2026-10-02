"""Bind a verified wake declaration to one selected edge and real Broker."""

from __future__ import annotations

from contextlib import AbstractContextManager
from pathlib import Path
import threading
import time
from typing import Any, Callable, Mapping

from core_runtime.captured_wake_v4 import (
    CapturedWakeDeclarationV4,
    CapturedWakeDriverV4,
    HostProcessWakeAdapterV4,
    LateBoundWakePortV4,
)
from core_runtime.authority.v4 import AuthorityScope, GrantLifetime
from tobkiri_host.models import InvocationFrame, OpaqueAuthorityRef, RequestContext
from tobkiri_host.ports import StaticAuthorityQuery
from tobkiri_host.triggers import TriggerRegistration
from tobkiri_protocol.canonical import canonical_digest


def bind_production_wake_v4(
    *,
    port: LateBoundWakePortV4,
    declaration: CapturedWakeDeclarationV4,
    owner_principal_id: str,
    target_principal_id: str,
    state_path: Path,
    identity: Mapping[str, Any],
    broker: Any,
    authority: Any,
    authority_store: Any,
    context_scope: Callable[[str], AbstractContextManager[RequestContext]],
    effect_scope: Callable[[RequestContext], Mapping[str, Any]],
    assert_current: Callable[[], None],
    wall_clock: Callable[[], float] = time.time,
    monotonic_clock: Callable[[], float] = time.monotonic,
    adapter_factory: Any = HostProcessWakeAdapterV4,
) -> CapturedWakeDriverV4:
    """Create only fresh audited requests from a dedicated selected clock caller.

    Production capture supplies a context scope which registers the exact clock
    principal's new Host session and signed edge. No UI/session or invocation
    object is accepted or retained by this binding.
    """
    captured = CapturedWakeDeclarationV4(
        declaration.contract_id,
        declaration.operation_id,
        dict(declaration.payload),
        declaration.interval_ms,
    )
    declaration_digest = captured.digest
    registration_id = "wake." + canonical_digest(
        {
            "profile_id": identity["profile_id"],
            "owner": owner_principal_id,
            "target": target_principal_id,
            "declaration": declaration_digest,
        }
    ).removeprefix("sha256:")
    pinned_identity = {
        **dict(identity),
        "owner_principal_id": owner_principal_id,
        "target_principal_id": target_principal_id,
        "declaration_digest": declaration_digest,
    }

    def frame(occurrence: str) -> InvocationFrame:
        return InvocationFrame(
            captured.contract_id,
            None,
            captured.operation_id,
            dict(captured.payload),
            timeout_ms=30000,
            idempotency_key=f"{registration_id}:{occurrence}",
        )

    def check() -> None:
        assert_current()
        if captured.digest != declaration_digest:
            raise PermissionError("wake declaration drifted")
        with context_scope("admission") as context:
            if context.caller_principal.value != owner_principal_id:
                raise PermissionError("wake caller is not the selected owner")
            requested_effect_scope = effect_scope(context)
            requested_scope = AuthorityScope.from_dict(requested_effect_scope)
            # SESSION/ONE_SHOT authority from an interactive call is never
            # promoted into recurring authority.
            recurring = [
                grant
                for grant in authority_store.list_grants()
                if grant.caller.principal_id == owner_principal_id
                and grant.target.principal_id == target_principal_id
                and grant.profile_id == context.profile_id
                and grant.activation_id == context.activation_id
                and grant.profile_authority_digest == context.profile_authority_digest
                and grant.security_epoch == context.security_epoch
                and not grant.revoked
                and not authority_store.is_revoked("grant", grant.grant_id)
                and grant.lifetime
                in {
                    GrantLifetime.PERSISTENT_PROFILE,
                    GrantLifetime.WORKFLOW_REVISION,
                    GrantLifetime.POLICY_EPHEMERAL,
                }
                and grant.issued_at <= wall_clock()
                and (grant.expires_at is None or wall_clock() < grant.expires_at)
                and requested_scope.is_subset_of(grant.scope)
            ]
            if not recurring:
                raise PermissionError("exact recurring wake Grant is unavailable")
            prepared = broker.prepare(frame("admission"), context)
            if prepared.binding.principal_ref.value != target_principal_id:
                raise PermissionError("wake selected target changed")
            authority.check_static_path(
                StaticAuthorityQuery(
                    context,
                    OpaqueAuthorityRef(target_principal_id),
                    prepared.request_digest,
                    requested_effect_scope,
                )
            )

    def invoke(
        occurrence: str, fence: Callable[[], None], cancellation: threading.Event
    ) -> Mapping[str, Any]:
        with context_scope(occurrence) as context:
            fence()
            if context.caller_principal.value != owner_principal_id:
                raise PermissionError("wake caller binding changed")

            def before_dispatch() -> None:
                assert_current()
                fence()

            # This is the actual Broker, with its own canonical digest, final
            # evidence check, current Grant, request lease and authoritative audit.
            return broker.invoke(
                frame(occurrence),
                context,
                effect_scope=effect_scope(context),
                parent_cancellation=cancellation,
                before_dispatch=before_dispatch,
            )

    driver = CapturedWakeDriverV4(
        state_path=state_path,
        declaration=captured,
        registration=TriggerRegistration(
            registration_id,
            captured.contract_id,
            captured.operation_id,
            OpaqueAuthorityRef(target_principal_id),
            str(identity["activation_digest"]),
            int(identity["security_epoch"]),
        ),
        binding_identity=pinned_identity,
        assert_authorized=check,
        invoke_fresh=invoke,
        current_epoch=lambda: authority_store.security_epoch,
        adapter_factory=adapter_factory,
        wall_clock=wall_clock,
        monotonic_clock=monotonic_clock,
    )
    port.bind(driver, owner_principal_id)
    return driver
