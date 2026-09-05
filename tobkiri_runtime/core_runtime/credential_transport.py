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
import hashlib
import http.client
import ipaddress
import json
import math
import os
from pathlib import Path
import re
import secrets
import socket
import ssl
import subprocess
import tempfile
from threading import RLock
import time
from typing import Any, Protocol, cast
import urllib.error
import urllib.parse
import urllib.request

from core_runtime.authority.v4 import (
    AuthorityStore,
    FunctionPrincipal,
    LeaseState,
)
from core_runtime.executable_trust import (
    ExecutableTrustError,
    trusted_executable_path,
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
        expected_resource_binding: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Resolve material only inside the Host transport boundary."""

    def select(
        self,
        *,
        consumer_pack_id: str,
        provider_instance_id: str,
        scope: str,
        profile_id: str,
        resource_binding: Mapping[str, Any],
        purpose: str = "provider.invoke",
    ) -> dict[str, Any] | None:
        """Return one exact opaque handle record without secret material."""


@dataclass(frozen=True)
class CredentialMaterialStoreBinding:
    """One Host credential store and its exact opaque key-version binding."""

    store: CredentialMaterialStore
    key_version: str

    def __post_init__(self) -> None:
        """Reject an incomplete factory result before provider dispatch."""
        if (
            not callable(getattr(self.store, "resolve", None))
            or not callable(getattr(self.store, "select", None))
            or not _safe_text(self.key_version)
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
    consumer_pack_id: str

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
        expected_resource_binding: Mapping[str, Any] | None = None,
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
        self._expected_resource_binding = (
            dict(expected_resource_binding)
            if expected_resource_binding is not None
            else None
        )
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
        consumer_pack_id: str,
        audit_sink: Callable[[Mapping[str, Any]], None] | None = None,
        clock: Callable[[], float] = time.time,
        monotonic_clock: Callable[[], float] = time.monotonic,
        expected_resource_binding: Mapping[str, Any] | None = None,
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
            expected_resource_binding=expected_resource_binding,
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
                    expected_resource_binding=self._expected_resource_binding,
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
            sanitized = _sanitize_json_response(value, secret_text)
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

    def push_git_https(
        self,
        *,
        git_executable: str,
        git_executable_identity: Mapping[str, Any],
        bare_repository: str,
        remote_url: str,
        refspec: str,
        force_with_lease: str,
        credential_handle: str,
        provider_instance_id: str,
        credential_scope: str,
    ) -> str:
        """Run one exact HTTPS Git push with Host-resolved credentials.

        This is deliberately a finite transport primitive, not a generic
        subprocess capability.  The Host constructs the sole ``git push``
        argv, injects credentials only into that child process, and returns a
        redacted status string.  Pack code never receives credential material,
        an askpass environment, or a reusable process handle.
        """

        expected_resource = self._expected_resource_binding
        if (
            not isinstance(expected_resource, Mapping)
            or dict(expected_resource)
            != {
                "endpoint_origin": _credential_origin(remote_url),
                "workspace_id": str(expected_resource.get("workspace_id") or ""),
            }
            or not _safe_text(expected_resource.get("workspace_id"))
        ):
            raise CredentialTransportDenied("binding_invalid")

        self._consume_once(
            endpoint=remote_url,
            credential_handle=credential_handle,
            provider_instance_id=provider_instance_id,
            credential_scope=credential_scope,
        )
        material: dict[str, Any] | None = None
        secret_bytes: bytearray | None = None
        username_bytes: bytearray | None = None
        secret_text = ""
        audit_status = "failed"
        try:
            executable = _trusted_git_executable(
                git_executable,
                expected_identity=git_executable_identity,
            )
            repository = _bare_repository(bare_repository, executable)
            origin = _credential_origin(remote_url)
            if not origin or _git_push_arguments_are_invalid(
                remote_url=remote_url,
                refspec=refspec,
                force_with_lease=force_with_lease,
            ):
                raise CredentialTransportDenied("binding_invalid")
            try:
                material = self._store.resolve(
                    self._binding.credential_handle,
                    consumer_pack_id=self._binding.consumer_pack_id,
                    provider_instance_id=self._binding.provider_instance_id,
                    profile_id=self._binding.profile_id,
                    scope=self._binding.credential_scope,
                    key_version=self._binding.credential_key_version,
                    purpose=self._binding.credential_purpose,
                    expected_resource_binding=self._expected_resource_binding,
                )
            except Exception:
                raise CredentialTransportDenied("store_failure") from None
            username, secret = _git_credential_material(material)
            username_bytes = bytearray(username.encode("utf-8"))
            secret_bytes = bytearray(secret.encode("utf-8"))
            secret_text = secret_bytes.decode("utf-8")
            output = _run_credentialed_git_push(
                executable=executable,
                repository=repository,
                remote_url=remote_url,
                refspec=refspec,
                force_with_lease=force_with_lease,
                username=username_bytes.decode("utf-8"),
                secret=secret_text,
            )
            audit_status = "completed"
            return _redact_git_output(output, secret_text)
        except CredentialTransportDenied:
            audit_status = "denied"
            raise
        except (OSError, PermissionError, UnicodeError, ValueError):
            raise CredentialTransportDenied("provider_failure") from None
        except Exception:
            raise CredentialTransportDenied("provider_failure") from None
        finally:
            _clear_material(material)
            if username_bytes is not None:
                username_bytes[:] = b"\x00" * len(username_bytes)
            if secret_bytes is not None:
                secret_bytes[:] = b"\x00" * len(secret_bytes)
            self._audit(
                status=audit_status,
                endpoint_origin=_origin(remote_url),
            )

    def _authority_still_active(self) -> bool:
        try:
            durable, state = self._authority_store.inspect_lease_token(
                self._invocation_token
            )
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
                self._authority_store.is_revoked(kind, identity)
                for kind, identity in targets
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
                    "status": (
                        status
                        if status in {"completed", "denied", "failed"}
                        else "failed"
                    ),
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
        self._selection_used = False
        self._git_selection: dict[str, Any] | None = None
        self._lock = RLock()

    def select_git_https_credential(
        self,
        *,
        workspace_id: str,
        endpoint_origin: str,
        provider_instance_id: str,
        credential_scope: str,
    ) -> Mapping[str, Any] | None:
        """Select one Host-bound opaque Git credential without resolving it."""

        origin = _credential_origin(endpoint_origin)
        if (
            not origin
            or origin != endpoint_origin
            or not _safe_text(workspace_id)
            or not _safe_text(provider_instance_id)
            or not _safe_text(credential_scope)
        ):
            raise CredentialTransportDenied("binding_invalid")
        with self._lock:
            if self._selection_used:
                raise CredentialTransportDenied("binding_invalid")
            self._selection_used = True
        try:
            selected = self._store.select(
                consumer_pack_id=self._consumer_pack_id,
                provider_instance_id=provider_instance_id,
                scope=credential_scope,
                profile_id=self._envelope.context.profile_id,
                resource_binding={
                    "endpoint_origin": origin,
                    "workspace_id": workspace_id,
                },
                purpose="provider.invoke",
            )
        except Exception:
            raise CredentialTransportDenied("store_failure") from None
        if selected is None:
            return None
        identity = {
            "handle": str(selected.get("handle") or ""),
            "key_version": str(selected.get("key_version") or ""),
            "consumer_pack_id": str(selected.get("consumer_pack_id") or ""),
            "provider_instance_id": str(
                selected.get("provider_instance_id") or ""
            ),
            "profile_id": str(selected.get("profile_id") or ""),
            "scope": credential_scope,
            "purpose": str(selected.get("purpose") or ""),
            "resource_binding": dict(selected.get("resource_binding") or {}),
        }
        expected = {
            "handle": identity["handle"],
            "key_version": self._credential_key_version,
            "consumer_pack_id": self._consumer_pack_id,
            "provider_instance_id": provider_instance_id,
            "profile_id": self._envelope.context.profile_id,
            "scope": credential_scope,
            "purpose": "provider.invoke",
            "resource_binding": {
                "endpoint_origin": origin,
                "workspace_id": workspace_id,
            },
        }
        credential_handle = cast(str, identity["handle"])
        if (
            identity != expected
            or not credential_handle.startswith(("credential:", "opaque:"))
        ):
            raise CredentialTransportDenied("binding_invalid")
        identity["binding_digest"] = _credential_selection_digest(identity)
        receipt = f"credential-selection:{secrets.token_urlsafe(32)}"
        with self._lock:
            self._git_selection = {
                "credential_identity": dict(identity),
                "selection_receipt": receipt,
            }
        return {**identity, "selection_receipt": receipt}

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

    def push_git_https(
        self,
        *,
        git_executable: str,
        git_executable_identity: Mapping[str, Any],
        bare_repository: str,
        remote_url: str,
        refspec: str,
        force_with_lease: str,
        credential_handle: str,
        provider_instance_id: str,
        credential_scope: str,
        workspace_id: str,
        selection_receipt: str,
    ) -> str:
        """Construct and consume one envelope-bound HTTPS Git transport."""

        with self._lock:
            selection = self._git_selection
            self._git_selection = None
            selected_identity = (
                selection.get("credential_identity")
                if isinstance(selection, Mapping)
                else None
            )
            selected_resource = (
                selected_identity.get("resource_binding")
                if isinstance(selected_identity, Mapping)
                else None
            )
            expected_resource = {
                "endpoint_origin": _credential_origin(remote_url),
                "workspace_id": workspace_id,
            }
            valid_selection = (
                isinstance(selection, Mapping)
                and selection_receipt == selection.get("selection_receipt")
                and isinstance(selected_identity, Mapping)
                and selected_identity.get("handle") == credential_handle
                and selected_identity.get("consumer_pack_id")
                == self._consumer_pack_id
                and selected_identity.get("provider_instance_id")
                == provider_instance_id
                and selected_identity.get("profile_id")
                == self._envelope.context.profile_id
                and selected_identity.get("scope") == credential_scope
                and selected_identity.get("purpose") == "provider.invoke"
                and isinstance(selected_resource, Mapping)
                and dict(selected_resource) == expected_resource
            )
            if self._used or not valid_selection:
                self._used = True
                raise CredentialTransportDenied("binding_invalid")
            if not _safe_text(selection_receipt) or not _safe_text(workspace_id):
                self._used = True
                raise CredentialTransportDenied("binding_invalid")
            if not selection_receipt.startswith("credential-selection:"):
                self._used = True
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
            endpoint_origin=_credential_origin(remote_url),
            current_security_epoch=self._current_security_epoch,
            consumer_pack_id=self._consumer_pack_id,
            expected_resource_binding=expected_resource,
            audit_sink=self._audit_sink,
            clock=self._clock,
            monotonic_clock=self._monotonic_clock,
        )
        return transport.push_git_https(
            git_executable=git_executable,
            git_executable_identity=git_executable_identity,
            bare_repository=bare_repository,
            remote_url=remote_url,
            refspec=refspec,
            force_with_lease=force_with_lease,
            credential_handle=credential_handle,
            provider_instance_id=provider_instance_id,
            credential_scope=credential_scope,
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


def _credential_selection_digest(value: Mapping[str, Any]) -> str:
    """Bind the exact secret-free handle identity into a prepared Git plan."""

    encoded = json.dumps(
        dict(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


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
        self.sock = self._pinned_context.wrap_socket(
            raw_socket, server_hostname=self.host
        )


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
        key: item for key, item in request.header_items() if key.lower() != "host"
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


def _sanitize_json_response(value: Any, secret: str, *, depth: int = 0) -> Any:
    """Return generic JSON that cannot disclose Host-resolved material.

    The Host deliberately does not know provider response schemas.  Provider
    adapters own response projection and normalization after this boundary has
    applied its generic size, JSON-shape, depth, and secret non-leak rules.
    """

    if depth > _MAX_RESPONSE_DEPTH:
        raise CredentialTransportDenied("response_invalid")
    if isinstance(value, dict):
        if any(not isinstance(key, str) or (secret and secret in key) for key in value):
            raise CredentialTransportDenied("response_invalid")
        return {
            key: _sanitize_json_response(item, secret, depth=depth + 1)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [
            _sanitize_json_response(item, secret, depth=depth + 1) for item in value
        ]
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


_GIT_OID = r"[0-9a-f]{40}(?:[0-9a-f]{24})?"
_GIT_REF = r"refs/heads/[A-Za-z0-9][A-Za-z0-9._/-]{0,199}"


def _trusted_git_executable(
    value: str,
    *,
    expected_identity: Mapping[str, Any],
) -> Path:
    """Revalidate one Git executable against the Provider capture identity."""

    try:
        return trusted_executable_path(value, expected_identity=expected_identity)
    except (ExecutableTrustError, OSError, ValueError, TypeError):
        raise CredentialTransportDenied("binding_invalid") from None


def _bare_repository(value: str, executable: Path) -> Path:
    """Verify that the Host credential port receives a temporary bare repo."""

    repository = Path(str(value or "")).resolve(strict=True)
    if not repository.is_dir() or repository.is_symlink():
        raise CredentialTransportDenied("binding_invalid")
    completed = subprocess.run(
        [str(executable), "-C", str(repository), "rev-parse", "--is-bare-repository"],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
        env=_hardened_git_environment(),
    )
    if completed.returncode != 0 or completed.stdout.strip() != "true":
        raise CredentialTransportDenied("binding_invalid")
    return repository


def _git_push_arguments_are_invalid(
    *,
    remote_url: str,
    refspec: str,
    force_with_lease: str,
) -> bool:
    """Recognize anything other than the one immutable HTTPS push shape."""

    parsed = urllib.parse.urlsplit(str(remote_url or ""))
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        return True
    refspec_match = re.fullmatch(
        rf"(?P<oid>{_GIT_OID}):(?P<ref>{_GIT_REF})",
        str(refspec or ""),
    )
    lease_match = re.fullmatch(
        rf"--force-with-lease=(?P<ref>{_GIT_REF}):(?P<oid>{_GIT_OID})",
        str(force_with_lease or ""),
    )
    return (
        refspec_match is None
        or lease_match is None
        or refspec_match.group("ref") != lease_match.group("ref")
    )


def _git_credential_material(material: Mapping[str, Any]) -> tuple[str, str]:
    """Return one bounded HTTPS Git username/token pair from Host material."""

    username = material.get("username") or "x-access-token"
    secret = (
        material.get("token") or material.get("api_key") or material.get("password")
    )
    if (
        not isinstance(username, str)
        or not isinstance(secret, str)
        or not username
        or not secret
        or len(username) > 256
        or len(secret) > 16_384
        or not _safe_text(username)
        or not _safe_text(secret)
    ):
        raise CredentialTransportDenied("material_invalid")
    return username, secret


def _run_credentialed_git_push(
    *,
    executable: Path,
    repository: Path,
    remote_url: str,
    refspec: str,
    force_with_lease: str,
    username: str,
    secret: str,
) -> str:
    """Run a single hooks-disabled Git HTTPS push with a private askpass hook."""

    with tempfile.TemporaryDirectory(prefix="tobkiri-git-credential-") as temporary:
        askpass = Path(temporary) / "askpass.sh"
        askpass.write_text(
            "#!/bin/sh\n"
            'case "$1" in\n'
            '  *Username*|*username*) printf %s "$TOBKIRI_GIT_ASKPASS_USERNAME" ;;\n'
            '  *Password*|*password*) printf %s "$TOBKIRI_GIT_ASKPASS_SECRET" ;;\n'
            "  *) exit 1 ;;\n"
            "esac\n",
            encoding="utf-8",
        )
        os.chmod(askpass, 0o700)
        environment = _hardened_git_environment()
        environment.update(
            {
                "GIT_ASKPASS": str(askpass),
                "SSH_ASKPASS": str(askpass),
                "TOBKIRI_GIT_ASKPASS_USERNAME": username,
                "TOBKIRI_GIT_ASKPASS_SECRET": secret,
            }
        )
        completed = subprocess.run(
            [
                str(executable),
                "-C",
                str(repository),
                "-c",
                f"core.hooksPath={os.devnull}",
                "-c",
                "core.fsmonitor=false",
                "-c",
                "credential.helper=",
                "-c",
                "http.extraHeader=",
                "-c",
                "http.followRedirects=false",
                "-c",
                "push.followTags=false",
                "-c",
                "push.gpgSign=false",
                "-c",
                "push.recurseSubmodules=no",
                "-c",
                "push.useForceIfIncludes=false",
                "-c",
                "protocol.allow=never",
                "-c",
                "protocol.https.allow=always",
                "-c",
                "protocol.ssh.allow=never",
                "-c",
                "protocol.ext.allow=never",
                "-c",
                "protocol.file.allow=never",
                "push",
                force_with_lease,
                "--",
                remote_url,
                refspec,
            ],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=180,
            check=False,
            env=environment,
        )
    output = (completed.stdout + completed.stderr)[:256_000]
    if completed.returncode != 0:
        raise CredentialTransportDenied("provider_failure")
    return output


def _hardened_git_environment() -> dict[str, str]:
    """Build a fresh no-prompt Git environment without ambient credentials."""

    environment = {
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_PROTOCOL_FROM_USER": "0",
        "GIT_TERMINAL_PROMPT": "0",
        "GCM_INTERACTIVE": "never",
        "LC_ALL": "C",
        "PATH": os.defpath,
    }
    for name in ("SYSTEMROOT", "TEMP", "TMP", "TMPDIR", "WINDIR"):
        value = os.environ.get(name)
        if value:
            environment[name] = value
    return environment


def _redact_git_output(value: str, secret: str) -> str:
    """Project Git output without exposing a Host-resolved credential value."""

    return str(value or "").replace(secret, "[REDACTED]")[:256_000]


__all__ = [
    "AuthorizedEnvelopeCredentialTransport",
    "CredentialMaterialStoreBinding",
    "CredentialMaterialStoreFactory",
    "CredentialTransportBinding",
    "CredentialTransportDenied",
    "HostBoundCredentialTransport",
]
