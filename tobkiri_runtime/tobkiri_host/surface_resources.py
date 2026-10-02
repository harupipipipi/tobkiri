"""Host-owned, captured Surface resource tickets; provider binding is optional.

The public action accepts no paths, handles or authority hints. A trusted captured
Provider supplies selection, exchange, rebinding and revocation callbacks. Until
that Provider is registered, acquisition is unavailable; this module implements
no native picker. Portable selections stay private. Only ``consume`` returns a
fresh invocation-local reference to trusted Host code, after server scope checks.
Normal selected v4 action admission and approval happen before this boundary.
"""

from __future__ import annotations

from dataclasses import dataclass, fields
import math
import re
import secrets
from threading import RLock
import time
from typing import Callable, Literal, Mapping, Protocol, TypeVar, cast

from .errors import HostCoreError
from .models import OpaqueAuthorityRef, RequestContext

SURFACE_RESOURCE_VERSION = "tobkiri.ui.surface-resource.v1"
ResourceKind = Literal["file", "image", "audio"]
_KINDS = frozenset({"file", "image", "audio"})
_TOKEN = re.compile(r"[A-Za-z0-9_-]{32,128}\Z")
_DIGEST = re.compile(r"sha256:[a-f0-9]{64}\Z")
_Result = TypeVar("_Result")


class SurfaceResourceUnavailable(HostCoreError):
    """The captured resource Provider or its rebinding hook is unavailable."""

    code = "surface_resource_unavailable"


class SurfaceResourceRejected(HostCoreError):
    """Resource input, capture, ticket, expiry or capacity failed closed."""

    code = "surface_resource_rejected"


class SurfaceResourceProviderError(HostCoreError):
    """A Provider callback failed without exposing its private exception text."""

    code = "surface_resource_provider_failed"


@dataclass(frozen=True)
class SurfaceResourceScope:
    """Exact server-captured presentation owner and intended consumer identity."""

    profile_id: str
    profile_revision: str
    activation_id: str
    plan_hash: str
    catalog_hash: str
    security_epoch: int
    presentation_owner_principal_id: str
    presentation_owner_session_id: str
    view_descriptor_hash: str
    renderer_id: str
    renderer_api_version: str
    renderer_digest: str
    renderer_descriptor_hash: str
    renderer_expires_at_ms: int
    consumer_contract_id: str
    consumer_contract_version: str
    consumer_operation_id: str
    consumer_principal_id: str
    consumer_provider_id: str
    consumer_function_id: str
    consumer_artifact_digest: str
    consumer_schema_digest: str

    def __post_init__(self) -> None:
        """Reject incomplete capture data; none of these fields are client input."""
        for field in fields(self):
            value = getattr(self, field.name)
            if field.name == "security_epoch":
                if type(value) is not int or value <= 0:
                    raise ValueError("resource security epoch is invalid")
            elif field.name == "renderer_expires_at_ms":
                if type(value) is not int or not 0 < value <= 2**53 - 1:
                    raise ValueError("resource renderer expiry is invalid")
            elif not _text(value, 512):
                raise ValueError("resource capture is incomplete")
            elif (
                field.name.endswith(("_hash", "_digest"))
                or field.name == "profile_revision"
            ):
                if _DIGEST.fullmatch(value) is None:
                    raise ValueError("resource capture digest is invalid")
        if self.renderer_api_version != "1.0.0":
            raise ValueError("resource renderer API is unavailable")


@dataclass(frozen=True)
class PrivateSurfaceSelection:
    """Portable Provider-owned reference and an inert public display label."""

    reference: object
    display_name: str


@dataclass(frozen=True)
class CapturedSurfaceResourceProvider:
    """Trusted adapter bound by the selected Provider's verified capture.

    ``acquire`` and ``exchange`` return portable private selections, never a
    request-bound ResourceHandle. ``consume`` rebinds the selection to its fresh
    authenticated RequestContext. ``revoke`` must release both selection and
    invocation-local references, including results returned after cancellation.
    Revocation must be idempotent. A callback that raises must clean up resources
    it has not returned. Optional ``cancel`` stops pending selection callbacks.
    """

    acquire: Callable[[ResourceKind, RequestContext], PrivateSurfaceSelection]
    exchange: Callable[[object, RequestContext], PrivateSurfaceSelection]
    revoke: Callable[[object], None]
    consume: Callable[[object, RequestContext], object] | None = None
    cancel: Callable[[], None] | None = None


@dataclass
class _Ticket:
    scope: SurfaceResourceScope
    kind: ResourceKind
    stage: Literal["selected", "exchanged", "consumed"]
    selection: PrivateSurfaceSelection
    expires_monotonic: float
    expires_at_ms: int


class SurfaceResourcePort:
    """Bounded one-use tickets owned by one captured Host Provider lifetime."""

    def __init__(
        self,
        provider: CapturedSurfaceResourceProvider | None = None,
        *,
        max_entries: int = 128,
        clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], float] = time.time,
    ) -> None:
        """Capture the explicit trusted adapter; there is no implicit fallback."""
        if type(max_entries) is not int or not 1 <= max_entries <= 4096:
            raise ValueError("resource entry quota is invalid")
        if provider is not None and (
            not isinstance(provider, CapturedSurfaceResourceProvider)
            or not all(
                callable(callback)
                for callback in (
                    provider.acquire,
                    provider.exchange,
                    provider.revoke,
                )
            )
            or (provider.consume is not None and not callable(provider.consume))
            or (provider.cancel is not None and not callable(provider.cancel))
        ):
            raise ValueError("resource Provider adapter is invalid")
        self._provider = provider
        self._max_entries = max_entries
        self._clock = clock
        self._wall_clock = wall_clock
        self._lock = RLock()
        self._closed = False
        self._tickets: dict[str, _Ticket] = {}
        self._inflight: set[str] = set()
        self._cleanup_pending: dict[int, object] = {}

    def invoke(
        self,
        request: Mapping[str, object],
        *,
        scope: SurfaceResourceScope,
        context: RequestContext,
        assert_current: Callable[[], None],
        deadline_monotonic: float,
        ttl_seconds: float = 30,
    ) -> dict[str, object]:
        """Run an exact public acquire/exchange action after v4 admission."""
        operation, kind, selection_id = _request(request)
        self._guard(scope, context, assert_current, deadline_monotonic)
        self._purge()
        provider = self._require_provider()
        with self._lock:
            self._require_open()
            if operation == "exchange":
                if selection_id is None:
                    raise SurfaceResourceRejected("resource selection is missing")
                ticket = self._lookup(selection_id, scope, kind, "selected")
                del self._tickets[selection_id]
            else:
                if self._size() >= self._max_entries:
                    raise SurfaceResourceRejected("resource entry quota exceeded")
                renderer_deadline = self._clock() + (
                    scope.renderer_expires_at_ms / 1000 - self._wall_clock()
                )
                expires, expires_ms = self._expiry(
                    ttl_seconds, min(deadline_monotonic, renderer_deadline)
                )
                ticket = _Ticket(
                    scope,
                    kind,
                    "selected",
                    PrivateSurfaceSelection(None, ""),
                    expires,
                    expires_ms,
                )
            pending = self._reserve()
        result: PrivateSurfaceSelection | None = None
        published = False
        selection_released = False
        try:
            self._guard(scope, context, assert_current, deadline_monotonic)
            self._require_live(ticket)
            result = _callback(
                lambda: (
                    provider.acquire(kind, context)
                    if operation == "acquire"
                    else provider.exchange(ticket.selection.reference, context)
                )
            )
            _selection(result)
            if (
                operation == "exchange"
                and result.reference is ticket.selection.reference
            ):
                raise SurfaceResourceProviderError(
                    "resource exchange did not replace selection"
                )
            if operation == "exchange":
                self._revoke([ticket.selection.reference])
                selection_released = True
            self._guard(scope, context, assert_current, deadline_monotonic)
            with self._lock:
                self._require_open()
                self._require_live(ticket)
                token = self._new_token()
                ticket.selection = result
                ticket.stage = "selected" if operation == "acquire" else "exchanged"
                if deadline_monotonic < ticket.expires_monotonic:
                    ticket.expires_monotonic = deadline_monotonic
                    ticket.expires_at_ms = min(
                        ticket.expires_at_ms,
                        int(
                            (self._wall_clock() + deadline_monotonic - self._clock())
                            * 1000
                        ),
                    )
                self._tickets[token] = ticket
                published = True
                return _wire(token, ticket)
        except HostCoreError:
            raise
        except Exception:
            raise SurfaceResourceProviderError(
                "resource Provider action failed"
            ) from None
        finally:
            resources: list[object] = []
            if not published and isinstance(result, PrivateSurfaceSelection):
                resources.append(result.reference)
            if operation == "exchange" and not published and not selection_released:
                resources.append(ticket.selection.reference)
            try:
                self._revoke(resources)
            finally:
                with self._lock:
                    self._inflight.discard(pending)

    def consume(
        self,
        selection_id: str,
        *,
        kind: ResourceKind,
        scope: SurfaceResourceScope,
        context: RequestContext,
        assert_current: Callable[[], None],
        deadline_monotonic: float,
    ) -> object:
        """Consume once in the exact Logic scope and rebind to this fresh request.

        The returned reference is Host-only and belongs to this invocation. It
        remains tracked for expiry/close cleanup and must never enter public JSON.
        The trusted caller must derive ``scope`` from its authenticated envelope.
        """
        self._guard(scope, context, assert_current, deadline_monotonic)
        self._purge()
        provider = self._require_provider()
        rebind = provider.consume
        if rebind is None:
            raise SurfaceResourceUnavailable("resource rebinding is unavailable")
        with self._lock:
            self._require_open()
            ticket = self._lookup(selection_id, scope, kind, "exchanged")
            del self._tickets[selection_id]
            pending = self._reserve()
        result: object = None
        published = False
        selection_released = False
        try:
            self._guard(scope, context, assert_current, deadline_monotonic)
            self._require_live(ticket)
            result = _callback(lambda: rebind(ticket.selection.reference, context))
            if result is None or result is ticket.selection.reference:
                raise SurfaceResourceProviderError(
                    "resource rebinding returned no fresh reference"
                )
            self._revoke([ticket.selection.reference])
            selection_released = True
            self._guard(scope, context, assert_current, deadline_monotonic)
            with self._lock:
                self._require_open()
                self._require_live(ticket)
                ticket.selection = PrivateSurfaceSelection(
                    result, ticket.selection.display_name
                )
                ticket.stage = "consumed"
                ticket.expires_monotonic = min(
                    ticket.expires_monotonic, deadline_monotonic
                )
                self._tickets[self._new_token()] = ticket
                published = True
                return result
        except HostCoreError:
            raise
        except Exception:
            raise SurfaceResourceProviderError(
                "resource Provider rebinding failed"
            ) from None
        finally:
            resources = [] if published else [result]
            if not published and not selection_released:
                resources.append(ticket.selection.reference)
            try:
                self._revoke(resources)
            finally:
                with self._lock:
                    self._inflight.discard(pending)

    def close(self) -> None:
        """Invalidate tickets first, then revoke private and late callback results.

        Cleanup failure keeps references quarantined for a subsequent close retry.
        In-flight callbacks cannot publish after this fence and revoke upon return.
        """
        with self._lock:
            self._closed = True
            resources = [
                ticket.selection.reference for ticket in self._tickets.values()
            ]
            resources.extend(self._cleanup_pending.values())
            self._tickets.clear()
        try:
            if self._provider is not None and self._provider.cancel is not None:
                self._provider.cancel()
        except Exception:
            raise SurfaceResourceProviderError(
                "resource Provider cancellation failed"
            ) from None
        finally:
            self._revoke(resources)

    def _guard(
        self,
        scope: SurfaceResourceScope,
        context: RequestContext,
        assert_current: Callable[[], None],
        deadline: float,
    ) -> None:
        if not isinstance(scope, SurfaceResourceScope) or not isinstance(
            context, RequestContext
        ):
            raise SurfaceResourceRejected("resource server capture is invalid")
        if (
            context.profile_id != scope.profile_id
            or context.profile_revision != scope.profile_revision
            or context.activation_id != scope.activation_id
            or context.plan_digest != scope.plan_hash
            or context.security_epoch != scope.security_epoch
        ):
            raise SurfaceResourceRejected("resource invocation capture changed")
        if type(deadline) not in (int, float) or not math.isfinite(deadline):
            raise SurfaceResourceRejected("resource deadline is invalid")
        self._require_provider()
        with self._lock:
            self._require_open()
        try:
            assert_current()
        except Exception:
            raise SurfaceResourceRejected(
                "resource invocation is no longer current"
            ) from None
        if (
            self._clock() >= deadline
            or self._wall_clock() * 1000 >= scope.renderer_expires_at_ms
        ):
            raise SurfaceResourceRejected("resource invocation expired")

    def _require_provider(self) -> CapturedSurfaceResourceProvider:
        if self._provider is None:
            raise SurfaceResourceUnavailable("resource acquisition is unavailable")
        return self._provider

    def _require_open(self) -> None:
        if self._closed:
            raise SurfaceResourceUnavailable("resource capture is closed")

    def _require_live(self, ticket: _Ticket) -> None:
        if self._clock() >= ticket.expires_monotonic:
            raise SurfaceResourceRejected("resource selection is unavailable")

    def _expiry(self, ttl: float, deadline: float) -> tuple[float, int]:
        if (
            type(ttl) not in (int, float)
            or not math.isfinite(ttl)
            or not 1 <= ttl <= 300
        ):
            raise SurfaceResourceRejected("resource lifetime is invalid")
        now = self._clock()
        lifetime = min(ttl, deadline - now)
        if lifetime < 1:
            raise SurfaceResourceRejected("resource lifetime is too short")
        expires_ms = int((self._wall_clock() + lifetime) * 1000)
        if expires_ms <= 0:
            raise SurfaceResourceRejected("resource wall clock is invalid")
        return now + lifetime, expires_ms

    def _lookup(
        self,
        token: str | None,
        scope: SurfaceResourceScope,
        kind: ResourceKind,
        stage: str,
    ) -> _Ticket:
        ticket = (
            self._tickets.get(token)
            if isinstance(token, str) and _TOKEN.fullmatch(token)
            else None
        )
        if (
            ticket is None
            or ticket.scope != scope
            or ticket.kind != kind
            or ticket.stage != stage
        ):
            raise SurfaceResourceRejected("resource selection is unavailable")
        self._require_live(ticket)
        return ticket

    def _size(self) -> int:
        return len(self._tickets) + len(self._inflight) + len(self._cleanup_pending)

    def _reserve(self) -> str:
        token = self._new_token()
        self._inflight.add(token)
        return token

    def _new_token(self) -> str:
        while True:
            token = secrets.token_urlsafe(32)
            if token not in self._tickets and token not in self._inflight:
                return token

    def _purge(self) -> None:
        with self._lock:
            expired = [
                key
                for key, ticket in self._tickets.items()
                if self._clock() >= ticket.expires_monotonic
            ]
            resources = [self._tickets.pop(key).selection.reference for key in expired]
        self._revoke(resources)

    def _revoke(self, resources: list[object]) -> None:
        provider = self._provider
        if provider is None:
            return
        unique = {
            id(resource): resource for resource in resources if resource is not None
        }
        failed = False
        for identity, resource in unique.items():
            try:
                provider.revoke(resource)
            except Exception:
                with self._lock:
                    self._cleanup_pending[identity] = resource
                failed = True
            else:
                with self._lock:
                    self._cleanup_pending.pop(identity, None)
        if failed:
            raise SurfaceResourceProviderError(
                "resource Provider cleanup failed"
            ) from None


class SurfaceResourceEnvelope(Protocol):
    """Authenticated envelope fields required by the captured action adapter."""

    @property
    def context(self) -> RequestContext: ...

    @property
    def deadline_monotonic(self) -> float: ...

    @property
    def contract_id(self) -> str: ...

    @property
    def contract_version(self) -> str: ...

    @property
    def target_principal(self) -> OpaqueAuthorityRef: ...

    @property
    def operation_id(self) -> str: ...


class SurfaceResourceInvocation(Protocol):
    """Structural subset of HostProviderInvocationContextV4; never client input."""

    @property
    def envelope(self) -> SurfaceResourceEnvelope: ...

    @property
    def presentation_owner_principal_id(self) -> str: ...

    @property
    def presentation_owner_session_id(self) -> str: ...

    def assert_current(self) -> None:
        """Recheck the Host invocation capture and cancellation/deadline fences."""


@dataclass(frozen=True)
class SurfaceResourceActionAdapter:
    """Typed optional action handler for an already admitted captured Provider."""

    port: SurfaceResourcePort
    scope_for: Callable[[SurfaceResourceInvocation], SurfaceResourceScope]
    ttl_seconds: float = 30

    def invoke(
        self,
        request: Mapping[str, object],
        invocation: SurfaceResourceInvocation,
    ) -> dict[str, object]:
        """Derive scope/context on the server and invoke the public resource action."""
        invocation.assert_current()
        scope = self._scope(invocation)
        return self.port.invoke(
            request,
            scope=scope,
            context=invocation.envelope.context,
            assert_current=invocation.assert_current,
            deadline_monotonic=invocation.envelope.deadline_monotonic,
            ttl_seconds=self.ttl_seconds,
        )

    def consume(
        self,
        selection_id: str,
        *,
        kind: ResourceKind,
        invocation: SurfaceResourceInvocation,
    ) -> object:
        """Rebind a token inside its exact authenticated consuming invocation."""
        invocation.assert_current()
        scope = self._scope(invocation)
        if (
            invocation.envelope.contract_id != scope.consumer_contract_id
            or invocation.envelope.contract_version != scope.consumer_contract_version
            or invocation.envelope.operation_id != scope.consumer_operation_id
            or invocation.envelope.target_principal.value != scope.consumer_principal_id
        ):
            raise SurfaceResourceRejected("resource consuming operation changed")
        return self.port.consume(
            selection_id,
            kind=kind,
            scope=scope,
            context=invocation.envelope.context,
            assert_current=invocation.assert_current,
            deadline_monotonic=invocation.envelope.deadline_monotonic,
        )

    def _scope(self, invocation: SurfaceResourceInvocation) -> SurfaceResourceScope:
        scope = self.scope_for(invocation)
        if (
            not isinstance(scope, SurfaceResourceScope)
            or scope.presentation_owner_principal_id
            != invocation.presentation_owner_principal_id
            or scope.presentation_owner_session_id
            != invocation.presentation_owner_session_id
        ):
            raise SurfaceResourceRejected("resource originating owner changed")
        return scope


def _text(value: object, limit: int) -> bool:
    return (
        isinstance(value, str)
        and 0 < len(value) <= limit
        and value.strip() == value
        and not any(ord(character) < 32 or ord(character) == 127 for character in value)
    )


def _callback(callback: Callable[[], _Result]) -> _Result:
    """Keep every Provider exception, including Host error subclasses, private."""
    try:
        return callback()
    except Exception:
        raise SurfaceResourceProviderError(
            "resource Provider callback failed"
        ) from None


def _request(request: Mapping[str, object]) -> tuple[str, ResourceKind, str | None]:
    if not isinstance(request, Mapping):
        raise SurfaceResourceRejected("resource request is invalid")
    operation = request.get("operation")
    expected = (
        {"operation", "kind"}
        if operation == "acquire"
        else {
            "operation",
            "kind",
            "selection_id",
        }
    )
    kind = request.get("kind")
    if (
        not isinstance(operation, str)
        or operation not in {"acquire", "exchange"}
        or set(request) != expected
        or not isinstance(kind, str)
        or kind not in _KINDS
    ):
        raise SurfaceResourceRejected("resource request is invalid")
    token = request.get("selection_id")
    if operation == "exchange" and (
        not isinstance(token, str) or _TOKEN.fullmatch(token) is None
    ):
        raise SurfaceResourceRejected("resource selection is unavailable")
    return operation, cast(ResourceKind, kind), cast(str | None, token)


def _selection(value: PrivateSurfaceSelection) -> None:
    if (
        not isinstance(value, PrivateSurfaceSelection)
        or value.reference is None
        or not _text(value.display_name, 256)
    ):
        raise SurfaceResourceProviderError("resource Provider selection is invalid")


def _wire(token: str, ticket: _Ticket) -> dict[str, object]:
    return {
        "version": SURFACE_RESOURCE_VERSION,
        "selection_id": token,
        "kind": ticket.kind,
        "display_name": ticket.selection.display_name,
        "expires_at_ms": ticket.expires_at_ms,
        "stage": ticket.stage,
    }
