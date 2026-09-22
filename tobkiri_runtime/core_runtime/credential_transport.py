"""Host-bound credential application at the provider transport boundary.

Normal Pack and generic process contracts carry only opaque credential handles.
This module is intentionally Host-owned: a backend constructs one adapter from
an already-authorized :class:`~tobkiri_host.broker.RequestEnvelope`, and the
adapter consumes its internal credential lease exactly once while constructing
the outbound request.  Decrypted material never becomes a Pack response.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from email.message import Message
import http.client
import ipaddress
import json
import math
from pathlib import Path
import socket
import ssl
from threading import RLock
import time
from typing import Any, Protocol
import urllib.error
import urllib.parse
import urllib.request

from core_runtime.authority.v4 import (
    AuthorityStore,
    FunctionPrincipal,
    LeaseState,
)
from tobkiri_host.broker import RequestEnvelope


class CredentialTransportDenied(PermissionError):
    """Uniform denial returned for every credential-binding failure."""

    _SAFE_CODES = frozenset(
        {
            "audit_failure",
            "binding_invalid",
            "material_invalid",
            "provider_failure",
            "response_invalid",
            "store_failure",
        }
    )

    def __init__(self, code: str = "binding_invalid") -> None:
        """Create a fixed, material-independent public denial."""

        self.code = code if code in self._SAFE_CODES else "binding_invalid"
        super().__init__(f"credential transport denied ({self.code})")


class JsonResponse(Protocol):
    """Small response protocol used by the Host HTTP adapter."""

    def __enter__(self) -> "JsonResponse": ...

    def __exit__(self, *args: object) -> None: ...

    def read(self, amount: int | None = None) -> bytes: ...


_MAX_RESPONSE_BYTES = 4 * 1024 * 1024
_MAX_RESPONSE_DEPTH = 32
_PROVIDER_RESPONSE_FIELDS = frozenset(
    {
        "arguments",
        "b64_json",
        "choices",
        "completion_tokens",
        "content",
        "created",
        "data",
        "embedding",
        "finish_reason",
        "function",
        "id",
        "index",
        "input_tokens",
        "logprobs",
        "message",
        "model",
        "name",
        "object",
        "output_tokens",
        "prompt_tokens",
        "revised_prompt",
        "role",
        "stop_reason",
        "system_fingerprint",
        "text",
        "tool_calls",
        "total_tokens",
        "type",
        "url",
        "usage",
    }
)


class CredentialMaterialStore(Protocol):
    """Host-injected credential capability; no concrete Pack import is allowed."""

    def resolve(
        self,
        handle: str,
        *,
        consumer_pack_id: str,
        provider_instance_id: str,
        scope: str,
        profile_id: str,
        key_version: str = "",
        purpose: str = "provider.invoke",
    ) -> dict[str, Any]:
        """Resolve material only inside the Host transport boundary."""


@dataclass(frozen=True)
class CredentialMaterialStoreBinding:
    """One Host credential store and its exact opaque key-version binding."""

    store: CredentialMaterialStore
    key_version: str

    def __post_init__(self) -> None:
        """Reject an incomplete factory result before provider dispatch."""
        if not callable(getattr(self.store, "resolve", None)) or not _safe_text(
            self.key_version
        ):
            raise ValueError("credential material store binding is invalid")


class CredentialMaterialStoreFactory(Protocol):
    """Typed composition-root factory for one Host credential store."""

    def __call__(
        self,
        *,
        user_data_root: Path,
    ) -> CredentialMaterialStoreBinding:
        """Create a store binding for the captured Host user-data root."""


@dataclass(frozen=True)
class CredentialTransportBinding:
    """Exact Host-captured identity and credential scope for one request."""

    profile_id: str
    activation_id: str
    security_epoch: int
    caller_principal_id: str
    provider_principal_id: str
    provider_function_id: str
    operation_id: str
    target_domain_id: str
    target_boot_epoch: int
    request_id: str
    request_digest: str
    credential_handle: str
    credential_key_version: str
    provider_instance_id: str
    credential_scope: str
    credential_purpose: str
    endpoint_origin: str
    consumer_pack_id: str = "rumi_provider_adapters_pack"

    def __post_init__(self) -> None:
        """Reject incomplete or non-opaque bindings before lease creation."""
        text_fields = (
            "profile_id",
            "activation_id",
            "caller_principal_id",
            "provider_principal_id",
            "provider_function_id",
            "operation_id",
            "target_domain_id",
            "request_id",
            "request_digest",
            "credential_handle",
            "credential_key_version",
            "provider_instance_id",
            "credential_scope",
            "credential_purpose",
            "endpoint_origin",
            "consumer_pack_id",
        )
        if any(not _safe_text(getattr(self, field)) for field in text_fields):
            raise ValueError("credential transport binding is incomplete")
        if not self.credential_handle.startswith(("credential:", "opaque:")):
            raise ValueError("credential transport requires an opaque handle")
        if self.security_epoch < 1 or self.target_boot_epoch < 1:
            raise ValueError("credential transport epoch is invalid")
        if _credential_origin(self.endpoint_origin) != self.endpoint_origin:
            raise ValueError("credential transport requires a canonical HTTPS origin")


class HostBoundCredentialTransport:
    """Single-use Host adapter that resolves and applies one credential."""

    def __init__(
        self,
        *,
        store: CredentialMaterialStore,
        authority_store: AuthorityStore,
        invocation_token: str,
        binding: CredentialTransportBinding,
        current_security_epoch: Callable[[], int],
        audit_sink: Callable[[Mapping[str, Any]], None] | None = None,
        clock: Callable[[], float] = time.time,
        monotonic_clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._store = store
        self._authority_store = authority_store
        self._invocation_token = invocation_token
        self._binding = binding
        self._current_security_epoch = current_security_epoch
        self._opener = _open_pinned_request
        self._audit_sink = audit_sink
        self._clock = clock
        self._monotonic_clock = monotonic_clock
        self._consumed = False
        self._lock = RLock()

    @classmethod
    def from_authorized_envelope(
        cls,
        envelope: RequestEnvelope,
        *,
        provider_principal: FunctionPrincipal,
        store: CredentialMaterialStore,
        authority_store: AuthorityStore,
        credential_handle: str,
        credential_key_version: str,
        provider_instance_id: str,
        credential_scope: str,
        credential_purpose: str,
        endpoint_origin: str,
        current_security_epoch: Callable[[], int],
        consumer_pack_id: str = "rumi_provider_adapters_pack",
        audit_sink: Callable[[Mapping[str, Any]], None] | None = None,
        clock: Callable[[], float] = time.time,
        monotonic_clock: Callable[[], float] = time.monotonic,
    ) -> "HostBoundCredentialTransport":
        """Capture a transport lease from the Broker-authenticated envelope."""
        bound_origin = _credential_origin(endpoint_origin)
        if not bound_origin:
            raise CredentialTransportDenied("credential transport denied")
        context = envelope.context
        try:
            invocation_token = envelope.lease.token.decode("ascii")
            durable, lease_state = authority_store.inspect_lease_token(invocation_token)
        except Exception:
            raise CredentialTransportDenied("credential transport denied") from None
        if (
            lease_state is not LeaseState.DISPATCHED
            or envelope.target_principal.value != provider_principal.principal_id
            or envelope.operation_id != provider_principal.operation_id
            or envelope.target_domain.value != context.target_domain_id
            or durable.caller.principal_id != context.caller_principal.value
            or durable.target != provider_principal
            or durable.profile_id != context.profile_id
            or durable.activation_id != context.activation_id
            or durable.security_epoch != context.security_epoch
            or durable.target_domain_id != context.target_domain_id
            or durable.target_boot_epoch != context.target_boot_epoch
            or durable.request_id != context.request_id
            or durable.request_digest != envelope.request_digest
        ):
            raise CredentialTransportDenied("credential transport denied")
        binding = CredentialTransportBinding(
            profile_id=context.profile_id,
            activation_id=context.activation_id,
            security_epoch=context.security_epoch,
            caller_principal_id=context.caller_principal.value,
            provider_principal_id=provider_principal.principal_id,
            provider_function_id=provider_principal.function_id,
            operation_id=envelope.operation_id,
            target_domain_id=context.target_domain_id,
            target_boot_epoch=context.target_boot_epoch,
            request_id=context.request_id,
            request_digest=envelope.request_digest,
            credential_handle=credential_handle,
            credential_key_version=credential_key_version,
            provider_instance_id=provider_instance_id,
            credential_scope=credential_scope,
            credential_purpose=credential_purpose,
            endpoint_origin=bound_origin,
            consumer_pack_id=consumer_pack_id,
        )
        return cls(
            store=store,
            authority_store=authority_store,
            invocation_token=invocation_token,
            binding=binding,
            current_security_epoch=current_security_epoch,
            audit_sink=audit_sink,
            clock=clock,
            monotonic_clock=monotonic_clock,
        )

    @property
    def binding(self) -> CredentialTransportBinding:
        """Expose only non-secret binding evidence for diagnostics and tests."""
        return self._binding

    def post_json(
        self,
        *,
        endpoint: str,
        headers: Mapping[str, str],
        body: Mapping[str, Any],
        credential_handle: str,
        provider_instance_id: str,
        credential_scope: str,
        credential_scheme: str,
        deadline: float,
    ) -> dict[str, Any]:
        """Run one request while severing every internal exception chain."""

        denial_code = "provider_failure"
        try:
            return self._post_json(
                endpoint=endpoint,
                headers=headers,
                body=body,
                credential_handle=credential_handle,
                provider_instance_id=provider_instance_id,
                credential_scope=credential_scope,
                credential_scheme=credential_scheme,
                deadline=deadline,
            )
        except CredentialTransportDenied as error:
            denial_code = error.code
        except Exception:
            denial_code = "provider_failure"
        raise CredentialTransportDenied(denial_code)

    def _post_json(
        self,
        *,
        endpoint: str,
        headers: Mapping[str, str],
        body: Mapping[str, Any],
        credential_handle: str,
        provider_instance_id: str,
        credential_scope: str,
        credential_scheme: str,
        deadline: float,
    ) -> dict[str, Any]:
        """Consume the sealed lease and perform one credentialed HTTP request."""
        now = self._clock()
        try:
            remaining = float(deadline) - now
        except (TypeError, ValueError, OverflowError):
            raise CredentialTransportDenied("binding_invalid") from None
        if not math.isfinite(remaining) or remaining <= 0:
            raise CredentialTransportDenied("binding_invalid")
        initial_remaining = remaining
        try:
            deadline_started = float(self._monotonic_clock())
        except (TypeError, ValueError, OverflowError):
            raise CredentialTransportDenied("binding_invalid") from None
        if not math.isfinite(deadline_started):
            raise CredentialTransportDenied("binding_invalid")
        self._consume_once(
            endpoint=endpoint,
            credential_handle=credential_handle,
            provider_instance_id=provider_instance_id,
            credential_scope=credential_scope,
        )
        material: dict[str, Any] | None = None
        secret_bytes: bytearray | None = None
        secret_text = ""
        audit_status = "failed"
        try:
            try:
                material = self._store.resolve(
                    self._binding.credential_handle,
                    consumer_pack_id=self._binding.consumer_pack_id,
                    provider_instance_id=self._binding.provider_instance_id,
                    profile_id=self._binding.profile_id,
                    scope=self._binding.credential_scope,
                    key_version=self._binding.credential_key_version,
                    purpose=self._binding.credential_purpose,
                )
            except Exception:
                raise CredentialTransportDenied("store_failure") from None
            if not isinstance(material, dict):
                raise CredentialTransportDenied("material_invalid")
            value = material.get("api_key") or material.get("token")
            if not isinstance(value, str) or not value:
                raise CredentialTransportDenied("material_invalid")
            encoded_body = json.dumps(body, ensure_ascii=False).encode("utf-8")
            _remaining_deadline_budget(
                initial_remaining=initial_remaining,
                started=deadline_started,
                clock=self._monotonic_clock,
            )
            secret_bytes = bytearray(value.encode("utf-8"))
            secret_text = secret_bytes.decode("utf-8")
            outbound_headers = {
                key: item
                for key, item in headers.items()
                if key.lower() not in {"authorization", "host", "x-api-key"}
            }
            if credential_scheme == "bearer":
                outbound_headers["Authorization"] = f"Bearer {secret_text}"
            elif credential_scheme == "anthropic":
                outbound_headers["x-api-key"] = secret_text
            else:
                raise CredentialTransportDenied("credential transport denied")
            request = urllib.request.Request(
                endpoint,
                data=encoded_body,
                headers=outbound_headers,
                method="POST",
            )
            remaining = _remaining_deadline_budget(
                initial_remaining=initial_remaining,
                started=deadline_started,
                clock=self._monotonic_clock,
            )
            timeout = min(60.0, remaining)
            with self._opener(request, timeout=timeout) as response:
                response_bytes = response.read(_MAX_RESPONSE_BYTES + 1)
                if len(response_bytes) > _MAX_RESPONSE_BYTES:
                    raise CredentialTransportDenied("response_invalid")
                value = json.loads(response_bytes.decode("utf-8"))
            if not isinstance(value, dict):
                raise CredentialTransportDenied("response_invalid")
            sanitized = _sanitize_provider_response(value, secret_text)
            audit_status = "completed"
            return sanitized
        except CredentialTransportDenied:
            audit_status = "denied"
            raise
        except urllib.error.HTTPError as exc:
            if 300 <= exc.code < 400:
                raise CredentialTransportDenied("credential transport denied") from None
            code = "quota" if exc.code == 429 else "provider_unavailable"
            raise RuntimeError(f"{code}: provider HTTP {exc.code}") from None
        except (KeyError, PermissionError, UnicodeError, ValueError):
            raise CredentialTransportDenied("response_invalid") from None
        except Exception:
            raise CredentialTransportDenied("provider_failure") from None
        finally:
            _clear_material(material)
            if secret_bytes is not None:
                secret_bytes[:] = b"\x00" * len(secret_bytes)
            self._audit(
                status=audit_status,
                endpoint_origin=_origin(endpoint),
            )

    def _consume_once(
        self,
        *,
        endpoint: str,
        credential_handle: str,
        provider_instance_id: str,
        credential_scope: str,
    ) -> None:
        binding = self._binding
        valid = (
            self._current_security_epoch() == binding.security_epoch
            and self._authority_still_active()
            and _credential_origin(endpoint) == binding.endpoint_origin
            and credential_handle == binding.credential_handle
            and provider_instance_id == binding.provider_instance_id
            and credential_scope == binding.credential_scope
        )
        with self._lock:
            if self._consumed or not valid:
                self._consumed = True
                self._audit(status="denied", endpoint_origin="")
                raise CredentialTransportDenied("credential transport denied")
            self._consumed = True

    def _authority_still_active(self) -> bool:
        try:
            durable, state = self._authority_store.inspect_lease_token(self._invocation_token)
            if state is not LeaseState.DISPATCHED:
                return False
            binding = self._binding
            if (
                durable.caller.principal_id != binding.caller_principal_id
                or durable.target.principal_id != binding.provider_principal_id
                or durable.target.function_id != binding.provider_function_id
                or durable.target.operation_id != binding.operation_id
                or durable.profile_id != binding.profile_id
                or durable.activation_id != binding.activation_id
                or durable.security_epoch != binding.security_epoch
                or durable.target_domain_id != binding.target_domain_id
                or durable.target_boot_epoch != binding.target_boot_epoch
                or durable.request_id != binding.request_id
                or durable.request_digest != binding.request_digest
            ):
                return False
            targets = (
                ("function_principal", durable.caller.principal_id),
                ("function_principal", durable.target.principal_id),
                ("execution_domain", durable.caller_domain_id),
                ("execution_domain", durable.target_domain_id),
                ("profile", durable.profile_id),
                ("activation", durable.activation_id),
                ("grant", durable.grant_id),
                ("provider_authority", durable.provider_authority_id),
            )
            return not any(
                self._authority_store.is_revoked(kind, identity) for kind, identity in targets
            )
        except Exception:
            return False

    def _audit(self, *, status: str, endpoint_origin: str) -> None:
        if self._audit_sink is None:
            return
        binding = self._binding
        try:
            self._audit_sink(
                {
                    "event": "credential_transport",
                    "status": status if status in {"completed", "denied", "failed"} else "failed",
                    "profile_id": binding.profile_id,
                    "activation_id": binding.activation_id,
                    "security_epoch": binding.security_epoch,
                    "caller_principal_id": binding.caller_principal_id,
                    "provider_principal_id": binding.provider_principal_id,
                    "provider_function_id": binding.provider_function_id,
                    "operation_id": binding.operation_id,
                    "target_domain_id": binding.target_domain_id,
                    "target_boot_epoch": binding.target_boot_epoch,
                    "request_id": binding.request_id,
                    "request_digest": binding.request_digest,
                    "credential_handle": binding.credential_handle,
                    "provider_instance_id": binding.provider_instance_id,
                    "credential_scope": binding.credential_scope,
                    "endpoint_origin": endpoint_origin,
                }
            )
        except Exception:
            return


class AuthorizedEnvelopeCredentialTransport:
    """Create at most one credential transport from one authorized envelope."""

    def __init__(
        self,
        *,
        envelope: RequestEnvelope,
        provider_principal: FunctionPrincipal,
        store: CredentialMaterialStore,
        authority_store: AuthorityStore,
        current_security_epoch: Callable[[], int],
        credential_key_version: str,
        consumer_pack_id: str,
        audit_sink: Callable[[Mapping[str, Any]], None] | None = None,
        clock: Callable[[], float] = time.time,
        monotonic_clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._envelope = envelope
        self._provider_principal = provider_principal
        self._store = store
        self._authority_store = authority_store
        self._current_security_epoch = current_security_epoch
        self._credential_key_version = credential_key_version
        self._consumer_pack_id = consumer_pack_id
        self._audit_sink = audit_sink
        self._clock = clock
        self._monotonic_clock = monotonic_clock
        self._used = False
        self._lock = RLock()

    def post_json(
        self,
        *,
        endpoint: str,
        headers: Mapping[str, str],
        body: Mapping[str, Any],
        credential_handle: str,
        provider_instance_id: str,
        credential_scope: str,
        credential_scheme: str,
        deadline: float,
    ) -> dict[str, Any]:
        """Construct and consume exactly one envelope-bound transport."""
        with self._lock:
            if self._used:
                raise CredentialTransportDenied("binding_invalid")
            self._used = True
        transport = HostBoundCredentialTransport.from_authorized_envelope(
            self._envelope,
            provider_principal=self._provider_principal,
            store=self._store,
            authority_store=self._authority_store,
            credential_handle=credential_handle,
            credential_key_version=self._credential_key_version,
            provider_instance_id=provider_instance_id,
            credential_scope=credential_scope,
            credential_purpose="provider.invoke",
            endpoint_origin=_credential_origin(endpoint),
            current_security_epoch=self._current_security_epoch,
            consumer_pack_id=self._consumer_pack_id,
            audit_sink=self._audit_sink,
            clock=self._clock,
            monotonic_clock=self._monotonic_clock,
        )
        return transport.post_json(
            endpoint=endpoint,
            headers=headers,
            body=body,
            credential_handle=credential_handle,
            provider_instance_id=provider_instance_id,
            credential_scope=credential_scope,
            credential_scheme=credential_scheme,
            deadline=deadline,
        )


def _remaining_deadline_budget(
    *,
    initial_remaining: float,
    started: float,
    clock: Callable[[], float],
) -> float:
    """Return a finite positive budget after monotonic elapsed time."""
    try:
        elapsed = float(clock()) - started
        remaining = initial_remaining - elapsed
    except (TypeError, ValueError, OverflowError):
        raise CredentialTransportDenied("binding_invalid") from None
    if (
        not math.isfinite(elapsed)
        or elapsed < 0
        or not math.isfinite(remaining)
        or remaining <= 0
    ):
        raise CredentialTransportDenied("binding_invalid")
    return remaining


def _safe_text(value: object) -> bool:
    text = str(value or "")
    return bool(text and "\x00" not in text and "\n" not in text and "\r" not in text)


def _origin(value: str) -> str:
    parsed = urllib.parse.urlsplit(str(value or ""))
    try:
        port = parsed.port
    except ValueError:
        return ""
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
    ):
        return ""
    host = parsed.hostname.lower()
    rendered_host = f"[{host}]" if ":" in host else host
    default_port = 80 if parsed.scheme == "http" else 443
    port_text = "" if port in {None, default_port} else f":{port}"
    return f"{parsed.scheme}://{rendered_host}{port_text}"


def _credential_origin(value: str) -> str:
    """Return a canonical origin only when credential transport is TLS-protected."""
    origin = _origin(value)
    if not origin or urllib.parse.urlsplit(origin).scheme != "https":
        return ""
    return origin


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    """TLS connection whose TCP peer is a previously validated address."""

    def __init__(self, host: str, resolved_ip: str, **kwargs: Any) -> None:
        super().__init__(host, **kwargs)
        self._resolved_ip = resolved_ip
        self._pinned_source_address = kwargs.get("source_address")
        self._pinned_context = kwargs.get("context") or ssl.create_default_context()

    def connect(self) -> None:
        """Connect to the vetted IP while authenticating the original hostname."""
        raw_socket = socket.create_connection(
            (self._resolved_ip, self.port),
            self.timeout,
            self._pinned_source_address,
        )
        self.sock = self._pinned_context.wrap_socket(raw_socket, server_hostname=self.host)


class _PinnedResponse:
    """Keep the pinned connection alive until its response is closed."""

    def __init__(
        self,
        connection: http.client.HTTPConnection,
        response: http.client.HTTPResponse,
    ) -> None:
        self._connection = connection
        self._response = response

    def __enter__(self) -> "_PinnedResponse":
        return self

    def __exit__(self, *args: object) -> None:
        self._response.close()
        self._connection.close()

    def read(self, amount: int | None = None) -> bytes:
        """Read at most the caller-provided response limit."""
        return self._response.read(amount)


def _open_pinned_request(
    request: urllib.request.Request,
    *,
    timeout: float,
) -> JsonResponse:
    """Open one non-redirecting request to an egress-vetted, DNS-pinned peer."""
    parsed = urllib.parse.urlsplit(request.full_url)
    origin = _origin(request.full_url)
    if not origin:
        raise CredentialTransportDenied("credential transport denied")
    host = parsed.hostname
    if host is None:
        raise CredentialTransportDenied("credential transport denied")
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    try:
        resolved = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except OSError:
        raise CredentialTransportDenied("credential transport denied") from None
    addresses = tuple(dict.fromkeys(str(item[4][0]) for item in resolved))
    if not addresses or any(not _safe_egress_address(address) for address in addresses):
        raise CredentialTransportDenied("credential transport denied")

    path = urllib.parse.urlunsplit(("", "", parsed.path or "/", parsed.query, ""))
    headers = {
        key: item
        for key, item in request.header_items()
        if key.lower() != "host"
    }
    headers["Host"] = parsed.netloc
    body = request.data
    last_error: OSError | None = None
    for address in addresses:
        if parsed.scheme == "https":
            connection: http.client.HTTPConnection = _PinnedHTTPSConnection(
                host,
                address,
                port=port,
                timeout=timeout,
                context=ssl.create_default_context(),
            )
        else:
            connection = http.client.HTTPConnection(address, port=port, timeout=timeout)
        try:
            connection.request(request.get_method(), path, body=body, headers=headers)
            response = connection.getresponse()
        except OSError as exc:
            connection.close()
            last_error = exc
            continue
        if 300 <= response.status < 400:
            response.close()
            connection.close()
            raise CredentialTransportDenied("credential transport denied")
        if response.status >= 400:
            status = response.status
            response.close()
            connection.close()
            raise urllib.error.HTTPError(
                origin,
                status,
                "provider request failed",
                Message(),
                None,
            )
        return _PinnedResponse(connection, response)
    if last_error is not None:
        raise last_error
    raise CredentialTransportDenied("credential transport denied")


def _safe_egress_address(value: str) -> bool:
    """Allow only globally routable unicast peers for credential-bearing traffic."""
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return False
    return address.is_global and not any(
        (
            address.is_loopback,
            address.is_link_local,
            address.is_multicast,
            address.is_private,
            address.is_reserved,
            address.is_unspecified,
        )
    )


def _sanitize_provider_response(value: Any, secret: str, *, depth: int = 0) -> Any:
    """Return JSON data whose field names and values cannot expose material."""

    if depth > _MAX_RESPONSE_DEPTH:
        raise CredentialTransportDenied("response_invalid")
    if isinstance(value, dict):
        if any(not isinstance(key, str) or key not in _PROVIDER_RESPONSE_FIELDS for key in value):
            raise CredentialTransportDenied("response_invalid")
        return {
            key: _sanitize_provider_response(item, secret, depth=depth + 1)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_sanitize_provider_response(item, secret, depth=depth + 1) for item in value]
    if isinstance(value, str) and secret and secret in value:
        return value.replace(secret, "[REDACTED]")
    if value is not None and not isinstance(value, (str, int, float, bool)):
        raise CredentialTransportDenied("response_invalid")
    return value


def _clear_material(material: dict[str, Any] | None) -> None:
    """Release resolved material without allowing cleanup failures to escape."""

    if material is None:
        return
    try:
        material.clear()
    except Exception:
        return


__all__ = [
    "AuthorizedEnvelopeCredentialTransport",
    "CredentialMaterialStoreBinding",
    "CredentialMaterialStoreFactory",
    "CredentialTransportBinding",
    "CredentialTransportDenied",
    "HostBoundCredentialTransport",
]
