"""Private, invocation-bound transport for verified Host owner evidence.

This transports observations, never grants. Registration belongs to verified
Host factory capture; callers must derive Binding fields from captured Broker
identities, never request JSON. A receiving owner still pins acceptable issuers.
"""
from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from threading import RLock
from typing import Any, Protocol

from tobkiri_protocol.canonical import canonical_json, strict_loads


class EvidenceDenied(PermissionError):
    """Evidence is missing, belongs to another invocation, or has expired."""


@dataclass(frozen=True)
class EvidenceBinding:
    """Host-created exact one-hop dispatch identity; no authority is implied."""

    issuer_principal: str
    target_principal: str
    contract_id: str
    contract_version: str
    operation_id: str
    payload_digest: str
    idempotency_key: str
    profile_id: str
    plan_digest: str
    activation_id: str
    activation_digest: str
    security_epoch: int
    presentation_owner_principal: str
    presentation_owner_session: str


class OwnerEvidence(Protocol):
    """An owner-defined bounded read interface, with no generic state access."""

    def read(self, selector: str) -> Mapping[str, Any]:
        """Read only evidence permitted by the authenticated owner reference."""


EvidenceResolver = Callable[[str, EvidenceBinding], OwnerEvidence]


class BoundInvocationEvidence:
    """A live view tied to the exact receiving envelope object."""

    def __init__(
        self, scope: _EvidenceScope, receiver: object, guard: Callable[[], None],
    ) -> None:
        self._scope, self._receiver, self._guard = scope, receiver, guard

    @property
    def issuer_principal(self) -> str:
        self._scope.assert_live(self._receiver)
        self._guard()
        return self._scope.binding.issuer_principal

    def read(self, selector: str) -> Mapping[str, Any]:
        """Fence both sides of a read against timeout, cancellation and close."""
        if type(selector) is not str or not selector or len(selector) > 256:
            raise EvidenceDenied("evidence selector is invalid")
        self._scope.assert_live(self._receiver)
        self._guard()
        value = self._scope.source.read(selector)
        if not isinstance(value, Mapping):
            raise EvidenceDenied("owner evidence response is invalid")
        snapshot = strict_loads(canonical_json(dict(value)))
        self._guard()
        self._scope.assert_live(self._receiver)
        return snapshot


class _EvidenceScope:
    def __init__(
        self, binding: EvidenceBinding, kind: str, source: OwnerEvidence,
        issuer_guard: Callable[[], None],
    ) -> None:
        self.binding, self.kind, self.source = binding, kind, source
        self._issuer_guard = issuer_guard
        self._lock = RLock()
        self._live = True
        self._receiver: object | None = None

    def receive(
        self, binding: EvidenceBinding, receiver: object, guard: Callable[[], None],
    ) -> BoundInvocationEvidence:
        if receiver is None:
            raise EvidenceDenied("receiving envelope is missing")
        guard()
        with self._lock:
            if not self._live or binding != self.binding:
                raise EvidenceDenied("evidence dispatch binding does not match")
            if self._receiver is not None and self._receiver is not receiver:
                raise EvidenceDenied("evidence is already bound to another invocation")
            self._receiver = receiver
        return BoundInvocationEvidence(self, receiver, guard)

    def assert_live(self, receiver: object) -> None:
        self._issuer_guard()
        with self._lock:
            if not self._live or self._receiver is not receiver:
                raise EvidenceDenied("invocation evidence is no longer live")

    def close(self) -> None:
        with self._lock:
            self._live = False


_current_scope: ContextVar[_EvidenceScope | None] = ContextVar(
    "tobkiri_private_invocation_evidence", default=None,
)


class InvocationEvidenceRegistry:
    """Capture-local evidence issuers, revoked when their Host capture closes."""

    def __init__(self) -> None:
        self._lock = RLock()
        self._closed = False
        self._issuers: dict[tuple[str, str], EvidenceResolver] = {}
        self._scopes: set[_EvidenceScope] = set()

    def register(self, issuer_principal: str, kind: str, resolver: EvidenceResolver) -> None:
        """Register only after verifying the factory's exact contribution set."""
        self.register_many(((issuer_principal, kind, resolver),))

    def register_many(self, declarations: tuple[tuple[str, str, EvidenceResolver], ...]) -> None:
        """Validate a complete captured issuer set before publishing any entry."""
        pending: dict[tuple[str, str], EvidenceResolver] = {}
        for issuer_principal, kind, resolver in declarations:
            if any(type(v) is not str or not v or len(v) > 512
                   for v in (issuer_principal, kind)) or not callable(resolver):
                raise EvidenceDenied("evidence issuer declaration is invalid")
            key = (issuer_principal, kind)
            if key in pending:
                raise EvidenceDenied("evidence issuer is duplicated")
            pending[key] = resolver
        with self._lock:
            if self._closed or self._issuers.keys() & pending.keys():
                raise EvidenceDenied("evidence issuer is closed or duplicated")
            self._issuers.update(pending)

    @contextmanager
    def dispatch(
        self, *, kind: str, reference: str, binding: EvidenceBinding,
        guard: Callable[[], None],
    ) -> Iterator[None]:
        """Bind a fresh, nonserializable scope around one real Broker dispatch."""
        if type(reference) is not str or not reference or len(reference) > 1024:
            raise EvidenceDenied("evidence reference is invalid")
        guard()
        with self._lock:
            resolver = self._issuers.get((binding.issuer_principal, kind))
            if self._closed or resolver is None:
                raise EvidenceDenied("evidence issuer is unavailable")
        source = resolver(reference, binding)
        guard()
        scope = _EvidenceScope(binding, kind, source, guard)
        with self._lock:
            if self._closed:
                raise EvidenceDenied("evidence registry closed during resolution")
            self._scopes.add(scope)
        token = _current_scope.set(scope)
        try:
            yield
        finally:
            # A copied worker context may survive return/timeout. Reset alone
            # is not revocation: invalidate the shared object first.
            scope.close()
            _current_scope.reset(token)
            with self._lock:
                self._scopes.discard(scope)

    def receive(
        self, *, kind: str, binding: EvidenceBinding, receiver: object,
        guard: Callable[[], None],
    ) -> BoundInvocationEvidence:
        """Reject inherited scopes on descendants or another capture."""
        scope = _current_scope.get()
        with self._lock:
            if self._closed or scope is None or scope not in self._scopes or scope.kind != kind:
                raise EvidenceDenied("invocation evidence is unavailable")
        return scope.receive(binding, receiver, guard)

    def close(self) -> None:
        """Revoke all live scopes before closing their owner stores."""
        with self._lock:
            self._closed = True
            for scope in self._scopes:
                scope.close()
            self._issuers.clear()
