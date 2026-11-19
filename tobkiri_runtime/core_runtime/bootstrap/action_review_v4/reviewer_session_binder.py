"""Bind independent review sessions through the actual captured Host Broker."""

from __future__ import annotations

import json
import threading
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Callable, Mapping

from core_runtime.authority.v4 import AuthorityDenied, LeaseState, authority_digest
from core_runtime.invocation_scope_v4 import CapturedInvocationScopeV4
from tobkiri_host.broker import RequestEnvelope, _thaw_payload
from tobkiri_host.models import InvocationFrame, RequestContext
from tobkiri_host.runtime import V4DispatchSession
from tobkiri_protocol.data_codec import digest_payload
from .canonical_reviewer_port import GENERATE_TARGET, GenerateDispatch, ReviewerGenerateLease
from .reviewer_capture_factory import CanonicalReviewerCaptureFactory, QUOTE_TARGET


@dataclass(frozen=True)
class ReviewerDispatchIdentity:
    """Bind one executing reviewer Gateway request to its saved invocation."""

    context: RequestContext
    target_pair: tuple[str, str]
    payload_digest: str
    request_digest: str
    resolved_session_id: str
    saved_parent: CapturedInvocationScopeV4

    def matches_gateway(self, envelope: RequestEnvelope) -> bool:
        return (
            envelope.context is self.context
            and envelope.context.caller_session_id == self.resolved_session_id
            and (envelope.contract_id, envelope.operation_id) == self.target_pair
            and envelope.request_digest == self.request_digest
            and digest_payload(_thaw_payload(envelope.payload)) == self.payload_digest
        )


class ReviewerDispatchRegistry:
    """Share live prepared reviewer identities with nested Broker workers."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._active: dict[str, ReviewerDispatchIdentity] = {}

    def lookup(self, resolved_session_id: str) -> ReviewerDispatchIdentity | None:
        with self._lock:
            return self._active.get(resolved_session_id)

    @contextmanager
    def retain(self, identity: ReviewerDispatchIdentity) -> Iterator[None]:
        resolved = identity.resolved_session_id
        if identity.context.caller_session_id != resolved or identity.target_pair not in (
            QUOTE_TARGET, GENERATE_TARGET,
        ):
            raise AuthorityDenied("reviewer dispatch identity changed")
        with self._lock:
            if resolved in self._active:
                raise AuthorityDenied("reviewer dispatch is already active")
            self._active[resolved] = identity
        try:
            yield
        finally:
            with self._lock:
                self._active.pop(resolved)


@dataclass(frozen=True)
class _PreparedReviewerDispatch:
    dispatch: V4DispatchSession
    registry: ReviewerDispatchRegistry
    identity: ReviewerDispatchIdentity
    session_id: str
    input_digest: str

    def invoke(
        self, contract_id: str, operation_id: str, payload: Mapping[str, Any], **options: Any,
    ) -> Mapping[str, Any]:
        arguments = dict(payload)
        if (
            (contract_id, operation_id) != self.identity.target_pair
            or arguments.pop("_session_id", None) != self.session_id
            or digest_payload(arguments) != self.input_digest
        ):
            raise AuthorityDenied("reviewer prepared dispatch changed")
        with self.registry.retain(self.identity):
            return self.dispatch.invoke(contract_id, operation_id, payload, **options)


def build_reviewer_capture(
    invocation: Any,
    saved_context: Any,
    *,
    broker: Any,
    authority: Any,
    authority_store: Any,
    catalog: Any,
    profile_id: str,
    policy_caller_binding: Any,
    generate_binding: Any,
    signed_generate_edge: Any,
    bind_nested_session: Callable[..., str],
    release_nested_session: Callable[..., None],
    context_for: Callable[..., Any],
    effect_scope_for: Callable[..., Any],
    capture_invocation_scope: Callable[..., Any],
    nested_cancellation_proof_for: Callable[..., Any],
    assert_current_capture: Callable[[], None],
    local_configuration_revision: Callable[[], tuple[str, int]],
    local_route_is_current: Callable[[Mapping[str, Any]], bool],
    capture_independent_authority: Callable[[Any, Any], Mapping[str, Any]],
    dispatch_capture: Mapping[str, Any],
    parent_deadline_monotonic: float,
    parent_cancellation: threading.Event,
    reviewer_dispatches: ReviewerDispatchRegistry | None = None,
    evidence_validity_seconds: int = 120,
) -> Any:
    """Prepare native reviewer facts; native approval remains the caller's job.

    Every callback is a private Host composition port. The signed edge resolver
    must capture actual independent Grant/provider rows. Configuration refresh
    performs Broker I/O before transactions; local guards perform no Broker I/O.
    No authority from the original tool operation is copied into a review lease.
    """
    del saved_context, catalog  # Identity comes from captured typed bindings.
    caller = policy_caller_binding.principal_ref.value
    target = generate_binding.principal_ref.value
    if (
        signed_generate_edge.authority_mode != "profile_grant"
        or signed_generate_edge.caller.principal_id != caller
        or signed_generate_edge.target.principal_id != target
        or tuple(signed_generate_edge.binding_key[-2:]) != GENERATE_TARGET
    ):
        raise AuthorityDenied("independent signed reviewer generate edge missing")
    if not isinstance(parent_cancellation, threading.Event):
        raise AuthorityDenied("reviewer parent cancellation capture missing")
    owner = (invocation.presentation_owner_principal_id, invocation.presentation_owner_session_id)
    parent = capture_invocation_scope(invocation.envelope)

    def guard() -> None:
        assert_current_capture()
        parent.assert_current()
        if parent_cancellation.is_set() or time.monotonic() >= parent_deadline_monotonic:
            raise AuthorityDenied("independent reviewer parent expired or cancelled")

    def session() -> tuple[str, str]:
        guard()
        name = "session.approval-review." + str(uuid.uuid4())
        resolved = bind_nested_session(name, caller, owner, parent_invocation=parent)
        if not isinstance(resolved, str) or not resolved:
            raise AuthorityDenied("independent reviewer session unavailable")
        return name, resolved

    # Resolve actual stable signed-edge authority before native selection. The
    # ephemeral session is not itself part of the native approved stable facts.
    capture_session, resolved = session()
    try:
        captured_context = context_for(*GENERATE_TARGET, capture_session)
        independent = dict(capture_independent_authority(captured_context, signed_generate_edge))
        if (
            independent.get("caller_principal_id") != caller
            or independent.get("target_principal_id") != target
            or independent.get("profile_id") != profile_id
        ):
            raise AuthorityDenied("independent reviewer authority capture rebound")
    finally:
        release_nested_session(capture_session, resolved)

    def prepare(
        target_pair: tuple[str, str],
        payload: Mapping[str, Any],
        boundary_digest: str,
        payload_digest: str,
    ) -> ReviewerGenerateLease:
        name, resolved_session = session()
        try:
            context = context_for(*target_pair, name)
            scope = effect_scope_for(*target_pair, payload, context)
            arguments = dict(payload)
            input_digest = (
                digest_payload(arguments)
                if reviewer_dispatches is not None and target_pair in (QUOTE_TARGET, GENERATE_TARGET)
                else None
            )
            prepared = broker.prepare(
                InvocationFrame(
                    contract_id=target_pair[0],
                    version_range=None,
                    operation_id=target_pair[1],
                    payload=arguments,
                ),
                context,
            )
            # The exact same context (including request ID) and scope must be
            # used by invoke; production context_for creates fresh IDs per call.
            dispatch = V4DispatchSession(
                broker=broker,
                context_for=lambda contract, operation, session_id: context,
                effect_scope_for=lambda contract, operation, arguments, ctx: scope,
                providers={},
                profile_id=profile_id,
                plan_digest=dispatch_capture["plan_digest"],
                profile_revision=dispatch_capture["profile_revision"],
                activation_id=dispatch_capture["activation_id"],
                security_epoch=dispatch_capture["security_epoch"],
                authority_control=authority,
                current_capture_check=guard,
            )
            root_expires_at = time.time() + max(0, parent_deadline_monotonic - time.monotonic())
            reserved: list[Any] = []

            def before() -> None:
                guard()
                if reserved:
                    raise AuthorityDenied("reviewer request reservation reused")
                reserved.append(
                    authority.reserve_effect(context, prepared.binding, prepared.request_digest)
                )

            def proof() -> Mapping[str, Any]:
                guard()
                if len(reserved) != 1:
                    raise AuthorityDenied("reviewer committed reservation missing")
                stored = authority_store.get_lease(reserved[0].value)
                if stored is None or stored[1] is not LeaseState.COMMITTED:
                    raise AuthorityDenied("reviewer independent lease uncommitted")
                lease = stored[0]
                if (
                    lease.request_digest != prepared.request_digest
                    or lease.caller.principal_id != caller
                    or lease.profile_id != profile_id
                ):
                    raise AuthorityDenied("reviewer committed request rebound")
                return {
                    "lease_id": lease.lease_id,
                    "state": stored[1].value,
                    "request_digest": lease.request_digest,
                    "payload_digest": payload_digest,
                    "boundary_digest": boundary_digest,
                    "session_id": name,
                    "expires_at": lease.expires_at,
                    "root_expires_at": root_expires_at,
                    "reviewer_prepared_snapshot": review_transport_snapshot(prepared),
                }

            cancellation_proof = nested_cancellation_proof_for(invocation.envelope, *owner)
            if cancellation_proof is None:
                raise AuthorityDenied("reviewer authenticated parent cancellation unavailable")
            reviewer_dispatch: GenerateDispatch = dispatch
            if reviewer_dispatches is not None and input_digest is not None:
                identity = ReviewerDispatchIdentity(
                    context, target_pair, digest_payload(_thaw_payload(prepared.normalized_payload)),
                    prepared.request_digest, resolved_session, parent,
                )
                reviewer_dispatch = _PreparedReviewerDispatch(
                    dispatch, reviewer_dispatches, identity, name, input_digest,
                )
            return ReviewerGenerateLease(
                reviewer_dispatch,
                name,
                caller,
                boundary_digest,
                payload_digest,
                min(parent_deadline_monotonic, prepared.deadline_monotonic),
                parent_cancellation,
                cancellation_proof,
                guard,
                before,
                guard,
                proof,
                lambda: release_nested_session(name, resolved_session),
            )
        except BaseException:
            release_nested_session(name, resolved_session)
            raise

    committed_reads: dict[tuple[str, str], tuple[Mapping[str, Any], Mapping[str, Any]]] = {}

    class IndependentReadDispatch:
        """Perform and retain privately authenticated configuration read results."""

        def invoke(
            self, contract: str, operation: str, payload: Mapping[str, Any]
        ) -> Mapping[str, Any]:
            arguments = dict(payload)
            arguments.pop("_session_id", None)
            pair = (contract, operation)
            lease = prepare(pair, arguments, "configuration-read", authority_digest(arguments))
            try:
                result = lease.dispatch.invoke(
                    contract,
                    operation,
                    {**arguments, "_session_id": lease.session_id},
                    parent_deadline_monotonic=lease.parent_deadline_monotonic,
                    parent_cancellation=lease.parent_cancellation,
                    parent_cancellation_proof=lease.parent_cancellation_proof,
                    before_dispatch=lease.before_dispatch,
                    execution_guard=lease.execution_guard,
                )
                committed_reads[pair] = (result, lease.committed_transport_proof())
                return result
            finally:
                lease.release()

    def authenticate_read(pair: tuple[str, str], result: Mapping[str, Any]) -> None:
        retained = committed_reads.pop(pair, None)
        if retained is None or retained[0] is not result:
            raise AuthorityDenied("independent reviewer configuration result unauthenticated")
        # Only this private dispatch can install a proof after native commit.
        guard()

    return CanonicalReviewerCaptureFactory(
        IndependentReadDispatch(),
        session_id="host-owned-independent-read",
        profile_id=profile_id,
        caller_principal_id=caller,
        independent_capture=independent,
        bind_generate=lambda payload, boundary, digest: prepare(
            GENERATE_TARGET, payload, boundary, digest
        ),
        assert_current=guard,
        authenticate_read=authenticate_read,
        local_configuration_revision=local_configuration_revision,
        local_route_is_current=local_route_is_current,
        evidence_validity_seconds=evidence_validity_seconds,
    ).capture()


def review_transport_snapshot(prepared: Any) -> Mapping[str, Any]:
    """Retain the exact normalized Broker request, including finite price decimals.

    This is private transport provenance, not a PreparedInvocationSnapshot used
    for resumed execution. That public snapshot schema only permits integers;
    gateway price decimals must remain exactly as the Broker normalized them.
    """

    def plain(value: Any) -> Any:
        if isinstance(value, Mapping):
            return {key: plain(item) for key, item in value.items()}
        if isinstance(value, (list, tuple)):
            return [plain(item) for item in value]
        return value

    result = {
        "contract_id": prepared.binding.operation.contract_id,
        "operation_id": prepared.binding.operation.operation_id,
        "contract_version": prepared.binding.operation.contract_version,
        "normalized_payload": plain(prepared.normalized_payload),
        "request_digest": prepared.request_digest,
        "binding_fingerprint": plain(prepared.binding_fingerprint),
        "context_fingerprint": plain(prepared.context_fingerprint),
    }
    return json.loads(json.dumps(result, allow_nan=False))
