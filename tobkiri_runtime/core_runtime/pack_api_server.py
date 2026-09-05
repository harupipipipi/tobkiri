"""Finite localhost HTTP boundary for the captured Tobkiri Pack v4 runtime."""

from __future__ import annotations

import heapq
import json
import logging
import os
import re
import threading
import time
import uuid
from dataclasses import dataclass
from enum import Enum
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol, cast
from urllib.parse import parse_qs, urlparse

from .api.api_response import APIResponse
from .api.auth_gate import AuthGateMixin
from .api.http_response import ResponseWriterMixin
from .api.request_body import RequestBodyMixin
from .api.setup_handlers import SetupHandlersMixin
from .api.web_mounts import WebMountMixin
from .api.web_mounts import WebMountEntry
from .control_reconciliation_v4 import (
    ControlReconciliationCapacityError,
    ControlReconciliationConflictError,
    ControlReconciliationError,
    ControlReconciliationStore,
    ControlReconciliationUnavailableError,
)
from .credential_transport import CredentialMaterialStoreFactory
from .global_contracts.http_contract_dispatch import (
    HTTPCapabilitySnapshot,
    HTTPContractBinding,
    HTTPContractRouteError,
    HTTPContractTarget,
    contract_binding_map,
    is_contract_route_path,
    resolve_contract_route,
)
from tobkiri_protocol.canonical import canonical_digest
from tobkiri_host.backends import ExecutionBackend
from .host_contract import (
    ExecutionProfileIdentity,
    HostContractError,
    capture_host_contract,
    capture_host_contract_from_file,
    validate_host_contract,
)
from .panel_auth import PanelAuthBinding, PanelAuthManager, get_panel_auth_manager
from .pack_control_v4 import RuntimeSurfaceFactory
from .authority.v4_models import AuthorityDenied
from .authority.v4 import AuthorityStore
from tobkiri_host.errors import HostCoreError

logger = logging.getLogger(__name__)

THREAD_JOIN_TIMEOUT_SECONDS = 5
MAX_CONCURRENT_REQUESTS = 32
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


class HTTPRuntimeErrorCode(str, Enum):
    """Stable Host error vocabulary for a captured HTTP operation."""

    PROFILE_NOT_ACTIVE = "PROFILE_NOT_ACTIVE"
    STALE_REVISION = "STALE_REVISION"
    DIGEST_MISMATCH = "DIGEST_MISMATCH"
    UNAPPROVED = "UNAPPROVED"
    TIMEOUT = "TIMEOUT"
    INVALID_REQUEST = "INVALID_REQUEST"
    API_FAILURE = "API_FAILURE"


_PUBLIC_ERROR_MESSAGES: Mapping[str, str] = {
    HTTPRuntimeErrorCode.INVALID_REQUEST.value: "The request is invalid",
    HTTPRuntimeErrorCode.PROFILE_NOT_ACTIVE.value: "The active Profile is unavailable",
    HTTPRuntimeErrorCode.STALE_REVISION.value: "The Profile revision is stale",
    HTTPRuntimeErrorCode.DIGEST_MISMATCH.value: "The request binding does not match",
    HTTPRuntimeErrorCode.UNAPPROVED.value: "Host approval is required",
    HTTPRuntimeErrorCode.TIMEOUT.value: "The runtime operation timed out",
    HTTPRuntimeErrorCode.API_FAILURE.value: "The runtime operation is unavailable",
}

_PUBLIC_ERROR_STATUS: Mapping[str, int] = {
    HTTPRuntimeErrorCode.INVALID_REQUEST.value: 400,
    HTTPRuntimeErrorCode.PROFILE_NOT_ACTIVE.value: 409,
    HTTPRuntimeErrorCode.STALE_REVISION.value: 409,
    HTTPRuntimeErrorCode.DIGEST_MISMATCH.value: 409,
    HTTPRuntimeErrorCode.UNAPPROVED.value: 403,
    HTTPRuntimeErrorCode.TIMEOUT.value: 504,
    HTTPRuntimeErrorCode.API_FAILURE.value: 503,
}

_HOST_CONTRACT_UNSET = object()

_ERROR_CODE_ALIASES: Mapping[str, str] = {
    "denied": HTTPRuntimeErrorCode.UNAPPROVED.value,
    "pack_control_denied": HTTPRuntimeErrorCode.UNAPPROVED.value,
    "pack_control_invalid_request": HTTPRuntimeErrorCode.INVALID_REQUEST.value,
    "pack_control_conflict": HTTPRuntimeErrorCode.STALE_REVISION.value,
    "pack_control_stale_revision": HTTPRuntimeErrorCode.STALE_REVISION.value,
    "pack_control_digest_mismatch": HTTPRuntimeErrorCode.DIGEST_MISMATCH.value,
    "pack_control_unapproved": HTTPRuntimeErrorCode.UNAPPROVED.value,
    "pack_control_unavailable": HTTPRuntimeErrorCode.API_FAILURE.value,
    "pack_control_timeout": HTTPRuntimeErrorCode.TIMEOUT.value,
    "timed_out": HTTPRuntimeErrorCode.TIMEOUT.value,
    "backend_unavailable": HTTPRuntimeErrorCode.API_FAILURE.value,
    "provider_failed": HTTPRuntimeErrorCode.API_FAILURE.value,
    "audit_unavailable": HTTPRuntimeErrorCode.API_FAILURE.value,
    "busy": HTTPRuntimeErrorCode.API_FAILURE.value,
    "resource_exhausted": HTTPRuntimeErrorCode.API_FAILURE.value,
    "host_core_error": HTTPRuntimeErrorCode.API_FAILURE.value,
}


def _public_error_code(value: object) -> str:
    """Return one stable public code without reflecting provider-controlled text."""

    if isinstance(value, HTTPRuntimeErrorCode):
        return value.value
    candidate = str(value or "").strip()
    if candidate in _PUBLIC_ERROR_STATUS:
        return candidate
    return _ERROR_CODE_ALIASES.get(candidate.lower(), HTTPRuntimeErrorCode.API_FAILURE.value)


def _public_error_result(code: object) -> dict[str, object]:
    """Build the only error representation persisted or returned by PackAPI."""

    public_code = _public_error_code(code)
    return {
        "host_operation_api_version": "io.tobkiri.host.operation.v1",
        "state": "error",
        "code": public_code,
        "message": _PUBLIC_ERROR_MESSAGES[public_code],
        "retryable": public_code
        in {
            HTTPRuntimeErrorCode.TIMEOUT.value,
            HTTPRuntimeErrorCode.API_FAILURE.value,
        },
        "write_set": [],
    }


def _exception_error_code(error: BaseException) -> str:
    """Classify an internal exception without exposing its text or class name."""

    current: BaseException | None = error
    seen: set[int] = set()
    host_code: str | None = None
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        captured_code = getattr(current, "code", None)
        if captured_code is not None:
            normalized = _public_error_code(captured_code)
            if normalized != HTTPRuntimeErrorCode.API_FAILURE.value:
                return normalized
        if isinstance(current, ControlReconciliationConflictError):
            return HTTPRuntimeErrorCode.DIGEST_MISMATCH.value
        if isinstance(current, ControlReconciliationUnavailableError):
            return HTTPRuntimeErrorCode.API_FAILURE.value
        if isinstance(current, AuthorityDenied):
            authority_codes = {
                "authority_denied": HTTPRuntimeErrorCode.UNAPPROVED.value,
                "revoked": HTTPRuntimeErrorCode.UNAPPROVED.value,
                "stale_epoch": HTTPRuntimeErrorCode.STALE_REVISION.value,
                "stale_revision": HTTPRuntimeErrorCode.STALE_REVISION.value,
                "digest_mismatch": HTTPRuntimeErrorCode.DIGEST_MISMATCH.value,
                "backend_unavailable": HTTPRuntimeErrorCode.API_FAILURE.value,
                "timed_out": HTTPRuntimeErrorCode.TIMEOUT.value,
            }
            return authority_codes.get(str(current.code), HTTPRuntimeErrorCode.UNAPPROVED.value)
        if isinstance(current, HostCoreError):
            candidate = str(current.code)
            if candidate in _ERROR_CODE_ALIASES:
                mapped = _ERROR_CODE_ALIASES[candidate]
                if candidate.startswith("pack_control_"):
                    return mapped
                if mapped != HTTPRuntimeErrorCode.API_FAILURE.value:
                    return mapped
                host_code = mapped
        if isinstance(current, (KeyError, ValueError)):
            return HTTPRuntimeErrorCode.INVALID_REQUEST.value
        current = current.__cause__ or current.__context__
    if host_code is not None:
        return host_code
    return HTTPRuntimeErrorCode.API_FAILURE.value


def _result_record_refs(result: Mapping[str, Any]) -> list[Mapping[str, str]]:
    """Return stable non-secret record references from one mutation result."""

    references: list[Mapping[str, str]] = []
    for key, value in sorted(result.items()):
        if not key.endswith("_id") or not isinstance(value, str) or not value:
            continue
        digest_key = key.removesuffix("_id") + "_digest"
        digest = result.get(digest_key)
        reference: dict[str, str] = {"kind": key.removesuffix("_id"), "id": value}
        if isinstance(digest, str) and digest.startswith("sha256:"):
            reference["digest"] = digest
        references.append(reference)
    return references


_RETIRED_API_ROOTS = frozenset(
    {
        "auth",
        "authority",
        "blocks",
        "capabilities",
        "containers",
        "desktop",
        "flows",
        "executors",
        "functions",
        "graphs",
        "integrations",
        "mobile",
        "network",
        "nodes",
        "packs",
        "panel",
        "pip",
        "privileges",
        "profiles",
        "routes",
        "runtime",
        "secrets",
        "stores",
        "units",
        "viewer",
        "webhooks",
    }
)


class DispatchSession(Protocol):
    """Captured Broker session exposed to the HTTP adapter."""

    def invoke(
        self,
        contract_id: str,
        operation_id: str,
        payload: Mapping[str, object],
        *,
        version_range: str | None = None,
    ) -> Mapping[str, object]:
        """Invoke one exact qualified operation through RequestBroker."""

    def provider_metadata(self, contract_id: str) -> tuple[Mapping[str, object], ...]:
        """Return the providers pinned into the captured activation."""

    def assert_current(self) -> None:
        """Reject a stale, revoked, or replaced capture."""

    def assert_operation_ready(self, contract_id: str, operation_id: str) -> None:
        """Reject a selected operation without a production backend."""

    @property
    def profile_id(self) -> str:
        """Return the exact captured Profile identity."""

    @property
    def plan_digest(self) -> str:
        """Return the exact captured ResolvedPlan digest."""

    @property
    def profile_revision(self) -> str:
        """Return the exact captured Profile revision."""

    @property
    def activation_id(self) -> str:
        """Return the exact captured activation identity."""

    @property
    def security_epoch(self) -> int:
        """Return the exact captured Authority security epoch."""


@dataclass(frozen=True)
class RuntimeCaptureInputs:
    """App-supplied immutable inputs needed to recapture a HTTP runtime."""

    bundle_root: Path
    ecosystem_root: Path
    contract_bindings: tuple[HTTPContractBinding, ...]
    activation_snapshot_loader: ActivationSnapshotLoader | None = None
    runtime_surface_factory: RuntimeSurfaceFactory | None = None
    capability_binding_snapshot_factory: CapabilityBindingSnapshotFactory | None = None
    capability_binding_selector: CapabilityBindingSelector | None = None
    packvm_backend_factory: Callable[[], ExecutionBackend | None] | None = None
    credential_store_factory: CredentialMaterialStoreFactory | None = None


class ActivationSnapshotLoader(Protocol):
    """Application verification of an already-selected activation envelope."""

    def __call__(
        self,
        *,
        active: object,
        workspace: Path,
        profile_id: str,
        authority_store: AuthorityStore,
        catalog: object,
    ) -> object:
        """Return the persisted active snapshot for this Profile."""


class CapabilityBindingSnapshotFactory(Protocol):
    """Application serialization of capability capture facts."""

    def __call__(
        self,
        binding: object,
        *,
        session: object,
        catalog: Mapping[str, object],
    ) -> Mapping[str, object]:
        """Return capability facts for the app surface."""


class CapabilityBindingSelector(Protocol):
    """Application selection of one capability binding from its route map."""

    def __call__(self, bindings: tuple[object, ...]) -> object | None:
        """Return the capability binding or ``None`` when unavailable."""


class RuntimeCaptureFactory(Protocol):
    """Composition root for application-owned map and artifact selection."""

    def __call__(self, active: object | None = None) -> RuntimeCaptureInputs:
        """Return the exact app-selected inputs for one capture boundary."""


class CapabilitySnapshotFactory(Protocol):
    """App-owned contribution capture injected into the generic HTTP Host."""

    def __call__(
        self,
        binding: HTTPContractBinding,
        *,
        session: DispatchSession,
        catalog: Mapping[str, object],
    ) -> HTTPCapabilitySnapshot:
        """Return targets admitted for this exact application capture."""


class CapabilitySnapshotReader(Protocol):
    """Read one captured capability snapshot for a selected HTTP map entry."""

    def __call__(
        self,
        binding: HTTPContractBinding,
        *,
        catalog: Mapping[str, object] | None = None,
    ) -> HTTPCapabilitySnapshot:
        """Return capture-verified static and dynamic targets."""


@dataclass(frozen=True)
class ApplicationHTTPContractRequest:
    """An application-decoded dynamic request, still subject to Host checks."""

    target: HTTPContractTarget
    payload: Mapping[str, object]


class HTTPApplicationPresentation(Protocol):
    """Application-owned HTTP payload and result presentation rules."""

    def decode_request(
        self,
        binding: HTTPContractBinding,
        *,
        body: Mapping[str, object],
        query: Mapping[str, object],
        session: DispatchSession,
        snapshot: HTTPCapabilitySnapshot,
    ) -> ApplicationHTTPContractRequest | None:
        """Decode an application-specific multi-target request."""

    def normalize_payload(
        self,
        target: HTTPContractTarget,
        payload: Mapping[str, object],
        *,
        session: DispatchSession,
        workspace_binding_resolver: WorkspaceBindingResolver | None,
    ) -> Mapping[str, object]:
        """Apply application-specific payload semantics after Host selection."""

    def requires_operation_ready(self, target: HTTPContractTarget) -> bool:
        """Return whether this UI target must be backend-ready at bind time."""

    def present_result(
        self,
        binding: HTTPContractBinding,
        result: Mapping[str, object],
        *,
        session: DispatchSession | None,
        routes: Mapping[tuple[str, str], HTTPContractBinding],
        capability_snapshot: CapabilitySnapshotReader,
    ) -> Mapping[str, object]:
        """Apply app UI projection to a Host-sanitized terminal result."""


class WorkspaceBindingResolver(Protocol):
    """Host-injected port for an immutable selected-workspace capture."""

    def __call__(self, profile_id: str) -> Mapping[str, object]:
        """Return canonical root and filesystem identity for the Profile."""


class LifecyclePort(Protocol):
    """Read-only lifecycle surface required by the HTTP shell."""

    def check_setup_status(self) -> dict[str, object]:
        """Return canonical setup and readiness state."""

    def get_health(self) -> dict[str, object]:
        """Return current process health."""


class PackVMLifecyclePort(Protocol):
    """Typed Host-owned lifecycle for the dedicated v4 PackVM."""

    def production_backend_registration(self) -> object | None: ...

    def prepare(self, *, session_id: str | None = None) -> Mapping[str, object]: ...

    def consent(
        self, payload: Mapping[str, object], *, session_id: str | None = None
    ) -> Mapping[str, object]: ...

    def provision(
        self, payload: Mapping[str, object], *, session_id: str | None = None
    ) -> Mapping[str, object]: ...

    def doctor(self) -> Mapping[str, object]: ...

    def readiness_snapshot(self) -> Mapping[str, object]: ...

    def progress(
        self, operation_id: str, *, session_id: str | None = None
    ) -> Mapping[str, object]: ...

    def cancel(
        self, payload: Mapping[str, object], *, session_id: str | None = None
    ) -> Mapping[str, object]: ...

    def stop(self, payload: Mapping[str, object]) -> Mapping[str, object]: ...

    def cleanup(
        self, payload: Mapping[str, object], *, session_id: str | None = None
    ) -> Mapping[str, object]: ...


@dataclass(frozen=True)
class RuntimeHTTPConfig:
    """Verified local-only server coordinates."""

    host: str
    port: int

    @classmethod
    def verify(cls, host: str, port: int) -> "RuntimeHTTPConfig":
        """Canonicalize a loopback request and reject network exposure."""

        if host.strip().lower() not in _LOOPBACK_HOSTS:
            raise ValueError("Pack v4 HTTP server is loopback-only")
        if not isinstance(port, int) or not 0 <= port <= 65535:
            raise ValueError("Pack v4 HTTP port must be between 0 and 65535")
        return cls(host="127.0.0.1", port=port)


class _PackThreadingHTTPServer(ThreadingHTTPServer):
    """Thread-per-request local server with bounded lifecycle semantics."""

    allow_reuse_address = True
    daemon_threads = True
    block_on_close = False
    request_queue_size = 128

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self._request_condition = threading.Condition()
        self._active_requests = 0
        self._accepting_requests = True
        self._request_slots = threading.BoundedSemaphore(MAX_CONCURRENT_REQUESTS)
        super().__init__(*args, **kwargs)

    def process_request(self, request: Any, client_address: Any) -> None:
        with self._request_condition:
            accepted = self._accepting_requests and self._request_slots.acquire(blocking=False)
            if accepted:
                self._active_requests += 1
        if not accepted:
            self._reject_overloaded_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            with self._request_condition:
                self._active_requests -= 1
                self._request_condition.notify_all()
            self._request_slots.release()
            raise

    def process_request_thread(self, request: Any, client_address: Any) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            with self._request_condition:
                self._active_requests -= 1
                self._request_condition.notify_all()
            self._request_slots.release()

    def _reject_overloaded_request(self, request: Any) -> None:
        """Apply backpressure without allocating another handler thread."""

        body = b'{"success":false,"error":"Pack API request capacity exhausted"}'
        response = (
            b"HTTP/1.1 503 Service Unavailable\r\n"
            b"Content-Type: application/json; charset=utf-8\r\n"
            b"Cache-Control: no-store\r\n"
            b"Connection: close\r\n" + f"Content-Length: {len(body)}\r\n\r\n".encode("ascii") + body
        )
        try:
            request.sendall(response)
        except OSError:
            pass
        finally:
            self.shutdown_request(request)

    def request_shutdown(self) -> None:
        """Request ``serve_forever`` exit without waiting for its thread."""

        # ``BaseServer.shutdown`` performs this assignment and then waits on
        # its private event.  Stop owns the bounded join below, so avoid that
        # unbounded wait while retaining the standard serve-forever signal.
        setattr(self, "_BaseServer__shutdown_request", True)

    def stop_accepting_requests(self) -> None:
        """Fence new handlers before lifecycle teardown begins."""

        with self._request_condition:
            self._accepting_requests = False

    def close_handler_slots(self) -> None:
        """Permanently fence this server instance after all handlers drain."""

        with self._request_condition:
            self._accepting_requests = False

    def wait_for_request_drain(self, timeout: float) -> bool:
        """Wait a bounded interval for accepted handlers to finish."""

        deadline = time.monotonic() + timeout
        with self._request_condition:
            while self._active_requests:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._request_condition.wait(remaining)
        return True

    def teardown_snapshot(self) -> dict[str, object]:
        """Return bounded, non-sensitive state for failed teardown diagnostics."""

        with self._request_condition:
            return {
                "active_requests": self._active_requests,
                "accepting_requests": self._accepting_requests,
            }


class _RequestReplayCapacityError(RuntimeError):
    """Raised when replay state is full of still-live session identities."""


@dataclass
class _ReplaySession:
    request_ids: dict[str, float]


class _RequestReplayGuard:
    """Consume browser request identities once per authenticated server."""

    _REQUEST_ID = re.compile(
        r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-" r"[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
    )

    DEFAULT_CAPACITY = 100_000

    def __init__(
        self,
        *,
        capacity: int = DEFAULT_CAPACITY,
        clock: Callable[[], float] = time.monotonic,
        max_session_ttl_seconds: float = PanelAuthManager.DEFAULT_SESSION_TTL_SECONDS,
    ) -> None:
        if capacity <= 0:
            raise ValueError("replay capacity must be positive")
        if max_session_ttl_seconds <= 0:
            raise ValueError("maximum session TTL must be positive")
        self._lock = threading.Lock()
        self._capacity = capacity
        self._clock = clock
        self._max_session_ttl_seconds = max_session_ttl_seconds
        self._sessions: dict[str, _ReplaySession] = {}
        self._expirations: list[tuple[float, str, str]] = []
        self._size = 0

    def consume(
        self,
        session_id: str,
        request_id: str,
        *,
        session_ttl_seconds: float | None = None,
    ) -> bool:
        """Return true only for a fresh canonical request identity."""

        if not session_id or self._REQUEST_ID.fullmatch(request_id) is None:
            return False
        requested_ttl = (
            self._max_session_ttl_seconds
            if session_ttl_seconds is None
            else float(session_ttl_seconds)
        )
        ttl = min(
            self._max_session_ttl_seconds,
            max(0.0, requested_ttl),
        )
        with self._lock:
            now = self._clock()
            self._purge_expired_locked(now)
            session = self._sessions.get(session_id)
            if session is None:
                session = _ReplaySession(request_ids={})
                self._sessions[session_id] = session
            if request_id in session.request_ids:
                return False
            if self._size >= self._capacity:
                if not session.request_ids:
                    del self._sessions[session_id]
                raise _RequestReplayCapacityError("request replay capacity exhausted")
            expires_at = now + ttl
            session.request_ids[request_id] = expires_at
            heapq.heappush(self._expirations, (expires_at, session_id, request_id))
            self._size += 1
        return True

    def _purge_expired_locked(self, now: float) -> None:
        while self._expirations and self._expirations[0][0] <= now:
            expires_at, session_id, request_id = heapq.heappop(self._expirations)
            session = self._sessions.get(session_id)
            if session is None or session.request_ids.get(request_id) != expires_at:
                continue
            del session.request_ids[request_id]
            self._size -= 1
            if not session.request_ids:
                del self._sessions[session_id]

    def renew_session(self, session_id: str, *, session_ttl_seconds: float) -> None:
        """Purge elapsed identities without extending their absolute horizons."""

        with self._lock:
            self._purge_expired_locked(self._clock())

    def snapshot(self) -> Mapping[str, int]:
        """Return non-sensitive bounded-state counters for tests and diagnostics."""

        with self._lock:
            self._purge_expired_locked(self._clock())
            return {
                "capacity": self._capacity,
                "entries": self._size,
                "sessions": len(self._sessions),
            }

    def valid(self, session_id: str, request_id: str) -> bool:
        """Return whether an identity is canonical without consuming it."""

        return bool(session_id and self._REQUEST_ID.fullmatch(request_id))


class PackAPIHandler(
    ResponseWriterMixin,
    AuthGateMixin,
    RequestBodyMixin,
    SetupHandlersMixin,
    WebMountMixin,
    BaseHTTPRequestHandler,
):
    """Serve only health, setup, panel auth/static, and Pack v4 dispatch."""

    _CLIENT_DISCONNECT_EXCEPTIONS = (
        BrokenPipeError,
        ConnectionResetError,
        ConnectionAbortedError,
    )
    _panel_auth_manager: PanelAuthManager | None = None
    _dispatch_session: DispatchSession | None = None
    _contract_routes: Mapping[tuple[str, str], HTTPContractBinding] = {}
    _capability_snapshot_factory: CapabilitySnapshotFactory | None = None
    _application_presentation: HTTPApplicationPresentation | None = None
    _contract_replay_guard: _RequestReplayGuard | None = None
    _operation_journal: ControlReconciliationStore | None = None
    _runtime_refresh: Callable[[DispatchSession | None], None] | None = None
    _packvm_lifecycle: PackVMLifecyclePort | None = None
    _workspace_binding_resolver: WorkspaceBindingResolver | None = None
    _host_contract_snapshot: Mapping[str, Any] | None = None
    _instance_web_mounts: tuple[WebMountEntry, ...] | None = None
    app_lifecycle_manager: LifecyclePort | None = None
    _runtime_port = 8765
    _request_auth_mode: str | None = None
    _panel_session: Mapping[str, object] | None = None
    _panel_session_cookie: str | None = None
    _raw_body_bytes = b""

    @staticmethod
    def canonical_v4_server_handler(
        *,
        panel_auth_manager: PanelAuthManager,
        dispatch_session: DispatchSession | None,
        app_lifecycle_manager: LifecyclePort | None,
        contract_routes: (Mapping[tuple[str, str], HTTPContractBinding] | None) = None,
        capability_snapshot_factory: CapabilitySnapshotFactory | None = None,
        application_presentation: HTTPApplicationPresentation | None = None,
        replay_guard: _RequestReplayGuard | None = None,
        operation_journal: ControlReconciliationStore | None = None,
        web_mounts: tuple[WebMountEntry, ...] | None = None,
        runtime_refresh: Callable[[DispatchSession | None], None] | None = None,
        workspace_binding_resolver: WorkspaceBindingResolver | None = None,
        packvm_lifecycle: PackVMLifecyclePort | None = None,
        host_contract: Mapping[str, Any] | None = None,
    ) -> type["PackAPIHandler"]:
        """Create an isolated handler bound to one captured runtime session."""

        bound_panel_auth = panel_auth_manager
        bound_dispatch = dispatch_session
        bound_lifecycle = app_lifecycle_manager
        bound_contract_routes = dict(contract_routes or {})
        bound_capability_snapshot_factory = capability_snapshot_factory
        bound_application_presentation = application_presentation
        bound_replay_guard = replay_guard
        bound_operation_journal = operation_journal
        bound_web_mounts = web_mounts
        bound_runtime_refresh = runtime_refresh
        bound_workspace_binding_resolver = workspace_binding_resolver
        bound_packvm_lifecycle = packvm_lifecycle
        bound_host_contract = (
            validate_host_contract(
                host_contract,
                expected_identity=(
                    bound_dispatch
                    if bound_dispatch is not None
                    and getattr(bound_dispatch, "session_kind", None) != "host_profile_control"
                    else None
                ),
            )
            if host_contract is not None
            else None
        )

        class BoundPackAPIHandler(PackAPIHandler):
            _panel_auth_manager = bound_panel_auth
            _dispatch_session = bound_dispatch
            app_lifecycle_manager = bound_lifecycle
            _contract_routes = bound_contract_routes
            _capability_snapshot_factory = (
                staticmethod(bound_capability_snapshot_factory)
                if bound_capability_snapshot_factory is not None
                else None
            )
            _application_presentation = bound_application_presentation
            _contract_replay_guard = bound_replay_guard
            _operation_journal = bound_operation_journal
            _packvm_lifecycle = bound_packvm_lifecycle
            _host_contract_snapshot = bound_host_contract
            _instance_web_mounts = bound_web_mounts
            _runtime_refresh = (
                staticmethod(bound_runtime_refresh) if bound_runtime_refresh is not None else None
            )
            _workspace_binding_resolver = (
                staticmethod(bound_workspace_binding_resolver)
                if bound_workspace_binding_resolver is not None
                else None
            )

            def _setup_install_pack(self, body: dict[str, object]) -> dict[str, object]:
                from .bootstrap.profile_capture import profile_capture_scope

                with profile_capture_scope():
                    return super()._setup_install_pack(body)

            def _refresh_setup_runtime_after_response(
                self,
                result: Mapping[str, object],
            ) -> None:
                """Request a cold Host recapture after flushing activation success.

                A setup handler serves the HostProfileControl capture.  Once
                activation commits, that capture is intentionally stale and
                must not be replaced in-process from a mutable contract path.
                The Launcher publishes the matching active contract before it
                restarts the Host, which then captures one coherent pair.
                """

                if result.get("state") != "active":
                    return
                from .restart_control import request_kernel_restart

                request_kernel_restart()

            @staticmethod
            def _fixed_web_mounts() -> tuple[WebMountEntry, ...]:
                if bound_web_mounts is not None:
                    return bound_web_mounts
                return WebMountMixin._fixed_web_mounts()

        BoundPackAPIHandler.__name__ = "PackAPIHandlerV4Instance"
        return BoundPackAPIHandler

    def handle(self) -> None:
        """Serve this connection from one immutable Host contract snapshot."""

        snapshot = self._host_contract_snapshot
        if snapshot is None:
            super().handle()
            return
        from .host_contract import bind_host_contract

        with bind_host_contract(snapshot):
            super().handle()

    def log_message(self, format: str, *args: object) -> None:
        """Write request logs after removing bootstrap query material."""

        sanitized = tuple(self._redact_log_value(value) for value in args)
        try:
            message = format % sanitized if sanitized else format
        except (TypeError, ValueError):
            message = " ".join(sanitized) if sanitized else format
        logger.info("API: %s", message)

    def _check_auth(self, method: str, path: str) -> bool:
        authenticated = AuthGateMixin._check_auth(self, method, path)
        if not authenticated:
            return False
        panel_session = self._panel_session
        session_id = panel_session.get("session_id") if panel_session else None
        guard = self._contract_replay_guard
        if isinstance(session_id, str) and guard is not None:
            guard.renew_session(
                session_id,
                session_ttl_seconds=self._panel_session_ttl_seconds(panel_session),
            )
        return True

    @staticmethod
    def _redact_log_value(value: object) -> str:
        return re.sub(
            r"([?&](?:token|code)=)[^&\s\"]+",
            r"\1[REDACTED]",
            "" if value is None else str(value),
            flags=re.IGNORECASE,
        )

    @staticmethod
    def _is_loopback_client(client_address: object) -> bool:
        if not isinstance(client_address, tuple) or not client_address:
            return False
        return str(client_address[0]).lower() in _LOOPBACK_HOSTS | {"::ffff:127.0.0.1"}

    def _reset_request_state(self) -> None:
        self._request_auth_mode = None
        self._panel_session = None
        self._panel_session_cookie = None
        self._raw_body_bytes = b""

    @classmethod
    def _get_cors_origin(cls, request_origin: str) -> str:
        """Allow only the exact local runtime origin."""

        allowed = {
            f"http://127.0.0.1:{cls._runtime_port}",
            f"http://localhost:{cls._runtime_port}",
        }
        return request_origin if request_origin in allowed else ""

    @staticmethod
    def _retired_api_path(path: str) -> bool:
        parts = path.strip("/").split("/")
        return len(parts) >= 2 and parts[0] == "api" and parts[1] in _RETIRED_API_ROOTS

    def _send_retired_api(self, path: str) -> None:
        self._send_response(
            APIResponse(
                False,
                data={
                    "api_version": "io.tobkiri.pack-api.v4",
                    "state": "legacy_api_retired",
                    "retired_route": path,
                    "write_set": [],
                },
                error="Legacy API route is retired; use an exact Pack v4 operation",
            ),
            410,
        )

    def _send_not_found(self) -> None:
        self._send_response(APIResponse(False, error="Not found"), 404)

    def _refresh_setup_runtime_after_response(
        self,
        result: Mapping[str, object],
    ) -> None:
        """No-op unless a canonical server handler binds runtime recapture."""

        del result

    def _send_contract_error(self, error: HTTPContractRouteError) -> None:
        self._send_response(
            APIResponse(
                False,
                data={"state": "contract_dispatch_denied", "code": error.code},
                error=str(error),
            ),
            error.status,
        )

    def _handle_contract_request(self, method: str) -> bool:
        """Dispatch one contract request with one explicit capture scope."""

        from .bootstrap.profile_capture import profile_capture_scope

        with profile_capture_scope():
            return self._handle_contract_request_scoped(method)

    def _handle_contract_request_scoped(self, method: str) -> bool:
        """Resolve, authenticate, and dispatch one exact frontend operation."""

        if not is_contract_route_path(urlparse(self.path).path):
            return False
        try:
            resolved = resolve_contract_route(self, method, self.path)
        except HTTPContractRouteError as error:
            self._discard_request_body()
            self._send_contract_error(error)
            return True
        if resolved is None:  # pragma: no cover - prefix was checked above
            return False
        route_binding = self._contract_routes.get((resolved.method, resolved.path))
        if route_binding is None:
            self._discard_request_body()
            self._send_contract_error(
                HTTPContractRouteError(
                    "CONTRACT_OPERATION_UNKNOWN",
                    "Unknown frontend contract operation",
                    404,
                )
            )
            return True
        if not self._check_auth(method, urlparse(self.path).path):
            self._discard_request_body()
            self._send_response(APIResponse(False, error="Unauthorized"), 401)
            return True
        panel_session = self._panel_session
        raw_session_id = panel_session.get("session_id") if panel_session else None
        session_id: str | None = raw_session_id if isinstance(raw_session_id, str) else None
        session_ttl_seconds = self._panel_session_ttl_seconds(panel_session)
        request_id = self.headers.get("X-Tobkiri-Request-ID", "").strip().lower()
        replay_guard = self._contract_replay_guard
        if (
            not isinstance(session_id, str)
            or replay_guard is None
            or not replay_guard.valid(session_id, request_id)
        ):
            self._discard_request_body()
            self._send_response(
                APIResponse(
                    False,
                    data={
                        "state": "contract_dispatch_denied",
                        "code": "invalid_request_identity",
                    },
                    error="Canonical request identity is missing or invalid",
                ),
                409,
            )
            return True
        session = self._dispatch_session
        if session is None:
            self._discard_request_body()
            self._send_response(
                APIResponse(False, error="Captured v4 dispatch session is unavailable"),
                503,
            )
            return True
        if method.upper() == "GET":
            self._discard_request_body()
            payload: dict[str, object] = dict(resolved.query)
            target = route_binding.targets[0] if len(route_binding.targets) == 1 else None
        else:
            body = self._parse_object_body()
            if body is None:
                return True
            if len(route_binding.targets) == 1:
                payload = {**resolved.query, **body}
                target = route_binding.targets[0]
            else:
                presentation = self._application_presentation
                decoded = (
                    presentation.decode_request(
                        route_binding,
                        body=body,
                        query=resolved.query,
                        session=session,
                        snapshot=self._capability_snapshot(route_binding),
                    )
                    if presentation is not None
                    else None
                )
                if decoded is None:
                    target = None
                    payload = {}
                else:
                    target = decoded.target
                    payload = dict(decoded.payload)
        if target is None:
            self._send_response(
                APIResponse(
                    False,
                    data={
                        "state": "contract_dispatch_denied",
                        "code": "unselected_contract_contribution",
                    },
                    error="Contract contribution is not selected",
                ),
                404,
            )
            return True
        if set(payload) - target.allowed_payload_keys:
            self._send_response(
                APIResponse(
                    False,
                    data={
                        "state": "contract_dispatch_denied",
                        "code": "invalid_contract_payload",
                    },
                    error="Contract payload contains unknown fields",
                ),
                400,
            )
            return True
        try:
            presentation = self._application_presentation
            if presentation is not None:
                payload = dict(
                    presentation.normalize_payload(
                        target,
                        payload,
                        session=session,
                        workspace_binding_resolver=self._workspace_binding_resolver,
                    )
                )
        except (OSError, ValueError) as error:
            logger.warning("Contract payload normalization failed", exc_info=error)
            self._send_response(
                APIResponse(
                    False,
                    data={
                        "state": "contract_dispatch_denied",
                        "code": "invalid_contract_payload",
                    },
                    error="Contract payload is invalid",
                ),
                400,
            )
            return True
        payload["_session_id"] = session_id
        operation_record: Mapping[str, Any] | None = None
        operation_journal = self._operation_journal
        if method.upper() == "GET" and operation_journal is not None:
            try:
                operation_journal.renew_session(
                    session_id,
                    expires_at=time.time() + session_ttl_seconds,
                )
            except ControlReconciliationCapacityError:
                pass
            except (ControlReconciliationUnavailableError, ControlReconciliationError):
                pass
        if method.upper() == "GET":
            try:
                fresh_get = replay_guard.consume(
                    session_id,
                    request_id,
                    session_ttl_seconds=session_ttl_seconds,
                )
            except _RequestReplayCapacityError:
                self._send_replay_capacity_error()
                return True
            if not fresh_get:
                self._send_response(
                    APIResponse(False, error="Canonical request identity is replayed"),
                    409,
                )
                return True
        else:
            if operation_journal is None:
                self._send_response(
                    APIResponse(False, error="Control operation journal is unavailable"),
                    503,
                )
                return True
            request_digest = canonical_digest(
                {
                    "method": resolved.method,
                    "path": resolved.path,
                    "contract_id": target.contract_id,
                    "operation_id": target.operation_id,
                    "payload": payload,
                }
            )
            try:
                operation_record = operation_journal.lookup_operation(
                    request_id=request_id,
                    session_id=session_id,
                    operation_id=target.operation_id,
                    contract_id=target.contract_id,
                    request_digest=request_digest,
                )
            except ControlReconciliationConflictError:
                self._send_response(
                    APIResponse(
                        False,
                        data={
                            "state": "contract_dispatch_denied",
                            "code": "operation_reconciliation_mismatch",
                        },
                        error="Control operation conflicts with durable state",
                    ),
                    409,
                )
                return True
            except (ControlReconciliationUnavailableError, ControlReconciliationError):
                self._send_response(
                    APIResponse(
                        False,
                        data={
                            "state": "contract_dispatch_denied",
                            "code": "operation_reconciliation_unavailable",
                        },
                        error="Control operation reconciliation is unavailable",
                    ),
                    503,
                )
                return True
            if operation_record is not None:
                state = str(operation_record["state"])
                prior_result = operation_record.get("result")
                if state in {"succeeded", "failed"} and isinstance(prior_result, Mapping):
                    self._send_contract_outcome(route_binding, prior_result)
                    return True
            try:
                # Only an exact durable terminal result may bypass freshness.
                # Unknown and pending requests must prove the capture current
                # before any replay admission, renewal, or journal write.
                session.assert_current()
            except (
                HostCoreError,
                KeyError,
                OSError,
                RuntimeError,
                ValueError,
            ) as error:
                public_result = _public_error_result(_exception_error_code(error))
                self._send_contract_outcome(route_binding, public_result)
                if public_result["code"] == HTTPRuntimeErrorCode.UNAPPROVED.value:
                    self._defer_response_log(
                        logger,
                        logging.INFO,
                        "Contract dispatch denied for %s/%s: %s",
                        target.contract_id,
                        target.operation_id,
                        public_result["code"],
                    )
                else:
                    self._defer_response_log(
                        logger,
                        logging.WARNING,
                        "Contract dispatch failed for %s/%s",
                        target.contract_id,
                        target.operation_id,
                        exc_info=error,
                    )
                return True
            if operation_record is not None:
                self._send_response(
                    APIResponse(
                        str(operation_record["state"]) == "pending",
                        data=dict(operation_record),
                    ),
                    200 if str(operation_record["state"]) == "pending" else 409,
                )
                return True
            replay_capacity_exhausted = False
            try:
                fresh = replay_guard.consume(
                    session_id,
                    request_id,
                    session_ttl_seconds=session_ttl_seconds,
                )
            except _RequestReplayCapacityError:
                fresh = False
                replay_capacity_exhausted = True
            if not fresh:
                if replay_capacity_exhausted:
                    self._send_replay_capacity_error()
                else:
                    self._send_response(
                        APIResponse(
                            False,
                            error="Canonical request identity is replayed",
                        ),
                        409,
                    )
                return True
            try:
                operation_journal.renew_session(
                    session_id,
                    expires_at=time.time() + session_ttl_seconds,
                )
                operation_record, created = operation_journal.begin_operation(
                    request_id=request_id,
                    session_id=session_id,
                    operation_id=target.operation_id,
                    contract_id=target.contract_id,
                    request_digest=request_digest,
                    session_expires_at=time.time() + session_ttl_seconds,
                )
            except ControlReconciliationCapacityError:
                self._send_reconciliation_capacity_error()
                return True
            except ControlReconciliationConflictError:
                self._send_response(
                    APIResponse(
                        False,
                        data={
                            "state": "contract_dispatch_denied",
                            "code": "operation_reconciliation_mismatch",
                        },
                        error="Control operation conflicts with durable state",
                    ),
                    409,
                )
                return True
            except (ControlReconciliationUnavailableError, ControlReconciliationError):
                self._send_response(
                    APIResponse(
                        False,
                        data={
                            "state": "contract_dispatch_denied",
                            "code": "operation_reconciliation_unavailable",
                        },
                        error="Control operation reconciliation is unavailable",
                    ),
                    503,
                )
                return True
            if not created:
                state = str(operation_record["state"])
                prior_result = operation_record.get("result")
                if state in {"succeeded", "failed"} and isinstance(prior_result, Mapping):
                    self._send_contract_outcome(route_binding, prior_result)
                else:
                    self._send_response(
                        APIResponse(state == "pending", data=dict(operation_record)),
                        200 if state == "pending" else 409,
                    )
                return True
        try:
            session.assert_current()
            result = session.invoke(
                target.contract_id,
                target.operation_id,
                payload,
            )
            safe_result = self._safe_contract_result(result)
            if operation_journal is not None and operation_record is not None:
                operation_journal.finish_operation(
                    request_id,
                    session_id=session_id,
                    state=("failed" if safe_result.get("state") == "error" else "succeeded"),
                    result=safe_result,
                    record_refs=_result_record_refs(safe_result),
                    safe_error_code=(
                        str(safe_result.get("code"))
                        if safe_result.get("state") == "error"
                        else None
                    ),
                )
            self._refresh_after_operation(target.operation_id, safe_result)
        except (
            HostCoreError,
            KeyError,
            OSError,
            RuntimeError,
            ValueError,
        ) as error:
            public_result = _public_error_result(_exception_error_code(error))
            journal_error: BaseException | None = None
            if operation_journal is not None and operation_record is not None:
                try:
                    operation_journal.finish_operation(
                        request_id,
                        session_id=session_id,
                        state="failed",
                        result=public_result,
                        safe_error_code=str(public_result["code"]),
                    )
                except ControlReconciliationError as reconciliation_error:
                    journal_error = reconciliation_error
                    public_result = _public_error_result(HTTPRuntimeErrorCode.API_FAILURE)
            self._send_contract_outcome(route_binding, public_result)
            # Write the bounded, sanitized response before diagnostic logging.
            # Logging a provider traceback can contend with suite-wide capture or
            # a slow sink and must never extend the frontend response deadline.
            if journal_error is not None:
                self._defer_response_log(
                    logger,
                    logging.WARNING,
                    "Contract rejection reconciliation failed for %s/%s",
                    target.contract_id,
                    target.operation_id,
                    exc_info=journal_error,
                )
            elif public_result["code"] == HTTPRuntimeErrorCode.UNAPPROVED.value:
                self._defer_response_log(
                    logger,
                    logging.INFO,
                    "Contract dispatch denied for %s/%s: %s",
                    target.contract_id,
                    target.operation_id,
                    public_result["code"],
                )
            else:
                self._defer_response_log(
                    logger,
                    logging.WARNING,
                    "Contract dispatch failed for %s/%s",
                    target.contract_id,
                    target.operation_id,
                    exc_info=error,
                )
            return True
        self._send_contract_outcome(route_binding, safe_result)
        return True

    @staticmethod
    def _panel_session_ttl_seconds(session: Mapping[str, object] | None) -> float:
        value = session.get("expires_in") if session else None
        if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0:
            return min(float(value), PanelAuthManager.DEFAULT_SESSION_TTL_SECONDS)
        return float(PanelAuthManager.DEFAULT_SESSION_TTL_SECONDS)

    def _send_replay_capacity_error(self) -> None:
        self._send_response(
            APIResponse(
                False,
                data={
                    "state": "contract_dispatch_denied",
                    "code": "request_replay_capacity_exhausted",
                },
                error="Request replay protection is temporarily unavailable",
            ),
            503,
        )

    def _send_reconciliation_capacity_error(self) -> None:
        self._send_response(
            APIResponse(
                False,
                data={
                    "state": "contract_dispatch_denied",
                    "code": "operation_reconciliation_capacity_exhausted",
                },
                error="Control operation reconciliation capacity is exhausted",
            ),
            503,
        )

    @staticmethod
    def _contract_result_status(result: Mapping[str, object]) -> int:
        """Map one public typed result to its semantic HTTP status."""

        if result.get("state") == "error":
            return _PUBLIC_ERROR_STATUS[_public_error_code(result.get("code"))]
        return 200

    @staticmethod
    def _safe_contract_result(result: Mapping[str, object]) -> dict[str, object]:
        """Remove all provider-controlled detail from typed failure results."""

        if result.get("state") == "error":
            return _public_error_result(result.get("code"))
        return dict(result)

    def _send_contract_outcome(
        self,
        binding: HTTPContractBinding,
        result: Mapping[str, object],
    ) -> None:
        """Send initial and replayed terminal outcomes through one mapping."""

        safe_result = self._safe_contract_result(result)
        status = self._contract_result_status(safe_result)
        presented = self._present_contract_result(binding, safe_result)
        if status == 200:
            self._send_response(APIResponse(True, data=presented), status)
            return
        self._send_response(
            APIResponse(
                False,
                data=presented,
                error=_PUBLIC_ERROR_MESSAGES[str(safe_result["code"])],
            ),
            status,
        )

    def _handle_packvm_lifecycle(self, method: str, path: str) -> bool:
        """Serve the finite authenticated v4 PackVM lifecycle contract."""

        prefix = "/api/v4/packvm/"
        if not path.startswith(prefix):
            return False
        operation = path.removeprefix(prefix)
        allowed = {
            ("POST", "prepare"),
            ("POST", "consent"),
            ("POST", "provision"),
            ("GET", "doctor"),
            ("GET", "progress"),
            ("POST", "cancel"),
            ("POST", "stop"),
            ("POST", "cleanup"),
        }
        if (method, operation) not in allowed:
            self._discard_request_body()
            self._send_not_found()
            return True
        if not self._check_auth(method, path):
            self._discard_request_body()
            self._send_response(APIResponse(False, error="Unauthorized"), 401)
            return True
        panel_session = self._panel_session
        raw_packvm_session_id = panel_session.get("session_id") if panel_session else None
        packvm_session_id: str | None = (
            raw_packvm_session_id if isinstance(raw_packvm_session_id, str) else None
        )
        if method == "POST":
            request_id = self.headers.get("X-Tobkiri-Request-ID", "").strip().lower()
            guard = self._contract_replay_guard
            try:
                fresh_request = bool(
                    packvm_session_id is not None
                    and guard is not None
                    and guard.consume(
                        packvm_session_id,
                        request_id,
                        session_ttl_seconds=self._panel_session_ttl_seconds(panel_session),
                    )
                )
            except _RequestReplayCapacityError:
                self._discard_request_body()
                self._send_replay_capacity_error()
                return True
            if not fresh_request:
                self._discard_request_body()
                self._send_response(
                    APIResponse(False, error="Canonical request identity is missing or replayed"),
                    409,
                )
                return True
            payload = self._parse_object_body()
            if payload is None:
                return True
        else:
            self._discard_request_body()
            payload = {}
        lifecycle = self._packvm_lifecycle
        if lifecycle is None:
            self._send_response(APIResponse(False, error="PackVM lifecycle is unavailable"), 503)
            return True
        try:
            if operation == "prepare":
                if payload:
                    raise ValueError("PackVM prepare payload must be empty")
                result = lifecycle.prepare(session_id=packvm_session_id)
            elif operation == "consent":
                result = lifecycle.consent(payload, session_id=packvm_session_id)
            elif operation == "provision":
                result = lifecycle.provision(payload, session_id=packvm_session_id)
            elif operation == "doctor":
                result = lifecycle.doctor()
            elif operation == "progress":
                operation_values = parse_qs(urlparse(self.path).query).get("operation_id", [])
                if len(operation_values) != 1:
                    raise ValueError("PackVM progress requires one operation_id")
                result = lifecycle.progress(operation_values[0], session_id=packvm_session_id)
            elif operation == "cancel":
                result = lifecycle.cancel(payload, session_id=packvm_session_id)
            elif operation == "stop":
                result = lifecycle.stop(payload)
            else:
                result = lifecycle.cleanup(payload, session_id=packvm_session_id)
            if operation == "doctor" and result.get("ready") is True and self._runtime_refresh:
                self._runtime_refresh(None)
            elif operation == "stop" and self._runtime_refresh:
                self._runtime_refresh(None)
            elif (
                operation == "progress"
                and result.get("operation_kind") == "cleanup"
                and result.get("state") == "succeeded"
                and self._runtime_refresh
            ):
                self._runtime_refresh(None)
        except (OSError, RuntimeError, ValueError) as error:
            public_result = _public_error_result(_exception_error_code(error))
            logger.warning(
                "PackVM lifecycle operation failed: %s",
                operation,
                exc_info=error,
            )
            self._send_response(
                APIResponse(
                    False,
                    data=public_result,
                    error=_PUBLIC_ERROR_MESSAGES[str(public_result["code"])],
                ),
                _PUBLIC_ERROR_STATUS[str(public_result["code"])],
            )
            return True
        self._send_response(APIResponse(True, data=dict(result)))
        return True

    def _refresh_after_operation(
        self,
        operation_id: str,
        result: Mapping[str, Any],
    ) -> None:
        """Publish a fresh runtime capture after an activation boundary."""

        refresh = self._runtime_refresh
        if operation_id == "profile.change.activate" and (
            result.get("state") != "active" or not result.get("activation_id")
        ):
            return
        if refresh is not None and operation_id in {
            "pack.enable",
            "pack.disable",
            "approval.revoke",
            "profile.change.activate",
            "runtime.restart",
        }:
            refresh(None)

    def _capability_snapshot(
        self,
        binding: HTTPContractBinding,
        catalog: Mapping[str, object] | None = None,
    ) -> HTTPCapabilitySnapshot:
        """Capture the exact targets and hash used by selection and presentation."""

        session = self._dispatch_session
        if session is None:
            return HTTPCapabilitySnapshot(
                catalog_hash=canonical_digest(
                    {
                        "profile_id": "",
                        "profile_revision": "",
                        "activation_id": "",
                        "plan_digest": "",
                        "contributions": [],
                    }
                ),
                targets=(),
            )
        if catalog is None:
            catalog = getattr(self, "_capability_catalog_cache", None)
            if catalog is None:
                try:
                    from .pack_control_v4 import capture_pack_catalog_reader

                    catalog = capture_pack_catalog_reader().read()
                    # A handler serves one request, so this cache cannot span a
                    # lifecycle mutation while avoiding duplicate authority
                    # database scans for hash and target selection.
                    self._capability_catalog_cache = catalog
                except Exception:
                    catalog = {"packs": []}
        if not isinstance(catalog, Mapping):
            catalog = {"packs": []}
        factory = self._capability_snapshot_factory
        if factory is None:
            return HTTPCapabilitySnapshot(
                catalog_hash=canonical_digest(
                    {
                        "profile_id": session.profile_id,
                        "profile_revision": session.profile_revision,
                        "activation_id": session.activation_id,
                        "plan_digest": session.plan_digest,
                        "contributions": [],
                    }
                ),
                targets=(),
            )
        return factory(binding, session=session, catalog=catalog)

    def _present_contract_result(
        self,
        binding: HTTPContractBinding,
        result: Mapping[str, object],
    ) -> dict[str, object]:
        """Apply an injected application projection to a sanitized result."""

        presentation = self._application_presentation
        if presentation is None:
            return dict(result)
        return dict(
            presentation.present_result(
                binding,
                result,
                session=self._dispatch_session,
                routes=self._contract_routes,
                capability_snapshot=self._capability_snapshot,
            )
        )

    def _parse_object_body(self) -> dict[str, object] | None:
        """Parse one JSON object and reject every other JSON root type."""

        parsed: object = self._parse_body()
        if parsed is None:
            return None
        if not isinstance(parsed, dict) or any(not isinstance(key, str) for key in parsed):
            self._send_response(
                APIResponse(False, error="Request body must be a JSON object"),
                400,
            )
            return None
        return {key: value for key, value in parsed.items() if isinstance(key, str)}

    def _send_mapping_result(self, result: Mapping[str, object]) -> None:
        response, status_code = self._mapping_response(result)
        self._send_response(response, status_code)

    @staticmethod
    def _mapping_response(
        result: Mapping[str, object],
    ) -> tuple[APIResponse, int]:
        """Convert one typed handler result into its HTTP envelope and status."""

        error = result.get("error")
        status = result.get("status_code", 500 if error is not None else 200)
        status_code = status if isinstance(status, int) else 500
        if error is None:
            return APIResponse(True, data=dict(result)), status_code
        return (
            APIResponse(
                False,
                data={
                    key: value
                    for key, value in result.items()
                    if key not in {"error", "status_code"}
                },
                error=str(error),
            ),
            status_code,
        )

    def _is_retired_setup_complete_path(self) -> bool:
        """Match only the canonical retired path, with any query string."""

        return urlparse(self.path).path == "/api/setup/complete"

    def _handle_retired_setup_complete(self, *, head_only: bool = False) -> None:
        """Return the method-independent no-write retirement contract."""

        self._discard_request_body()
        result = self._retired_setup_complete_state()
        if not head_only:
            self._send_mapping_result(result)
            return
        response, status = self._mapping_response(result)
        data = response.to_json().encode("utf-8")
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
        except self._CLIENT_DISCONNECT_EXCEPTIONS:
            self.close_connection = True

    def _handle_health(self) -> None:
        lifecycle = self.__class__.app_lifecycle_manager
        health: dict[str, object] = (
            lifecycle.get_health()
            if lifecycle is not None
            else {
                "status": "ok",
                "needs_setup": True,
                "runtime_status": "starting",
            }
        )
        challenge = (
            self.headers.get("X-Rumi-Desktop-Health-Challenge", "")
            if hasattr(self, "headers")
            else ""
        )
        identity = self._current_health_execution_identity()
        if identity is not None:
            health.update(identity.as_mapping())
        manager = self.__class__._panel_auth_manager
        challenge_response = (
            manager.desktop_challenge_response(challenge) if manager is not None else ""
        )
        if challenge_response:
            health["desktop_challenge_response"] = challenge_response
        self._send_response(APIResponse(True, data=health))

    @classmethod
    def _current_health_execution_identity(cls) -> ExecutionProfileIdentity | None:
        """Project only a current non-bootstrap execution capture on health."""

        session = cls._dispatch_session
        if session is None or getattr(session, "session_kind", None) == "host_profile_control":
            return None
        try:
            session.assert_current()
            return ExecutionProfileIdentity.from_source(session)
        except (
            AttributeError,
            HostContractError,
            HostCoreError,
            KeyError,
            OSError,
            RuntimeError,
            TypeError,
            ValueError,
        ):
            return None

    @classmethod
    def _current_panel_auth_binding(cls) -> PanelAuthBinding | None:
        """Capture the current host-owned identity for panel authentication."""

        session = cls._dispatch_session
        if session is None:
            return None
        try:
            session.assert_current()
            security_epoch = int(getattr(session, "security_epoch"))
            if security_epoch < 1:
                return None
            return PanelAuthBinding(
                profile_id=str(session.profile_id),
                profile_revision=str(session.profile_revision),
                activation_id=str(session.activation_id),
                plan_digest=str(session.plan_digest),
                security_epoch=security_epoch,
            )
        except (
            AttributeError,
            HostCoreError,
            KeyError,
            OSError,
            RuntimeError,
            TypeError,
            ValueError,
        ):
            return None

    def _handle_panel_bootstrap(self) -> None:
        manager = self._panel_auth_manager
        secret = self.headers.get("X-Rumi-Desktop-Bootstrap", "")
        binding = self._current_panel_auth_binding()
        if (
            manager is None
            or binding is None
            or not self._is_loopback_client(self.client_address)
            or not manager.validate_bootstrap_secret(secret)
        ):
            self._discard_request_body()
            self._send_response(APIResponse(False, error="Unauthorized"), 401)
            return
        self._discard_request_body()
        self._send_response(APIResponse(True, data=manager.issue_login_code(binding)))

    def _handle_panel_exchange(self, body: Mapping[str, object]) -> None:
        manager = self._panel_auth_manager
        if not self._is_loopback_client(self.client_address) or not self._check_panel_origin():
            self._send_response(APIResponse(False, error="Forbidden origin"), 403)
            return
        code_value = body.get("code")
        code = code_value.strip() if isinstance(code_value, str) else ""
        binding = self._current_panel_auth_binding()
        exchange = (
            manager.exchange_code(code, binding)
            if manager is not None and binding is not None
            else None
        )
        if exchange is None:
            self._send_response(APIResponse(False, error="Invalid or expired code"), 401)
            return
        cookie = self._build_set_cookie(
            "rumi_panel_session",
            str(exchange["session_id"]),
            path="/",
            max_age=int(exchange["expires_in"]),
            http_only=True,
        )
        self._send_response(
            APIResponse(
                True,
                data={
                    "csrf_token": exchange["csrf_token"],
                    "expires_in": exchange["expires_in"],
                },
            ),
            extra_headers=[("Set-Cookie", cookie)],
        )

    def _setup_pre_auth_allowed(self) -> bool:
        lifecycle = self.__class__.app_lifecycle_manager
        if lifecycle is None:
            return False
        try:
            return lifecycle.check_setup_status().get("needs_setup") is True
        except (OSError, RuntimeError, ValueError):
            logger.exception("Canonical setup state could not be verified")
            return False

    def _handle_setup_status(self) -> None:
        lifecycle = self.__class__.app_lifecycle_manager
        state: dict[str, object] = (
            lifecycle.check_setup_status()
            if lifecycle is not None
            else {
                "needs_setup": True,
                "reason": "lifecycle_manager_unavailable",
            }
        )
        self._send_response(APIResponse(True, data=state))

    def _serve_mount_bootstrap_page(
        self,
        target: str,
        mount: WebMountEntry,
    ) -> None:
        """Exchange a one-time desktop code before serving an authenticated mount."""

        prefix = mount["path_prefix"]
        index_path = f"{prefix}/{mount['index_file']}"
        allowed_targets = {prefix, f"{prefix}/", index_path}
        safe_target = target if target in allowed_targets else f"{prefix}/"
        if safe_target == index_path:
            safe_target = f"{prefix}/"
        target_literal = json.dumps(safe_target)
        document = f"""<!doctype html><meta charset=\"utf-8\"><title>Tobkiri</title>
<script>
document.addEventListener('DOMContentLoaded',()=>{{
const code=new URL(location.href).searchParams.get('code');
if(!code){{document.body.textContent='Tobkiri Launcher authentication required';}}
else fetch('/api/panel/auth/exchange',{{method:'POST',credentials:'same-origin',
headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{code}})}})
.then(r=>{{if(!r.ok)throw new Error('authentication failed');return r.json()}})
.then(v=>{{sessionStorage.setItem('rumi-panel-csrf',v.data.csrf_token);location.replace({target_literal})}})
.catch(()=>{{document.body.textContent='Tobkiri Launcher authentication failed';}});
}});
</script>""".encode("utf-8")
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(document)))
            self.end_headers()
            self.wfile.write(document)
        except self._CLIENT_DISCONNECT_EXCEPTIONS:
            self.close_connection = True

    @staticmethod
    def _profile_registry_store() -> Any:
        """Return the Host-owned Named Profile registry for this process."""

        from .bootstrap.profile_capture import (
            host_profile_catalog,
            runtime_user_data_root,
        )
        from .profile_definition_store_v4 import ProfileDefinitionStore

        host_profile_catalog()
        return ProfileDefinitionStore(runtime_user_data_root())

    def _profile_registry_payload(self) -> dict[str, object]:
        """Project all Named Profiles and the separate active execution pointer."""

        from .active_profile_store_v4 import ActiveProfileStore
        from .bootstrap.profile_capture import (
            repair_legacy_active_profile_pointer,
            runtime_user_data_root,
        )

        store = self._profile_registry_store()
        state = store.snapshot()
        repair_legacy_active_profile_pointer()
        active = ActiveProfileStore(runtime_user_data_root()).load(verify_snapshot=True)
        return {
            "profile_registry_api_version": "io.tobkiri.profile-registry.v4",
            "generation": int(state["generation"]),
            "active_profile_id": active.profile_id if active is not None else None,
            "active_profile_revision": (active.profile_revision if active is not None else None),
            "profiles": store.list_profile_payloads(),
        }

    def _handle_profile_registry_read(self, path: str) -> None:
        if not self._check_auth("GET", path):
            self._send_response(APIResponse(False, error="Unauthorized"), 401)
            return
        try:
            self._send_mapping_result(self._profile_registry_payload())
        except Exception:
            logger.exception("Named Profile registry read failed")
            self._send_mapping_result(
                {"error": "Named Profile registry is unavailable", "status_code": 503}
            )

    def _handle_profile_registry_mutation(
        self,
        path: str,
        action: str,
        body: Mapping[str, object],
    ) -> None:
        if not self._check_auth("POST", path):
            self._send_response(APIResponse(False, error="Unauthorized"), 401)
            return
        allowed: dict[str, frozenset[str]] = {
            "create": frozenset(
                {
                    "profile_id",
                    "display_name",
                    "source_profile_id",
                    "expected_store_generation",
                }
            ),
            "update": frozenset(
                {
                    "profile_id",
                    "display_name",
                    "expected_profile_revision",
                    "expected_store_generation",
                }
            ),
            "duplicate": frozenset(
                {
                    "profile_id",
                    "new_profile_id",
                    "display_name",
                    "expected_profile_revision",
                    "expected_store_generation",
                }
            ),
            "delete": frozenset(
                {
                    "profile_id",
                    "expected_profile_revision",
                    "expected_store_generation",
                }
            ),
        }
        if action not in allowed or set(body) - allowed[action]:
            self._send_mapping_result(
                {"error": "Named Profile mutation shape is invalid", "status_code": 400}
            )
            return
        from .active_profile_store_v4 import ActiveProfileStore
        from .bootstrap.profile_capture import runtime_user_data_root
        from .profile_definition_store_v4 import (
            ProfileDefinitionNotFound,
            ProfileDefinitionStoreConflict,
        )

        try:
            store = self._profile_registry_store()
            generation = body.get("expected_store_generation")
            expected_generation = generation if isinstance(generation, int) else None
            profile_id = str(body.get("profile_id") or "")
            expected_revision = str(body.get("expected_profile_revision") or "") or None
            display_name = str(body.get("display_name") or "").strip() or None
            if action == "create":
                source_id = str(body.get("source_profile_id") or "")
                if not source_id:
                    raise ValueError("source_profile_id is required")
                source = store.get_profile(source_id)
                if source is None:
                    raise ProfileDefinitionNotFound(source_id)
                changed = store.create_profile(
                    source.profile,
                    profile_id=profile_id,
                    display_name=display_name,
                    expected_store_generation=expected_generation,
                )
            elif action == "update":
                changed = store.update_profile(
                    profile_id,
                    patch={"display_name": display_name or profile_id},
                    expected_profile_revision=expected_revision,
                    expected_store_generation=expected_generation,
                )
            elif action == "duplicate":
                changed = store.duplicate_profile(
                    profile_id,
                    new_profile_id=str(body.get("new_profile_id") or "") or None,
                    display_name=display_name,
                    expected_profile_revision=expected_revision,
                    expected_store_generation=expected_generation,
                )
            else:
                active = ActiveProfileStore(runtime_user_data_root()).load(verify_snapshot=True)
                if active is not None and active.profile_id == profile_id:
                    self._send_mapping_result(
                        {
                            "error": "The active execution Profile cannot be deleted",
                            "status_code": 409,
                        }
                    )
                    return
                changed = store.delete_profile(
                    profile_id,
                    expected_profile_revision=expected_revision,
                    expected_store_generation=expected_generation,
                )
            result = self._profile_registry_payload()
            result["changed_profile"] = changed.to_dict()
            result["action"] = action
            self._send_mapping_result(result)
        except ProfileDefinitionNotFound:
            self._send_mapping_result({"error": "Named Profile was not found", "status_code": 404})
        except ProfileDefinitionStoreConflict:
            self._send_mapping_result(
                {"error": "Named Profile revision is stale", "status_code": 409}
            )
        except (OSError, RuntimeError, ValueError):
            logger.exception("Named Profile registry mutation failed")
            self._send_mapping_result(
                {"error": "Named Profile mutation was rejected", "status_code": 400}
            )

    def do_OPTIONS(self) -> None:
        """Answer local panel preflight without widening the origin set."""

        self._reset_request_state()
        if self._is_retired_setup_complete_path():
            self._handle_retired_setup_complete()
            return
        origin = self._get_cors_origin(self.headers.get("Origin", ""))
        if not origin:
            self._send_response(APIResponse(False, error="Forbidden origin"), 403)
            return
        try:
            self.send_response(204)
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header(
                "Access-Control-Allow-Headers",
                "Content-Type, X-Rumi-CSRF, X-Rumi-Desktop-Bootstrap, X-Tobkiri-Request-ID",
            )
            self.send_header("Content-Length", "0")
            self.end_headers()
        except self._CLIENT_DISCONNECT_EXCEPTIONS:
            self.close_connection = True

    def do_GET(self) -> None:
        """Dispatch the finite read-only route set."""

        self._reset_request_state()
        path = urlparse(self.path).path
        if self._handle_packvm_lifecycle("GET", path):
            return
        if self._handle_contract_request("GET"):
            return
        if self._is_retired_setup_complete_path():
            self._handle_retired_setup_complete()
            return
        if path == "/health":
            self._handle_health()
            return
        if path == "/":
            self.send_response(302)
            self.send_header("Location", "/panel/")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if path == "/api/setup/status":
            self._handle_setup_status()
            return
        if path == "/api/setup/packs":
            if not self._setup_pre_auth_allowed() and not self._check_auth("GET", path):
                self._send_response(APIResponse(False, error="Unauthorized"), 401)
                return
            self._send_mapping_result(self._setup_list_packs())
            return
        if path == "/api/setup/migration/status":
            self._send_mapping_result(self._setup_get_migration_status())
            return
        if path == "/api/v4/profiles":
            self._handle_profile_registry_read(path)
            return
        mount = self._match_web_mount(path)
        if mount is not None:
            if mount["auth_required"] and not self._check_auth("GET", path):
                prefix = mount["path_prefix"]
                root_paths = {prefix, f"{prefix}/", f"{prefix}/{mount['index_file']}"}
                if mount.get("auth_bootstrap", False) and path in root_paths:
                    self._serve_mount_bootstrap_page(path, mount)
                else:
                    self._send_response(APIResponse(False, error="Unauthorized"), 401)
                return
            self._serve_static_file(path, mount)
            return
        if self._retired_api_path(path):
            self._send_retired_api(path)
            return
        self._send_not_found()

    def do_POST(self) -> None:
        """Dispatch canonical setup/auth and exact Broker operations."""

        self._reset_request_state()
        path = urlparse(self.path).path
        if self._handle_packvm_lifecycle("POST", path):
            return
        if self._handle_contract_request("POST"):
            return
        if self._is_retired_setup_complete_path():
            self._handle_retired_setup_complete()
            return
        if path == "/api/panel/auth/bootstrap":
            self._handle_panel_bootstrap()
            return
        if path == "/api/panel/auth/exchange":
            body = self._parse_object_body()
            if body is not None:
                self._handle_panel_exchange(body)
            return
        if path == "/api/setup/runtime/reconcile":
            if not self._check_auth("POST", path):
                self._discard_request_body()
                self._send_response(APIResponse(False, error="Unauthorized"), 401)
                return
            self._discard_request_body()
            refresh = self.__class__._runtime_refresh
            lifecycle = self.__class__.app_lifecycle_manager
            if refresh is None or lifecycle is None:
                self._send_mapping_result({
                    "error": "Canonical runtime reconciliation is unavailable",
                    "status_code": 503,
                    "state": "runtime_unavailable",
                })
                return
            try:
                refresh(None)
                health = lifecycle.get_health()
            except Exception as error:
                from .app_lifecycle_manager import mark_runtime_failed

                mark_runtime_failed("canonical runtime capture failed")
                logger.warning(
                    "Canonical runtime reconciliation failed",
                    exc_info=error,
                )
                self._send_mapping_result({
                    "error": "Canonical runtime reconciliation failed",
                    "status_code": 503,
                    "state": "runtime_unavailable",
                })
                return
            self._send_mapping_result({
                "state": health.get("runtime_status", "starting"),
                "runtime_ready": health.get("runtime_ready", False),
            })
            return
        if path == "/api/setup/packs/install":
            if not self._setup_pre_auth_allowed() and not self._check_auth("POST", path):
                self._discard_request_body()
                self._send_response(APIResponse(False, error="Unauthorized"), 401)
                return
            body = self._parse_object_body()
            if body is not None:
                result = self._setup_install_pack(body)
                try:
                    self._send_mapping_result(result)
                finally:
                    # ``_send_response`` flushes its complete envelope. Once
                    # activation committed, a later disconnect must not keep
                    # this stale HostProfileControl process alive.
                    self._refresh_setup_runtime_after_response(result)
            return
        profile_action = {
            "/api/v4/profiles/create": "create",
            "/api/v4/profiles/update": "update",
            "/api/v4/profiles/duplicate": "duplicate",
            "/api/v4/profiles/delete": "delete",
        }.get(path)
        if profile_action is not None:
            body = self._parse_object_body()
            if body is not None:
                self._handle_profile_registry_mutation(path, profile_action, body)
            return
        if path == "/api/v4/dispatch":
            self._discard_request_body()
            self._send_retired_api(path)
            return
        if self._retired_api_path(path):
            self._discard_request_body()
            self._send_retired_api(path)
            return
        self._discard_request_body()
        self._send_not_found()

    def do_PUT(self) -> None:
        """Retire historical mutation routes without parsing their payloads."""

        self._reset_request_state()
        path = urlparse(self.path).path
        if self._handle_contract_request("PUT"):
            return
        if self._is_retired_setup_complete_path():
            self._handle_retired_setup_complete()
            return
        self._discard_request_body()
        if self._retired_api_path(path):
            self._send_retired_api(path)
        else:
            self._send_not_found()

    def do_DELETE(self) -> None:
        """Retire historical deletion routes without manager access."""
        self._reset_request_state()
        if self._handle_contract_request("DELETE"):
            return
        self.do_PUT()

    def do_PATCH(self) -> None:
        """Retire historical partial mutations without manager access."""
        self._reset_request_state()
        if self._handle_contract_request("PATCH"):
            return
        self.do_PUT()

    def do_HEAD(self) -> None:
        """Expose standard header-only semantics for the retired exact path."""

        self._reset_request_state()
        if self._is_retired_setup_complete_path():
            self._handle_retired_setup_complete(head_only=True)
            return
        self._send_not_found()


class PackAPIServer:
    """Own one verified loopback HTTP server and captured v4 handler."""

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 8765,
        *,
        panel_auth_manager: PanelAuthManager | None = None,
        dispatch_session: DispatchSession | None = None,
        app_lifecycle_manager: LifecyclePort | None = None,
        contract_bindings: tuple[HTTPContractBinding, ...] = (),
        runtime_capture_factory: RuntimeCaptureFactory | None = None,
        capability_snapshot_factory: CapabilitySnapshotFactory | None = None,
        application_presentation: HTTPApplicationPresentation | None = None,
        web_mounts: tuple[WebMountEntry, ...] | None = None,
        workspace_binding_resolver: WorkspaceBindingResolver | None = None,
        packvm_lifecycle: PackVMLifecyclePort | None = None,
        host_contract: Mapping[str, Any] | None = None,
    ) -> None:
        self.config = RuntimeHTTPConfig.verify(host, port)
        self.host = self.config.host
        self.port = self.config.port
        self._panel_auth_manager = panel_auth_manager or get_panel_auth_manager()
        self._dispatch_session = dispatch_session
        # A caller-provided session remains caller-owned.  Refreshes that the
        # server captures itself become server-owned so their Broker,
        # authority store, and provider close callbacks have a bounded owner.
        self._dispatch_session_owned_by_server = False
        self.app_lifecycle_manager = app_lifecycle_manager
        self._contract_routes = contract_binding_map(contract_bindings)
        self._runtime_capture_factory = runtime_capture_factory
        self._capability_snapshot_factory = capability_snapshot_factory
        self._application_presentation = application_presentation
        self._web_mounts = web_mounts
        self._workspace_binding_resolver = workspace_binding_resolver
        self._host_contract_snapshot = (
            validate_host_contract(host_contract) if host_contract is not None else None
        )
        self._packvm_lifecycle = packvm_lifecycle
        self._replay_guard = _RequestReplayGuard()
        from .bootstrap.profile_capture import runtime_user_data_root

        self._operation_journal = ControlReconciliationStore(
            runtime_user_data_root() / "control" / "reconciliation-v4.sqlite3",
            instance_id=str(uuid.uuid4()),
        )
        self.server: _PackThreadingHTTPServer | None = None
        self.thread: threading.Thread | None = None
        self.handler_class: type[PackAPIHandler] | None = None
        self._lifecycle_lock = threading.RLock()
        self._lifecycle_generation = 0
        self._runtime_refresh_sequence = 0
        self._lifecycle_state = "stopped"
        self._stop_complete = threading.Event()
        self._stop_complete.set()
        self._stop_failed = False

    def start(self) -> None:
        """Start a fresh finite handler with no inherited route state."""

        with self._lifecycle_lock:
            if self._lifecycle_state == "stopping":
                raise RuntimeError("Pack v4 API server is stopping")
            if self._lifecycle_state == "drain_failed":
                raise RuntimeError("Pack v4 API server teardown is incomplete")
            if self.is_running():
                return
            self._lifecycle_generation += 1
            lifecycle_generation = self._lifecycle_generation
            self._lifecycle_state = "starting"
            self._stop_complete.clear()
            self._stop_failed = False
            try:
                self._validate_contract_runtime()
                handler = PackAPIHandler.canonical_v4_server_handler(
                    panel_auth_manager=self._panel_auth_manager,
                    dispatch_session=self._dispatch_session,
                    app_lifecycle_manager=self.app_lifecycle_manager,
                    contract_routes=self._contract_routes,
                    capability_snapshot_factory=self._capability_snapshot_factory,
                    application_presentation=self._application_presentation,
                    replay_guard=self._replay_guard,
                    operation_journal=self._operation_journal,
                    web_mounts=self._web_mounts,
                    runtime_refresh=self._runtime_refresh_callback(lifecycle_generation),
                    workspace_binding_resolver=self._workspace_binding_resolver,
                    packvm_lifecycle=self._packvm_lifecycle,
                    host_contract=self._host_contract_snapshot,
                )
                server = _PackThreadingHTTPServer((self.host, self.port), handler)
            except Exception:
                self._lifecycle_state = "stopped"
                self._stop_complete.set()
                raise
            actual_port = int(server.server_address[1])
            handler._runtime_port = actual_port
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            self.port = actual_port
            self.handler_class = handler
            self.server = server
            self.thread = thread
            self._lifecycle_state = "running"
            thread.start()
        logger.info("Pack v4 API server started on http://%s:%s", self.host, self.port)

    def issue_panel_login_code(self) -> Mapping[str, object]:
        """Issue a desktop handoff code bound to the current server capture."""

        with self._lifecycle_lock:
            handler = self.handler_class
            if self._lifecycle_state != "running" or handler is None:
                raise RuntimeError("Pack v4 API server is not running")
            binding = handler._current_panel_auth_binding()
            if binding is None:
                raise RuntimeError("current panel authentication capture is unavailable")
            return self._panel_auth_manager.issue_login_code(binding)

    def _runtime_refresh_callback(
        self,
        lifecycle_generation: int,
    ) -> Callable[[DispatchSession | None], None]:
        """Bind handler publication to the lifecycle generation that created it."""

        refresh_method = self._refresh_runtime_capture

        def refresh(session: DispatchSession | None = None) -> None:
            if getattr(refresh_method, "__func__", None) is PackAPIServer._refresh_runtime_capture:
                refresh_method(
                    session,
                    lifecycle_generation=lifecycle_generation,
                )
                return
            with self._lifecycle_lock:
                if (
                    self._lifecycle_state != "running"
                    or lifecycle_generation != self._lifecycle_generation
                ):
                    return
            override_refresh = cast(Callable[[DispatchSession | None], None], refresh_method)
            override_refresh(session)

        return refresh

    def _validate_contract_runtime(self) -> None:
        """Verify the exact capture and route ownership before binding a socket."""

        self._host_contract_snapshot = self._validate_contract_capture(
            self._dispatch_session,
            self._contract_routes,
        )

    def _validate_contract_capture(
        self,
        session: DispatchSession | None,
        routes: Mapping[tuple[str, str], HTTPContractBinding],
        *,
        host_contract: object = _HOST_CONTRACT_UNSET,
    ) -> Mapping[str, Any] | None:
        """Validate a complete session/map pair before publishing either value."""

        snapshot = (
            getattr(self, "_host_contract_snapshot", None)
            if host_contract is _HOST_CONTRACT_UNSET
            else host_contract
        )
        if snapshot is not None and not isinstance(snapshot, Mapping):
            raise RuntimeError("Host contract snapshot is invalid")
        if not routes:
            if snapshot is None:
                return None
            expected_session_identity: DispatchSession | None = None
            if session is not None:
                session.assert_current()
                if getattr(session, "session_kind", None) != "host_profile_control":
                    expected_session_identity = session
            try:
                return validate_host_contract(
                    snapshot,
                    expected_identity=expected_session_identity,
                )
            except HostContractError as error:
                raise RuntimeError("Host contract snapshot is invalid") from error
        if session is None:
            raise RuntimeError("frontend contracts require a captured v4 session")
        session.assert_current()
        host_profile_control = getattr(session, "session_kind", None) == "host_profile_control"
        expected_identity: DispatchSession | None = None if host_profile_control else session
        try:
            if snapshot is None and not host_profile_control:
                snapshot = capture_host_contract(expected_identity=expected_identity)
            elif snapshot is not None:
                snapshot = validate_host_contract(
                    snapshot,
                    expected_identity=expected_identity,
                )
        except HostContractError as error:
            raise RuntimeError(
                "the captured execution requires a Host contract bound to the capture"
            ) from error
        if host_profile_control:
            if (
                getattr(session, "execution_profile_id", object()) is not None
                or not str(getattr(session, "principal_id", ""))
                or not session.plan_digest.startswith("sha256:")
            ):
                raise RuntimeError("Host Profile control identity is invalid")
        elif (
            not session.profile_id
            or not session.profile_revision.startswith("sha256:")
            or not session.activation_id.strip()
            or not session.plan_digest.startswith("sha256:")
        ):
            raise RuntimeError("frontend contracts require an exact active Profile")
        for binding in routes.values():
            for target in binding.targets:
                providers = session.provider_metadata(target.contract_id)
                exact = tuple(
                    provider
                    for provider in providers
                    if provider.get("provider_id") == target.provider_id
                    and provider.get("operation_id") == target.operation_id
                    and (
                        (
                            host_profile_control
                            and provider.get("principal_id")
                            == getattr(session, "principal_id", None)
                            and provider.get("execution_profile_id") is None
                            and provider.get("catalog_digest") == session.plan_digest
                        )
                        or (
                            not host_profile_control
                            and provider.get("profile_id") == session.profile_id
                            and provider.get("profile_revision") == session.profile_revision
                            and provider.get("activation_id") == session.activation_id
                            and provider.get("plan_digest") == session.plan_digest
                        )
                    )
                )
                if len(exact) != 1 or target.function_id != target.provider_id:
                    raise RuntimeError("frontend contract Provider identity is unavailable")
                presentation = self._application_presentation
                if presentation is None or presentation.requires_operation_ready(target):
                    session.assert_operation_ready(
                        target.contract_id,
                        target.operation_id,
                    )
        if self._web_mounts is not None:
            for mount in self._web_mounts:
                root = mount["web_root"]
                if not root.is_dir() or not (root / mount["index_file"]).is_file():
                    raise RuntimeError("frontend contract web mount is unavailable")
        return snapshot

    def _refresh_runtime_capture(
        self,
        activated_session: DispatchSession | None = None,
        *,
        lifecycle_generation: int,
    ) -> None:
        """Atomically publish a current Broker session and canonical route map."""

        with self._lifecycle_lock:
            if (
                self._lifecycle_state != "running"
                or lifecycle_generation != self._lifecycle_generation
            ):
                return
            self._runtime_refresh_sequence += 1
            refresh_sequence = self._runtime_refresh_sequence
            base_session = self._dispatch_session

        from tobkiri_host.runtime import install_dispatch_session

        from .authority.v4 import AuthorityStore
        from .bootstrap.production_v4 import capture_production_dispatch
        from .bootstrap.profile_capture import (
            capture_active_profile,
            runtime_user_data_root,
        )
        from .di_container import get_container

        session = activated_session
        server_captured_session = session is None
        host_contract = getattr(self, "_host_contract_snapshot", None)
        try:
            factory = self._runtime_capture_factory
            if factory is None:
                raise RuntimeError("application runtime capture composition is unavailable")
            if session is None:
                active = capture_active_profile()
                inputs = factory(active)
                authority = AuthorityStore(runtime_user_data_root() / "authority" / "v4.sqlite3")
                try:
                    session = capture_production_dispatch(
                        active,
                        bundle_root=inputs.bundle_root,
                        ecosystem_root=inputs.ecosystem_root,
                        authority_store=authority,
                        packvm_provisioner=inputs.packvm_backend_factory,
                        packvm_readiness_reader=(
                            self._packvm_lifecycle.readiness_snapshot
                            if self._packvm_lifecycle is not None
                            else None
                        ),
                        http_contract_bindings=inputs.contract_bindings,
                        activation_snapshot_loader=inputs.activation_snapshot_loader,
                        runtime_surface_factory=inputs.runtime_surface_factory,
                        capability_binding_snapshot_factory=(
                            inputs.capability_binding_snapshot_factory
                        ),
                        capability_binding_selector=inputs.capability_binding_selector,
                        credential_store_factory=inputs.credential_store_factory,
                    )
                except Exception:
                    authority.close()
                    raise
            else:
                inputs = factory()
            routes = contract_binding_map(inputs.contract_bindings)
            if routes:
                host_profile_control = (
                    getattr(session, "session_kind", None) == "host_profile_control"
                )
                expected_identity = None if host_profile_control else session
                if not host_profile_control and os.getenv("TOBKIRI_HOST_CONTRACT_PATH", "").strip():
                    # A lifecycle refresh is an explicit authority boundary:
                    # capture the Launcher-published replacement once, then
                    # bind the resulting snapshot to the new handler.
                    host_contract = capture_host_contract_from_file(
                        expected_identity=expected_identity
                    )
                elif not host_profile_control and host_contract is None:
                    host_contract = capture_host_contract_from_file(
                        expected_identity=expected_identity
                    )
                elif host_contract is not None:
                    host_contract = validate_host_contract(
                        host_contract,
                        expected_identity=expected_identity,
                    )
            host_contract = self._validate_contract_capture(
                session,
                routes,
                host_contract=host_contract,
            )
        except Exception:
            self._close_unpublished_session(session, base_session=base_session)
            raise

        previous: DispatchSession | None = None
        with self._lifecycle_lock:
            stale_refresh = (
                self._lifecycle_state != "running"
                or lifecycle_generation != self._lifecycle_generation
                or refresh_sequence != self._runtime_refresh_sequence
            )
            if not stale_refresh:
                previous = self._dispatch_session
                published_generation = self._lifecycle_generation
                handler = PackAPIHandler.canonical_v4_server_handler(
                    panel_auth_manager=self._panel_auth_manager,
                    dispatch_session=session,
                    app_lifecycle_manager=self.app_lifecycle_manager,
                    contract_routes=routes,
                    capability_snapshot_factory=self._capability_snapshot_factory,
                    application_presentation=self._application_presentation,
                    replay_guard=self._replay_guard,
                    operation_journal=self._operation_journal,
                    web_mounts=self._web_mounts,
                    runtime_refresh=self._runtime_refresh_callback(published_generation),
                    workspace_binding_resolver=self._workspace_binding_resolver,
                    packvm_lifecycle=self._packvm_lifecycle,
                    host_contract=host_contract,
                )
                handler._runtime_port = self.port
                self._dispatch_session = session
                self._dispatch_session_owned_by_server = server_captured_session
                self._contract_routes = routes
                self._host_contract_snapshot = host_contract
                self.handler_class = handler
                if self.server is not None:
                    self.server.RequestHandlerClass = handler
                install_dispatch_session(get_container(), session)
        if stale_refresh:
            self._close_unpublished_session(session, base_session=base_session)
            return
        if previous is not None and previous is not session:
            close = getattr(previous, "close", None)
            if callable(close):
                close()
        from .app_lifecycle_manager import mark_runtime_ready

        mark_runtime_ready()

    @staticmethod
    def _close_unpublished_session(
        session: DispatchSession | None,
        *,
        base_session: DispatchSession | None,
    ) -> None:
        """Close a discarded candidate without touching the captured base session."""

        if session is None or session is base_session:
            return
        close = getattr(session, "close", None)
        if callable(close):
            try:
                close()
            except Exception:
                logger.exception("failed to close an unpublished dispatch session")

    def stop(self) -> None:
        """Stop the server and discard its captured handler bindings."""

        owned_dispatch_session: DispatchSession | None = None

        with self._lifecycle_lock:
            if self._lifecycle_state == "stopped":
                return
            if self._lifecycle_state == "stopping":
                stop_complete = self._stop_complete
                owns_stop = False
                server = None
                thread = None
            else:
                self._lifecycle_state = "stopping"
                self._lifecycle_generation += 1
                self._stop_complete.clear()
                self._stop_failed = False
                stop_complete = self._stop_complete
                owns_stop = True
                server = self.server
                thread = self.thread
                if server is not None:
                    server.stop_accepting_requests()
                dispatch_session = self._dispatch_session
                cancel_pending_reads = getattr(
                    dispatch_session,
                    "cancel_pending_reads",
                    None,
                )
                if callable(cancel_pending_reads):
                    cancel_pending_reads()

        if not owns_stop:
            if not stop_complete.wait(timeout=THREAD_JOIN_TIMEOUT_SECONDS):
                raise RuntimeError("Pack v4 API server stop timed out")
            if self._stop_failed:
                raise RuntimeError("Pack v4 API server teardown incomplete")
            return

        deadline = time.monotonic() + THREAD_JOIN_TIMEOUT_SECONDS
        if server is not None:
            server.request_shutdown()
        if thread is not None:
            thread.join(timeout=max(0.0, deadline - time.monotonic()))
        serving_thread_alive = thread is not None and thread.is_alive()
        drained = True
        diagnostics: dict[str, object] = {
            "serving_thread_alive": serving_thread_alive,
        }
        if server is not None:
            drained = server.wait_for_request_drain(max(0.0, deadline - time.monotonic()))
            diagnostics.update(server.teardown_snapshot())

        with self._lifecycle_lock:
            if drained and not serving_thread_alive:
                if server is not None:
                    server.close_handler_slots()
                    server.server_close()
                self._operation_journal.close()
                if self.server is server:
                    self.server = None
                if self.thread is thread:
                    self.thread = None
                self.handler_class = None
                if self._dispatch_session_owned_by_server:
                    owned_dispatch_session = self._dispatch_session
                    self._dispatch_session = None
                    self._dispatch_session_owned_by_server = False
                self._lifecycle_state = "stopped"
                self._stop_failed = False
                self._stop_complete.set()
            else:
                self._lifecycle_state = "drain_failed"
                self._stop_failed = True
                self._stop_complete.set()

        if not drained or serving_thread_alive:
            logger.error("Pack v4 API server teardown incomplete: %s", diagnostics)
            raise RuntimeError(f"Pack v4 API server teardown incomplete: {diagnostics}")
        if owned_dispatch_session is not None:
            close = getattr(owned_dispatch_session, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:
                    logger.exception("failed to close server-owned dispatch session")
        logger.info("Pack v4 API server stopped")

    def is_running(self) -> bool:
        """Return whether the serving thread is alive."""

        return self.server is not None and self.thread is not None and self.thread.is_alive()


_api_server: PackAPIServer | None = None


def get_pack_api_server() -> PackAPIServer | None:
    """Return the process-local Pack v4 HTTP server, if started."""

    return _api_server


def initialize_pack_api_server(
    host: str = "127.0.0.1",
    port: int = 8765,
    *,
    panel_auth_manager: PanelAuthManager | None = None,
    dispatch_session: DispatchSession | None = None,
    app_lifecycle_manager: LifecyclePort | None = None,
    contract_bindings: tuple[HTTPContractBinding, ...] = (),
    runtime_capture_factory: RuntimeCaptureFactory | None = None,
    capability_snapshot_factory: CapabilitySnapshotFactory | None = None,
    application_presentation: HTTPApplicationPresentation | None = None,
    web_mounts: tuple[WebMountEntry, ...] | None = None,
    workspace_binding_resolver: WorkspaceBindingResolver | None = None,
    packvm_lifecycle: PackVMLifecyclePort | None = None,
    host_contract: Mapping[str, Any] | None = None,
) -> PackAPIServer:
    """Replace the process-local server with one verified v4 instance."""

    global _api_server
    if _api_server is not None:
        _api_server.stop()
    server = PackAPIServer(
        host=host,
        port=port,
        panel_auth_manager=panel_auth_manager,
        dispatch_session=dispatch_session,
        app_lifecycle_manager=app_lifecycle_manager,
        contract_bindings=contract_bindings,
        runtime_capture_factory=runtime_capture_factory,
        capability_snapshot_factory=capability_snapshot_factory,
        application_presentation=application_presentation,
        web_mounts=web_mounts,
        workspace_binding_resolver=workspace_binding_resolver,
        packvm_lifecycle=packvm_lifecycle,
        host_contract=host_contract,
    )
    server.start()
    _api_server = server
    return server


def shutdown_pack_api_server() -> None:
    """Stop and forget the process-local Pack v4 HTTP server."""

    global _api_server
    if _api_server is not None:
        _api_server.stop()
        _api_server = None


__all__ = [
    "PackAPIHandler",
    "PackAPIServer",
    "RuntimeHTTPConfig",
    "get_pack_api_server",
    "initialize_pack_api_server",
    "shutdown_pack_api_server",
]
