"""Envelope-bound, credential-free transport to a registered local AI server."""

from __future__ import annotations

from collections.abc import Callable, Mapping
import http.client
import json
import math
import socket
from threading import RLock
import time
from typing import Any
from urllib.parse import urlsplit

from core_runtime.authority.v4 import AuthorityStore, FunctionPrincipal
from core_runtime.http_request_lifetime import HttpRequestLifetime
from tobkiri_host.broker import RequestEnvelope

_REGISTRY_CONTRACT = "tobkiri.resource.ai.provider.registry.v1"
_ROUTES = {
    "tobkiri.service.ai.provider.generate.v1": ("ai.generate", "/chat/completions"),
    "tobkiri.service.ai.provider.stream.v1": ("ai.stream", "/chat/completions"),
    "tobkiri.service.ai.provider.embedding.v1": ("ai.embedding", "/embeddings"),
    "tobkiri.service.ai.provider.image.v1": ("ai.image", "/images/generations"),
}
_MAX_BYTES = 4 * 1024 * 1024
_MAX_DEPTH = 32


class LocalProviderTransportDenied(PermissionError):
    """Expose a fixed error without returning registry or request material."""

    def __init__(self) -> None:
        super().__init__("local provider transport denied")


def local_provider_base(value: Any) -> str:
    """Return a canonical literal-loopback OpenAI base, or an empty string."""
    if not isinstance(value, str):
        return ""
    try:
        parsed = urlsplit(value)
        port = parsed.port
        if (
            parsed.scheme != "http"
            or parsed.hostname not in {"127.0.0.1", "::1"}
            or parsed.username is not None
            or parsed.password is not None
            or port is None
            or not 1 <= port <= 65535
            or parsed.query
            or parsed.fragment
            or parsed.path.rstrip("/") not in {"", "/v1"}
        ):
            return ""
        host = f"[{parsed.hostname}]" if parsed.hostname == "::1" else parsed.hostname
        canonical = f"http://{host}:{port}{parsed.path.rstrip('/')}"
        return canonical if value.rstrip("/") == canonical else ""
    except ValueError:
        return ""


class AuthorizedEnvelopeLocalProviderTransport:
    """Use one dispatched lease and re-read its registry-owned connection.

    This port is separate from credential transport. It cannot access secret
    storage, resolve hostnames, use a proxy, follow a redirect, or send beyond
    the exact registered loopback base and the current AI operation's route.
    """

    def __init__(
        self,
        *,
        envelope: RequestEnvelope,
        provider_principal: FunctionPrincipal,
        authority_store: AuthorityStore,
        allowed_contract_ids: frozenset[str],
        registry_reader: Callable[[str, str, Mapping[str, Any]], Mapping[str, Any]],
        consumer_pack_id: str,
        audit_sink: Callable[[Mapping[str, Any]], None] | None = None,
        clock: Callable[[], float] = time.time,
        monotonic_clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._envelope = envelope
        self._principal = provider_principal
        self._authority = authority_store
        self._allowed = allowed_contract_ids
        self._registry_reader = registry_reader
        self._consumer = consumer_pack_id
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
        provider_instance_id: str,
        provider_scope: str,
        registry_contract_id: str,
        registry_operation_id: str,
        deadline: float,
    ) -> dict[str, Any]:
        """Perform one finite local inference request under the active lease."""
        status = "denied"
        origin = ""
        with self._lock:
            if self._used:
                raise LocalProviderTransportDenied()
            self._used = True
        try:
            route = _ROUTES.get(self._envelope.contract_id)
            if (
                route is None
                or route[0] != provider_scope
                or registry_contract_id != _REGISTRY_CONTRACT
                or registry_contract_id not in self._allowed
                or not self._active()
            ):
                raise LocalProviderTransportDenied()
            request = self._envelope.payload
            expected_connection = request.get("provider_connection_id")
            if expected_connection is None:
                provider_id = request.get("provider_id")
                expected_connection = f"provider.{provider_id}" if isinstance(provider_id, str) else None
            if expected_connection != provider_instance_id:
                raise LocalProviderTransportDenied()
            remaining = float(deadline) - self._clock()
            if not math.isfinite(remaining) or remaining <= 0:
                raise LocalProviderTransportDenied()
            stop_at = min(
                self._envelope.deadline_monotonic,
                self._monotonic_clock() + remaining,
            )
            snapshot = self._registry_reader(
                registry_contract_id,
                registry_operation_id,
                {"profile_id": self._envelope.context.profile_id},
            )
            providers = snapshot.get("providers") if isinstance(snapshot, Mapping) else None
            matches = [
                item for item in providers if isinstance(item, Mapping)
                and item.get("provider_instance_id") == provider_instance_id
                and item.get("enabled") is True
            ] if isinstance(providers, list) else []
            if len(matches) != 1:
                raise LocalProviderTransportDenied()
            connection = matches[0]
            base = local_provider_base(connection.get("endpoint"))
            if (
                not base
                or connection.get("credential_handle") is not None
                or connection.get("adapter_id") not in {"openai-compatible", "openai"}
                or endpoint != base + route[1]
                or not self._active()
            ):
                raise LocalProviderTransportDenied()
            origin = base.split("/v1", 1)[0]
            outbound_headers = self._headers(headers)
            _check_depth(body)
            encoded_body = json.dumps(body, ensure_ascii=False, allow_nan=False).encode("utf-8")
            if len(encoded_body) > _MAX_BYTES:
                raise LocalProviderTransportDenied()
            self._audit("started", provider_instance_id, provider_scope, origin)
            with_lifetime = HttpRequestLifetime(
                timeout=min(60.0, stop_at - self._monotonic_clock()),
                deadline=stop_at,
                cancellation=self._envelope.cancellation_requested,
                clock=self._monotonic_clock,
                authority_check=self._active,
            )
            try:
                result = self._request(endpoint, outbound_headers, encoded_body, with_lifetime)
            finally:
                with_lifetime.close()
            if not self._active():
                raise LocalProviderTransportDenied()
            status = "completed"
            return result
        except LocalProviderTransportDenied:
            raise
        except Exception:
            raise LocalProviderTransportDenied() from None
        finally:
            self._audit(status, provider_instance_id, provider_scope, origin)

    @staticmethod
    def _headers(headers: Mapping[str, str]) -> dict[str, str]:
        allowed = {"content-type", "accept", "user-agent"}
        if any(
            not isinstance(key, str) or key.lower() not in allowed
            or not isinstance(value, str) or "\r" in value or "\n" in value
            for key, value in headers.items()
        ):
            raise LocalProviderTransportDenied()
        return dict(headers)

    def _request(
        self, endpoint: str, headers: Mapping[str, str], body: bytes,
        lifetime: HttpRequestLifetime,
    ) -> dict[str, Any]:
        parsed = urlsplit(endpoint)
        address = parsed.hostname
        if address is None:
            raise LocalProviderTransportDenied()
        connection = http.client.HTTPConnection(address, parsed.port, timeout=lifetime.remaining())
        # Connect the numeric address directly; no resolver or proxy can change it.
        peer = socket.socket(socket.AF_INET6 if address == "::1" else socket.AF_INET)
        response = None
        try:
            peer.settimeout(lifetime.remaining())
            lifetime.attach(peer)
            peer.connect((address, parsed.port))
            connection.sock = peer
            connection.auto_open = 0
            lifetime.check()
            connection.request("POST", parsed.path, body=body, headers=dict(headers))
            response = connection.getresponse()
            lifetime.check()
            if not 200 <= response.status < 300:
                raise LocalProviderTransportDenied()
            raw = response.read(_MAX_BYTES + 1)
            lifetime.check()
            if len(raw) > _MAX_BYTES:
                raise LocalProviderTransportDenied()
            result = json.loads(
                raw.decode("utf-8"), parse_constant=_reject_number,
                object_pairs_hook=_unique_fields,
            )
            if not isinstance(result, dict):
                raise LocalProviderTransportDenied()
            _check_depth(result)
            return result
        finally:
            if response is not None:
                response.close()
            connection.close()
            peer.close()

    def _active(self) -> bool:
        envelope = self._envelope
        context = envelope.context
        try:
            if (
                envelope.cancellation_requested.is_set()
                or self._monotonic_clock() >= envelope.deadline_monotonic
            ):
                return False
            durable = self._authority.inspect_active_lease_token(envelope.lease.token.decode("ascii"))
            if (
                envelope.target_principal.value != self._principal.principal_id
                or envelope.operation_id != self._principal.operation_id
                or envelope.target_domain.value != context.target_domain_id
                or durable.target != self._principal
                or durable.caller.principal_id != context.caller_principal.value
                or durable.profile_id != context.profile_id
                or durable.activation_id != context.activation_id
                or durable.activation_digest != context.activation_digest
                or durable.plan_digest != context.plan_digest
                or durable.profile_authority_digest != context.profile_authority_digest
                or durable.fencing_token != context.fencing_token
                or durable.security_epoch != context.security_epoch
                or durable.caller_domain_id != context.caller_domain_id
                or durable.caller_boot_epoch != context.caller_boot_epoch
                or durable.target_domain_id != context.target_domain_id
                or durable.target_boot_epoch != context.target_boot_epoch
                or durable.request_id != context.request_id
                or durable.request_digest != envelope.request_digest
            ):
                return False
            return True
        except Exception:
            return False

    def _audit(self, status: str, provider: str, scope: str, origin: str) -> None:
        if self._audit_sink is None:
            raise LocalProviderTransportDenied()
        context = self._envelope.context
        try:
            self._audit_sink({
                "event": "local_provider_transport", "status": status,
                "profile_id": context.profile_id, "activation_id": context.activation_id,
                "security_epoch": context.security_epoch,
                "caller_principal_id": context.caller_principal.value,
                "provider_principal_id": self._principal.principal_id,
                "request_id": context.request_id, "request_digest": self._envelope.request_digest,
                "consumer_pack_id": self._consumer, "provider_instance_id": provider,
                "provider_scope": scope, "endpoint_origin": origin,
            })
        except Exception:
            raise LocalProviderTransportDenied() from None


def _check_depth(value: Any, depth: int = 0) -> None:
    if depth > _MAX_DEPTH:
        raise LocalProviderTransportDenied()
    if isinstance(value, float) and not math.isfinite(value):
        raise LocalProviderTransportDenied()
    children = value.values() if isinstance(value, dict) else value if isinstance(value, list) else ()
    for child in children:
        _check_depth(child, depth + 1)


def _reject_number(_value: str) -> None:
    raise LocalProviderTransportDenied()


def _unique_fields(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result = {}
    for key, value in pairs:
        if key in result:
            raise LocalProviderTransportDenied()
        result[key] = value
    return result
