from __future__ import annotations

import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import inspect
import json
import os
from pathlib import Path
import socket
import sys
import time
from threading import Thread
from typing import Any, Mapping
import urllib.request
from dataclasses import replace

import pytest

from ecosystem.rumi_credential_broker_pack.runtime.store import CredentialBrokerStore
from tobkiri_host import credential_store as store_module
from core_runtime.credential_transport import (
    AuthorizedEnvelopeCredentialTransport,
    CredentialTransportDenied,
    HostBoundCredentialTransport,
    _MAX_RESPONSE_BYTES,
)
from core_runtime import credential_transport as transport_module
from core_runtime.global_contract_dispatch import (
    GlobalContractClient,
    HostCredentialTransportError,
)
from ecosystem.rumi_credential_broker_pack.runtime.service import (
    CredentialBrokerService,
)
from ecosystem.rumi_provider_adapters_pack.runtime.adapter import (
    REGISTRY_CONTRACT,
    REGISTRY_OPERATION,
    create_generate_operation,
)
from tests.test_authority_v4_lifecycle import _Harness
from tests.test_tobkiri_host_authority_v4_adapter import (
    _adapter,
    _context,
    _digest,
    _queries,
)
from tobkiri_host.broker import RequestEnvelope
from tobkiri_host.models import OpaqueAuthorityRef


class _Response:
    def __init__(self, value: dict[str, Any]) -> None:
        self._value = value

    def __enter__(self) -> "_Response":
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def read(self, amount: int | None = None) -> bytes:
        return json.dumps(self._value).encode("utf-8")[:amount]


class _BytesResponse:
    def __init__(self, value: bytes) -> None:
        self._value = value

    def __enter__(self) -> "_BytesResponse":
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def read(self, amount: int | None = None) -> bytes:
        return self._value[:amount]


class _ControlledProvider(BaseHTTPRequestHandler):
    response_body = b"{}"
    response_location: str | None = None
    received: list[dict[str, str | None]] = []

    def do_POST(self) -> None:
        """Capture credential headers and emit the configured provider response."""
        type(self).received.append(
            {
                "authorization": self.headers.get("Authorization"),
                "api_key": self.headers.get("x-api-key"),
                "host": self.headers.get("Host"),
                "path": self.path,
            }
        )
        if type(self).response_location is not None:
            self.send_response(302)
            self.send_header("Location", type(self).response_location)
        else:
            self.send_response(200)
        self.send_header("Content-Length", str(len(type(self).response_body)))
        self.end_headers()
        self.wfile.write(type(self).response_body)

    def log_message(self, _format: str, *_args: object) -> None:
        """Keep controlled-server traffic out of test diagnostics."""


def _start_provider(
    *,
    body: bytes = b"{}",
    location: str | None = None,
) -> tuple[ThreadingHTTPServer, type[_ControlledProvider]]:
    class Handler(_ControlledProvider):
        received: list[dict[str, str | None]] = []

    Handler.response_body = body
    Handler.response_location = location
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, Handler


def _https_transport(
    tmp_path: Path,
    *,
    secret: str = "redirect-secret",
) -> tuple[HostBoundCredentialTransport, dict[str, Any]]:
    service = CredentialBrokerService(user_data_root=tmp_path / "credential")
    created = service.invoke(
        "create",
        {
            "secret_material": {"api_key": secret},
            "profile_id": "profile-1",
            "consumer_pack_id": "provider-adapter-pack",
            "provider_instance_id": "provider.adapter-main",
            "scopes": ["generate"],
        },
    )
    authority, envelope = _dispatched_envelope(tmp_path / "dispatch")
    transport = HostBoundCredentialTransport.from_authorized_envelope(
        envelope,
        provider_principal=authority.target,
        store=service.store,
        authority_store=authority.store,
        credential_handle=created["handle"],
        credential_key_version=created["key_version"],
        provider_instance_id="provider.adapter-main",
        credential_scope="generate",
        credential_purpose="provider.invoke",
        endpoint_origin="https://provider.example",
        current_security_epoch=lambda: authority.store.security_epoch,
        consumer_pack_id="provider-adapter-pack",
    )
    arguments = {
        "endpoint": "https://provider.example/v1/messages",
        "headers": {},
        "body": {},
        "credential_handle": created["handle"],
        "provider_instance_id": "provider.adapter-main",
        "credential_scope": "generate",
        "credential_scheme": "bearer",
        "deadline": 9_999_999_999.0,
    }
    return transport, arguments


def test_credential_transport_requires_explicit_captured_consumer_identity() -> None:
    """No provider Pack identity may be selected by a Host-side default."""

    binding = inspect.signature(transport_module.CredentialTransportBinding)
    factory = inspect.signature(HostBoundCredentialTransport.from_authorized_envelope)

    assert binding.parameters["consumer_pack_id"].default is inspect.Parameter.empty
    assert factory.parameters["consumer_pack_id"].default is inspect.Parameter.empty


def test_host_json_sanitizer_allows_unknown_provider_extensions() -> None:
    """Provider field selection belongs to the adapter Pack, not the Host."""

    assert transport_module._sanitize_json_response(
        {
            "provider_extension": {"trace": "safe"},
            "choices": [],
        },
        "credential-canary",
    ) == {"provider_extension": {"trace": "safe"}, "choices": []}


def _test_http_request(
    port: int,
    *,
    headers: dict[str, str] | None = None,
) -> urllib.request.Request:
    """Build a plaintext request only for the isolated pinned-opener tests."""
    return urllib.request.Request(
        f"http://provider.example:{port}/v1/messages",
        data=b"{}",
        headers=headers or {},
        method="POST",
    )


@pytest.mark.parametrize(
    "deadline",
    [
        pytest.param(time.time() - 60.0, id="past"),
        pytest.param(float("nan"), id="nan"),
        pytest.param(float("inf"), id="positive-infinity"),
        pytest.param(float("-inf"), id="negative-infinity"),
    ],
)
def test_transport_rejects_invalid_deadline_before_consumption_or_material_access(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    deadline: float,
) -> None:
    transport, arguments = _https_transport(tmp_path)
    touched: list[str] = []

    def resolve(*_args: object, **_kwargs: object) -> dict[str, Any]:
        touched.append("store")
        raise AssertionError("invalid deadline must not resolve credential material")

    def open_request(*_args: object, **_kwargs: object) -> _Response:
        touched.append("opener")
        raise AssertionError("invalid deadline must not open a provider request")

    monkeypatch.setattr(transport._store, "resolve", resolve)
    monkeypatch.setattr(transport, "_opener", open_request)
    arguments["deadline"] = deadline

    with pytest.raises(CredentialTransportDenied) as denied:
        transport.post_json(**arguments)

    assert denied.value.code == "binding_invalid"
    assert transport._consumed is False
    assert touched == []


@pytest.mark.parametrize(
    ("resolved_at", "opens_request"),
    [
        pytest.param(10.999, True, id="just-before-deadline"),
        pytest.param(11.0, False, id="at-deadline"),
        pytest.param(11.001, False, id="past-deadline"),
        pytest.param(float("nan"), False, id="nan-clock"),
        pytest.param(float("inf"), False, id="infinite-clock"),
    ],
)
def test_transport_rechecks_deadline_after_secret_resolution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    resolved_at: float,
    opens_request: bool,
) -> None:
    transport, arguments = _https_transport(tmp_path, secret="deadline-canary")
    clock = [10.0]
    opened: list[tuple[str | None, float]] = []
    material = {"api_key": "deadline-canary"}

    def resolve(*args: object, **kwargs: object) -> dict[str, Any]:
        del args, kwargs
        clock[0] = resolved_at
        return material

    def open_request(request: urllib.request.Request, *, timeout: float) -> _Response:
        opened.append((request.get_header("Authorization"), timeout))
        return _Response({})

    monkeypatch.setattr(transport._store, "resolve", resolve)
    monkeypatch.setattr(transport, "_monotonic_clock", lambda: clock[0])
    monkeypatch.setattr(transport, "_clock", lambda: 100.0)
    monkeypatch.setattr(transport, "_opener", open_request)
    arguments["deadline"] = 101.0

    if opens_request:
        assert transport.post_json(**arguments) == {}
        assert opened[0][0] == "Bearer deadline-canary"
        assert opened[0][1] == pytest.approx(0.001)
    else:
        with pytest.raises(CredentialTransportDenied) as denied:
            transport.post_json(**arguments)

        assert denied.value.code == "binding_invalid"
        assert opened == []
        assert "deadline-canary" not in str(denied.value)
        assert "deadline-canary" not in repr(material)


def _pin_test_network(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[list[str], list[tuple[str, int]]]:
    resolutions: list[str] = []
    connections: list[tuple[str, int]] = []

    def resolve(host: str, port: int, **_kwargs: Any) -> list[tuple[Any, ...]]:
        resolutions.append(host)
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port))]

    def connect(address: tuple[str, int], *args: Any, **kwargs: Any) -> socket.socket:
        timeout = args[0] if args else kwargs.get("timeout")
        connections.append(address)
        client = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        client.settimeout(timeout)
        client.connect(("127.0.0.1", address[1]))
        return client

    monkeypatch.setattr(transport_module.socket, "getaddrinfo", resolve)
    monkeypatch.setattr(transport_module.socket, "create_connection", connect)
    return resolutions, connections


def test_windows_credential_root_acl_is_hardened_with_argument_vector(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[list[str], dict[str, Any]]] = []

    def _run(argv: list[str], **kwargs: Any) -> None:
        calls.append((argv, kwargs))

    monkeypatch.setattr(store_module.subprocess, "run", _run)
    store_module._secure_windows_directory(tmp_path)

    assert len(calls) == 1
    argv, kwargs = calls[0]
    assert argv[:5] == [
        "powershell.exe",
        "-NoLogo",
        "-NoProfile",
        "-NonInteractive",
        "-Command",
    ]
    assert len(argv) == 6
    assert str(tmp_path) not in argv
    assert "$target = [Console]::In.ReadToEnd()" in argv[-1]
    assert "DirectorySecurity]::new()" in argv[-1]
    assert "SetAccessRuleProtection($true, $false)" in argv[-1]
    assert ".SetAccessControl($acl)" in argv[-1]
    assert "$rules.Count -ne 1" in argv[-1]
    assert kwargs == {
        "check": True,
        "capture_output": True,
        "text": True,
        "timeout": 15,
        "input": str(tmp_path),
    }


def test_windows_credential_root_acl_failure_is_redacted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _run(*_args: object, **_kwargs: object) -> None:
        raise store_module.subprocess.CalledProcessError(
            1,
            ["powershell.exe"],
            output="sensitive stdout",
            stderr="sensitive stderr",
        )

    monkeypatch.setattr(store_module.subprocess, "run", _run)
    with pytest.raises(PermissionError) as caught:
        store_module._secure_windows_directory(tmp_path)
    assert str(caught.value) == "credential Windows ACL could not be secured"
    assert "sensitive" not in str(caught.value)


def test_windows_credential_root_acl_is_applied_once_per_store(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = CredentialBrokerStore(user_data_root=tmp_path)
    secured: list[Path] = []
    monkeypatch.setattr(store_module.os, "name", "nt")
    monkeypatch.setattr(store_module, "_secure_windows_directory", secured.append)

    store._prepare_storage()
    store._prepare_storage()

    assert secured == [store.root]


def _dispatched_envelope(
    tmp_path: Path,
) -> tuple[_Harness, RequestEnvelope]:
    authority = _Harness(tmp_path / "authority")
    adapter = _adapter(authority)
    context = _context(authority)
    request_digest = _digest("credential-provider-request")
    _static, final = _queries(authority, context, request_digest)
    lease = adapter.authorize_and_issue_lease(final)
    adapter.recheck_effect_boundary(
        context,
        OpaqueAuthorityRef(authority.target.principal_id),
        lease,
    )
    return authority, RequestEnvelope(
        context=context,
        target_principal=OpaqueAuthorityRef(authority.target.principal_id),
        target_domain=OpaqueAuthorityRef(authority.target_domain.domain_id),
        contract_id="host.http",
        contract_version="1.0.0",
        operation_id=authority.target.operation_id,
        payload={"credential_handle": "opaque"},
        request_digest=request_digest,
        deadline_monotonic=9_999_999_999.0,
        lease=lease,
        idempotency_key=None,
    )


def test_credential_material_is_encrypted_and_listing_is_redacted(
    tmp_path: Path,
) -> None:
    service = CredentialBrokerService(user_data_root=tmp_path)

    created = service.invoke(
        "create",
        {
            "secret_material": {"api_key": "fixture-secret"},
            "profile_id": "profile-a",
            "consumer_pack_id": "provider-adapter-pack",
            "provider_instance_id": "provider.adapter-main",
            "scopes": ["generate", "stream"],
            "label": "fixture",
        },
    )
    listed = service.invoke("list", {"profile_id": "profile-a"})

    store_text = service.store.path.read_text(encoding="utf-8")
    assert "fixture-secret" not in store_text
    assert "ciphertext" not in str(listed)
    assert "secret_material" not in str(listed)
    assert created["handle"].startswith("credential:")
    restarted = CredentialBrokerService(user_data_root=tmp_path)
    restarted_list = restarted.invoke("list", {"profile_id": "profile-a"})
    assert restarted_list["credentials"][0]["handle"] == created["handle"]
    assert "fixture-secret" not in restarted.store.path.read_text(encoding="utf-8")


def test_git_credential_selection_is_exact_secret_free_and_fails_on_ambiguity(
    tmp_path: Path,
) -> None:
    store = CredentialBrokerStore(user_data_root=tmp_path)
    binding = {
        "endpoint_origin": "https://github.example",
        "workspace_id": "workspace-a",
    }
    created = store.create(
        secret_material={"token": "selection-secret"},
        consumer_pack_id="rumi_git_publish_pack",
        provider_instance_id="git-publish.service",
        profile_id="profile-1",
        scopes=["git.publish"],
        resource_binding=binding,
    )

    selected = store.select(
        consumer_pack_id="rumi_git_publish_pack",
        provider_instance_id="git-publish.service",
        profile_id="profile-1",
        scope="git.publish",
        resource_binding=binding,
    )

    assert selected == created
    assert "ciphertext" not in selected
    assert "selection-secret" not in json.dumps(selected)
    assert (
        store.select(
            consumer_pack_id="rumi_git_publish_pack",
            provider_instance_id="git-publish.service",
            profile_id="profile-1",
            scope="git.publish",
            resource_binding={**binding, "workspace_id": "workspace-other"},
        )
        is None
    )
    store.create(
        secret_material={"token": "second-secret"},
        consumer_pack_id="rumi_git_publish_pack",
        provider_instance_id="git-publish.service",
        profile_id="profile-1",
        scopes=["git.publish"],
        resource_binding=binding,
    )
    with pytest.raises(PermissionError, match="ambiguous"):
        store.select(
            consumer_pack_id="rumi_git_publish_pack",
            provider_instance_id="git-publish.service",
            profile_id="profile-1",
            scope="git.publish",
            resource_binding=binding,
        )


def test_git_credential_selection_ignores_expired_binding(tmp_path: Path) -> None:
    store = CredentialBrokerStore(user_data_root=tmp_path)
    binding = {
        "endpoint_origin": "https://github.example",
        "workspace_id": "workspace-a",
    }
    store.create(
        secret_material={"token": "expired-secret"},
        consumer_pack_id="rumi_git_publish_pack",
        provider_instance_id="git-publish.service",
        profile_id="profile-1",
        scopes=["git.publish"],
        resource_binding=binding,
        expires_at=time.time() - 1,
    )
    assert (
        store.select(
            consumer_pack_id="rumi_git_publish_pack",
            provider_instance_id="git-publish.service",
            profile_id="profile-1",
            scope="git.publish",
            resource_binding=binding,
        )
        is None
    )


@pytest.mark.parametrize(
    "expires_at",
    [True, "123", float("nan"), float("inf"), float("-inf")],
)
def test_credential_create_and_migration_reject_non_json_expiration(
    tmp_path: Path,
    expires_at: object,
) -> None:
    service = CredentialBrokerService(user_data_root=tmp_path / "service")
    payload = {
        "secret_material": {"token": "expiration-secret"},
        "profile_id": "profile-1",
        "consumer_pack_id": "rumi_git_publish_pack",
        "provider_instance_id": "git-publish.service",
        "scopes": ["git.publish"],
        "expires_at": expires_at,
    }

    with pytest.raises(ValueError, match="expires_at"):
        service.invoke("create", payload)

    store = CredentialBrokerStore(user_data_root=tmp_path / "migration")
    records = [payload]
    try:
        expected_hash = store_module._hash({"records": records})
    except ValueError:
        expected_hash = "sha256:" + "0" * 64
    with pytest.raises(ValueError):
        store.migrate(records, expected_source_hash=expected_hash)


@pytest.mark.parametrize(
    "expires_at",
    ["tomorrow", True, float("nan"), float("inf"), float("-inf")],
)
def test_tampered_expiration_fails_closed_on_select_and_resolve(
    tmp_path: Path,
    expires_at: object,
) -> None:
    store = CredentialBrokerStore(user_data_root=tmp_path)
    binding = {
        "endpoint_origin": "https://github.example",
        "workspace_id": "workspace-a",
    }
    created = store.create(
        secret_material={"token": "tampered-expiration-secret"},
        consumer_pack_id="rumi_git_publish_pack",
        provider_instance_id="git-publish.service",
        profile_id="profile-1",
        scopes=["git.publish"],
        resource_binding=binding,
    )
    state = store._read()
    record = state["credentials"][created["handle"]]
    record["expires_at"] = expires_at
    unsigned = {key: value for key, value in record.items() if key != "record_mac"}
    raw = json.dumps(
        unsigned,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    record["record_mac"] = store_module.hmac.new(
        store._fernet()._signing_key,
        raw,
        hashlib.sha256,
    ).hexdigest()
    store.path.write_text(
        json.dumps(state, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    common = {
        "consumer_pack_id": "rumi_git_publish_pack",
        "provider_instance_id": "git-publish.service",
        "profile_id": "profile-1",
        "scope": "git.publish",
        "resource_binding": binding,
    }
    try:
        selected = store.select(**common)
    except (PermissionError, ValueError):
        pass
    else:
        assert selected is None
    with pytest.raises((PermissionError, ValueError)):
        store.resolve(
            created["handle"],
            consumer_pack_id="rumi_git_publish_pack",
            provider_instance_id="git-publish.service",
            profile_id="profile-1",
            scope="git.publish",
            expected_resource_binding=binding,
        )

def test_host_composition_binds_the_real_host_credential_store(
    tmp_path: Path,
) -> None:
    from tobkiri_host.credential_store import host_credential_store_factory

    binding = host_credential_store_factory(user_data_root=tmp_path)

    assert isinstance(binding.store, CredentialBrokerStore)
    assert binding.store.user_data_root == tmp_path
    assert binding.key_version == store_module.KEY_VERSION


def test_authorized_git_selector_binds_profile_workspace_origin_and_is_one_shot(
    tmp_path: Path,
) -> None:
    service = CredentialBrokerService(user_data_root=tmp_path / "credential")
    created = service.store.create(
        secret_material={"token": "host-only-selection-secret"},
        consumer_pack_id="rumi_git_publish_pack",
        provider_instance_id="git-publish.service",
        profile_id="profile-1",
        scopes=["git.publish"],
        resource_binding={
            "endpoint_origin": "https://github.example",
            "workspace_id": "workspace-a",
        },
    )
    authority, envelope = _dispatched_envelope(tmp_path / "dispatch")
    transport = AuthorizedEnvelopeCredentialTransport(
        envelope=envelope,
        provider_principal=authority.target,
        store=service.store,
        authority_store=authority.store,
        current_security_epoch=lambda: authority.store.security_epoch,
        credential_key_version=store_module.KEY_VERSION,
        consumer_pack_id="rumi_git_publish_pack",
    )

    selected = transport.select_git_https_credential(
        workspace_id="workspace-a",
        endpoint_origin="https://github.example",
        provider_instance_id="git-publish.service",
        credential_scope="git.publish",
    )

    assert selected is not None
    assert selected["handle"] == created["handle"]
    assert selected["resource_binding"] == {
        "endpoint_origin": "https://github.example",
        "workspace_id": "workspace-a",
    }
    assert str(selected["binding_digest"]).startswith("sha256:")
    assert "host-only-selection-secret" not in json.dumps(selected)
    with pytest.raises(CredentialTransportDenied):
        transport.select_git_https_credential(
            workspace_id="workspace-a",
            endpoint_origin="https://github.example",
            provider_instance_id="git-publish.service",
            credential_scope="git.publish",
        )


def _authorized_git_selector(
    tmp_path: Path,
) -> tuple[
    CredentialBrokerService,
    dict[str, Any],
    _Harness,
    RequestEnvelope,
    AuthorizedEnvelopeCredentialTransport,
]:
    service = CredentialBrokerService(user_data_root=tmp_path / "credential")
    created = service.store.create(
        secret_material={"token": "receipt-bound-secret"},
        consumer_pack_id="rumi_git_publish_pack",
        provider_instance_id="git-publish.service",
        profile_id="profile-1",
        scopes=["git.publish"],
        resource_binding={
            "endpoint_origin": "https://github.example",
            "workspace_id": "workspace-a",
        },
    )
    authority, envelope = _dispatched_envelope(tmp_path / "dispatch")
    transport = AuthorizedEnvelopeCredentialTransport(
        envelope=envelope,
        provider_principal=authority.target,
        store=service.store,
        authority_store=authority.store,
        current_security_epoch=lambda: authority.store.security_epoch,
        credential_key_version=store_module.KEY_VERSION,
        consumer_pack_id="rumi_git_publish_pack",
    )
    return service, created, authority, envelope, transport


def _authorized_git_push_arguments(
    *,
    handle: str,
    receipt: str,
    workspace_id: str = "workspace-a",
) -> dict[str, Any]:
    return {
        "git_executable": "/usr/bin/git",
        "git_executable_identity": {"capture": "git"},
        "bare_repository": "/private/host-state/transport.git",
        "remote_url": "https://github.example/owner/repository.git",
        "refspec": "a" * 40 + ":refs/heads/main",
        "force_with_lease": "--force-with-lease=refs/heads/main:" + "0" * 40,
        "credential_handle": handle,
        "provider_instance_id": "git-publish.service",
        "credential_scope": "git.publish",
        "workspace_id": workspace_id,
        "selection_receipt": receipt,
    }


def test_authorized_git_push_rejects_skip_substitution_and_cross_workspace(
    tmp_path: Path,
) -> None:
    _service, created, _authority, _envelope, transport = _authorized_git_selector(
        tmp_path / "skip"
    )
    with pytest.raises(CredentialTransportDenied):
        transport.push_git_https(
            **_authorized_git_push_arguments(
                handle=created["handle"],
                receipt="credential-selection:forged",
            )
        )

    _service, created, _authority, _envelope, transport = _authorized_git_selector(
        tmp_path / "substitute"
    )
    selected = transport.select_git_https_credential(
        workspace_id="workspace-a",
        endpoint_origin="https://github.example",
        provider_instance_id="git-publish.service",
        credential_scope="git.publish",
    )
    assert selected is not None
    with pytest.raises(CredentialTransportDenied):
        transport.push_git_https(
            **_authorized_git_push_arguments(
                handle="credential:known-other-handle",
                receipt=str(selected["selection_receipt"]),
            )
        )

    _service, created, _authority, _envelope, transport = _authorized_git_selector(
        tmp_path / "workspace"
    )
    selected = transport.select_git_https_credential(
        workspace_id="workspace-a",
        endpoint_origin="https://github.example",
        provider_instance_id="git-publish.service",
        credential_scope="git.publish",
    )
    assert selected is not None
    with pytest.raises(CredentialTransportDenied):
        transport.push_git_https(
            **_authorized_git_push_arguments(
                handle=created["handle"],
                receipt=str(selected["selection_receipt"]),
                workspace_id="workspace-b",
            )
        )


def test_authorized_git_selection_succeeds_once_and_receipt_dies_on_restart(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, created, authority, envelope, transport = _authorized_git_selector(
        tmp_path
    )
    selected = transport.select_git_https_credential(
        workspace_id="workspace-a",
        endpoint_origin="https://github.example",
        provider_instance_id="git-publish.service",
        credential_scope="git.publish",
    )
    assert selected is not None
    captured_bindings: list[dict[str, str]] = []

    class _SelectedPush:
        def push_git_https(self, **_kwargs: object) -> str:
            return "selected push completed"

    def capture_transport(
        _cls: object,
        _envelope: RequestEnvelope,
        **kwargs: Any,
    ) -> _SelectedPush:
        captured_bindings.append(dict(kwargs["expected_resource_binding"]))
        return _SelectedPush()

    monkeypatch.setattr(
        HostBoundCredentialTransport,
        "from_authorized_envelope",
        classmethod(capture_transport),
    )
    arguments = _authorized_git_push_arguments(
        handle=created["handle"],
        receipt=str(selected["selection_receipt"]),
    )

    assert transport.push_git_https(**arguments) == "selected push completed"
    assert captured_bindings == [
        {
            "endpoint_origin": "https://github.example",
            "workspace_id": "workspace-a",
        }
    ]
    with pytest.raises(CredentialTransportDenied):
        transport.push_git_https(**arguments)

    restarted = AuthorizedEnvelopeCredentialTransport(
        envelope=envelope,
        provider_principal=authority.target,
        store=service.store,
        authority_store=authority.store,
        current_security_epoch=lambda: authority.store.security_epoch,
        credential_key_version=store_module.KEY_VERSION,
        consumer_pack_id="rumi_git_publish_pack",
    )
    with pytest.raises(CredentialTransportDenied):
        restarted.push_git_https(**arguments)


def test_store_decrypt_requires_exact_selected_resource_binding(
    tmp_path: Path,
) -> None:
    service, created, _authority, _envelope, _transport = _authorized_git_selector(
        tmp_path
    )
    common = {
        "consumer_pack_id": "rumi_git_publish_pack",
        "provider_instance_id": "git-publish.service",
        "profile_id": "profile-1",
        "scope": "git.publish",
        "key_version": store_module.KEY_VERSION,
        "purpose": "provider.invoke",
    }

    with pytest.raises(PermissionError, match="resource"):
        service.store.resolve(
            created["handle"],
            **common,
            expected_resource_binding={
                "endpoint_origin": "https://github.example",
                "workspace_id": "workspace-b",
            },
        )
    assert service.store.resolve(
        created["handle"],
        **common,
        expected_resource_binding={
            "endpoint_origin": "https://github.example",
            "workspace_id": "workspace-a",
        },
    ) == {"token": "receipt-bound-secret"}


def test_generic_resolution_is_denied_and_host_transport_applies_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = CredentialBrokerService(user_data_root=tmp_path)
    created = service.invoke(
        "create",
        {
            "secret_material": {"api_key": "fixture-secret"},
            "profile_id": "profile-1",
            "consumer_pack_id": "provider-adapter-pack",
            "provider_instance_id": "provider.adapter-main",
            "scopes": ["ai.generate"],
        },
    )

    with pytest.raises(PermissionError, match="Host transport"):
        service.invoke(
            "resolve",
            {
                "_contract_consumer_pack_id": "provider-adapter-pack",
                "handle": created["handle"],
                "provider_instance_id": "provider.adapter-main",
                "scope": "generate",
                "profile_id": "profile-1",
            },
        )

    authority, envelope = _dispatched_envelope(tmp_path)
    audit: list[dict[str, Any]] = []
    observed_authorization = []

    def opener(request, *, timeout):
        del timeout
        observed_authorization.append(request.get_header("Authorization"))
        return _Response(
            {
                "choices": [
                    {
                        "message": {"content": "fixture-secret must be redacted"},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {},
            }
        )

    monkeypatch.setattr(transport_module, "_open_pinned_request", opener)

    transport = HostBoundCredentialTransport.from_authorized_envelope(
        envelope,
        provider_principal=authority.target,
        store=service.store,
        authority_store=authority.store,
        credential_handle=created["handle"],
        credential_key_version=created["key_version"],
        provider_instance_id="provider.adapter-main",
        credential_scope="ai.generate",
        credential_purpose="provider.invoke",
        endpoint_origin="https://provider.example",
        current_security_epoch=lambda: authority.store.security_epoch,
        consumer_pack_id="provider-adapter-pack",
        audit_sink=lambda event: audit.append(dict(event)),
    )

    class CapturedSession:
        profile_id = "profile-1"
        plan_digest = _digest("credential-provider-plan")

        def invoke(self, contract_id, operation_id, payload, **_kwargs):
            assert (contract_id, operation_id, payload) == (
                REGISTRY_CONTRACT,
                REGISTRY_OPERATION,
                {"profile_id": "profile-1"},
            )
            return {
                "providers": [
                    {
                        "provider_instance_id": "provider.adapter-main",
                        "adapter_id": "openai-compatible",
                        "credential_handle": created["handle"],
                        "endpoint": "https://provider.example",
                        "enabled": True,
                    }
                ]
            }

        def provider_metadata(self, _contract_id):
            return ()

    client = GlobalContractClient(
        session=CapturedSession(),
        allowed_contract_ids=frozenset({REGISTRY_CONTRACT}),
        consumer_pack_id="rumi_provider_adapters_pack",
        host_credential_transport=transport,
    )
    result = create_generate_operation(client)(
        "generate",
        {
            "profile_id": "profile-1",
            "provider_id": "adapter-main",
            "model_id": "adapter-main/model",
            "messages": [{"role": "user", "content": "hello"}],
            "deadline": 9_999_999_999.0,
        },
    )

    assert observed_authorization == ["Bearer fixture-secret"]
    assert result["output"] == "[REDACTED] must be redacted"
    public_snapshot = json.dumps(
        {
            "result": result,
            "audit": audit,
            "binding": transport.binding.__dict__,
            "argv": sys.argv,
            "environment": dict(os.environ),
        },
        sort_keys=True,
    )
    assert "fixture-secret" not in public_snapshot
    assert audit[-1]["credential_handle"] == created["handle"]
    assert audit[-1]["provider_instance_id"] == "provider.adapter-main"
    with pytest.raises(CredentialTransportDenied, match="denied"):
        transport.post_json(
            endpoint="https://provider.example/v1/messages",
            headers={},
            body={},
            credential_handle=created["handle"],
            provider_instance_id="provider.adapter-main",
            credential_scope="ai.generate",
            credential_scheme="bearer",
            deadline=9_999_999_999.0,
        )


def test_credential_transport_rejects_plaintext_before_secret_resolution(
    tmp_path: Path,
) -> None:
    class NeverResolveStore:
        calls = 0

        def resolve(self, *_args: object, **_kwargs: object) -> dict[str, Any]:
            self.calls += 1
            raise AssertionError(
                "plaintext endpoint must not resolve credential material"
            )

    store = NeverResolveStore()
    authority, envelope = _dispatched_envelope(tmp_path / "binding")
    with pytest.raises(CredentialTransportDenied, match="denied") as binding_denied:
        HostBoundCredentialTransport.from_authorized_envelope(
            envelope,
            provider_principal=authority.target,
            store=store,
            authority_store=authority.store,
            credential_handle="credential:opaque-review-a",
            credential_key_version="key-version-review-a",
            provider_instance_id="provider.adapter-main",
            credential_scope="ai.generate",
            credential_purpose="provider.invoke",
            endpoint_origin="http://provider.example",
            current_security_epoch=lambda: authority.store.security_epoch,
            consumer_pack_id="provider-adapter-pack",
        )

    assert store.calls == 0
    assert "opaque-review-a" not in str(binding_denied.value)

    authority, envelope = _dispatched_envelope(tmp_path / "request")
    audit: list[dict[str, Any]] = []
    transport = HostBoundCredentialTransport.from_authorized_envelope(
        envelope,
        provider_principal=authority.target,
        store=store,
        authority_store=authority.store,
        credential_handle="credential:opaque-review-a",
        credential_key_version="key-version-review-a",
        provider_instance_id="provider.adapter-main",
        credential_scope="ai.generate",
        credential_purpose="provider.invoke",
        endpoint_origin="https://provider.example",
        current_security_epoch=lambda: authority.store.security_epoch,
        consumer_pack_id="provider-adapter-pack",
        audit_sink=lambda event: audit.append(dict(event)),
    )
    with pytest.raises(CredentialTransportDenied, match="denied") as request_denied:
        transport.post_json(
            endpoint="http://provider.example/v1/messages",
            headers={},
            body={},
            credential_handle="credential:opaque-review-a",
            provider_instance_id="provider.adapter-main",
            credential_scope="ai.generate",
            credential_scheme="bearer",
            deadline=9_999_999_999.0,
        )

    assert store.calls == 0
    assert "opaque-review-a" not in str(request_denied.value)
    assert audit[-1]["status"] == "denied"
    assert audit[-1]["endpoint_origin"] == ""


@pytest.mark.parametrize("same_port", [True, False])
def test_transport_does_not_follow_cross_origin_redirect_or_forward_secret(
    monkeypatch: pytest.MonkeyPatch,
    same_port: bool,
) -> None:
    target, TargetHandler = _start_provider(
        location="http://third.example/final",
    )
    target_port = int(target.server_address[1])
    source, SourceHandler = _start_provider()
    source_port = int(source.server_address[1])
    redirected_port = source_port if same_port else target_port
    SourceHandler.response_location = (
        f"http://disallowed.example:{redirected_port}/redirected"
    )
    resolutions, connections = _pin_test_network(monkeypatch)
    request = _test_http_request(
        source_port,
        headers={"authorization": "Bearer redirect-secret"},
    )
    try:
        with pytest.raises(CredentialTransportDenied, match="denied") as denied:
            transport_module._open_pinned_request(request, timeout=2)
    finally:
        source.shutdown()
        target.shutdown()
        source.server_close()
        target.server_close()

    assert SourceHandler.received == [
        {
            "authorization": "Bearer redirect-secret",
            "api_key": None,
            "host": f"provider.example:{source_port}",
            "path": "/v1/messages",
        }
    ]
    assert TargetHandler.received == []
    assert resolutions == ["provider.example"]
    assert connections == [("93.184.216.34", source_port)]
    assert "redirect-secret" not in str(denied.value)


def test_transport_rejects_unsafe_resolution_before_secret_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = _test_http_request(
        18080,
        headers={"Authorization": "Bearer redirect-secret"},
    )
    connected: list[tuple[str, int]] = []
    monkeypatch.setattr(
        transport_module.socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 18080))
        ],
    )
    monkeypatch.setattr(
        transport_module.socket,
        "create_connection",
        lambda address, *_args, **_kwargs: connected.append(address),
    )

    with pytest.raises(CredentialTransportDenied, match="denied") as denied:
        transport_module._open_pinned_request(request, timeout=2)

    assert connected == []
    assert "redirect-secret" not in str(denied.value)


def test_transport_pins_one_dns_answer_and_caps_response_bytes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source, SourceHandler = _start_provider()
    source_port = int(source.server_address[1])
    resolutions, connections = _pin_test_network(monkeypatch)
    try:
        with transport_module._open_pinned_request(
            _test_http_request(source_port),
            timeout=2,
        ) as response:
            assert response.read() == b"{}"
    finally:
        source.shutdown()
        source.server_close()

    assert len(SourceHandler.received) == 1
    assert resolutions == ["provider.example"]
    assert connections == [("93.184.216.34", source_port)]

    oversized = b'{"value":"' + b"x" * _MAX_RESPONSE_BYTES + b'"}'
    monkeypatch.setattr(
        transport_module,
        "_open_pinned_request",
        lambda *_args, **_kwargs: _BytesResponse(oversized),
    )
    transport, arguments = _https_transport(tmp_path)
    with pytest.raises(CredentialTransportDenied, match="denied"):
        transport.post_json(**arguments)


def test_revocation_prevents_later_resolution(tmp_path: Path) -> None:
    service = CredentialBrokerService(user_data_root=tmp_path)
    created = service.invoke(
        "create",
        {
            "secret_material": {"token": "fixture"},
            "profile_id": "profile-a",
            "consumer_pack_id": "provider-adapter-pack",
            "provider_instance_id": "adapter-main",
            "scopes": ["generate"],
        },
    )

    service.invoke("revoke", {"handle": created["handle"], "profile_id": "profile-a"})

    with pytest.raises(PermissionError):
        service.invoke(
            "resolve",
            {
                "_contract_consumer_pack_id": "provider-adapter-pack",
                "handle": created["handle"],
                "provider_instance_id": "adapter-main",
                "scope": "generate",
                "profile_id": "profile-a",
            },
        )


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("provider_instance_id", "adapter-other"),
        ("credential_scope", "stream"),
        ("credential_handle", "credential:forged"),
        ("endpoint", "https://other.example/v1/messages"),
    ),
)
def test_host_transport_rejects_wrong_exact_binding(
    tmp_path: Path,
    field: str,
    value: str,
) -> None:
    service = CredentialBrokerService(user_data_root=tmp_path)
    created = service.invoke(
        "create",
        {
            "secret_material": {"api_key": "binding-sentinel"},
            "profile_id": "profile-1",
            "consumer_pack_id": "provider-adapter-pack",
            "provider_instance_id": "adapter-main",
            "scopes": ["generate"],
        },
    )
    authority, envelope = _dispatched_envelope(tmp_path)
    transport = HostBoundCredentialTransport.from_authorized_envelope(
        envelope,
        provider_principal=authority.target,
        store=service.store,
        authority_store=authority.store,
        credential_handle=created["handle"],
        credential_key_version=created["key_version"],
        provider_instance_id="provider.adapter-main",
        credential_scope="generate",
        credential_purpose="provider.invoke",
        endpoint_origin="https://provider.example",
        current_security_epoch=lambda: authority.store.security_epoch,
        consumer_pack_id="provider-adapter-pack",
    )
    arguments = {
        "endpoint": "https://provider.example/v1/messages",
        "headers": {},
        "body": {},
        "credential_handle": created["handle"],
        "provider_instance_id": "adapter-main",
        "credential_scope": "generate",
        "credential_scheme": "bearer",
        "deadline": 9_999_999_999.0,
    }
    arguments[field] = value
    with pytest.raises(CredentialTransportDenied, match="denied") as denied:
        transport.post_json(**arguments)
    assert "binding-sentinel" not in str(denied.value)


def test_host_git_transport_resolves_only_inside_one_exact_push(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The Git transport gives the Host runner, not a Pack, credential material."""

    canary = "git-credential-sentinel"
    service = CredentialBrokerService(user_data_root=tmp_path / "credential")
    created = service.invoke(
        "create",
        {
            "secret_material": {"token": canary},
            "profile_id": "profile-1",
            "consumer_pack_id": "rumi_git_publish_pack",
            "provider_instance_id": "git-publish.service",
            "scopes": ["git.publish"],
            "resource_binding": {
                "endpoint_origin": "https://github.example",
                "workspace_id": "workspace-a",
            },
        },
    )
    authority, envelope = _dispatched_envelope(tmp_path / "dispatch")
    transport = HostBoundCredentialTransport.from_authorized_envelope(
        envelope,
        provider_principal=authority.target,
        store=service.store,
        authority_store=authority.store,
        credential_handle=created["handle"],
        credential_key_version=created["key_version"],
        provider_instance_id="git-publish.service",
        credential_scope="git.publish",
        credential_purpose="provider.invoke",
        endpoint_origin="https://github.example",
        current_security_epoch=lambda: authority.store.security_epoch,
        consumer_pack_id="rumi_git_publish_pack",
        expected_resource_binding={
            "endpoint_origin": "https://github.example",
            "workspace_id": "workspace-a",
        },
    )
    bare = tmp_path / "transport.git"
    bare.mkdir()
    captured: dict[str, str] = {}
    captured_identity: dict[str, object] = {}

    def trusted_git(
        _value: str,
        *,
        expected_identity: dict[str, object],
    ) -> Path:
        captured_identity.update(expected_identity)
        return Path("/usr/bin/git")

    monkeypatch.setattr(
        transport_module,
        "_trusted_git_executable",
        trusted_git,
    )
    monkeypatch.setattr(
        transport_module, "_bare_repository", lambda value, _exe: Path(value)
    )

    def run_push(**kwargs: str) -> str:
        captured.update(kwargs)
        return f"published {kwargs['secret']}"

    monkeypatch.setattr(transport_module, "_run_credentialed_git_push", run_push)
    result = transport.push_git_https(
        git_executable="/usr/bin/git",
        git_executable_identity={"capture": "git"},
        bare_repository=str(bare),
        remote_url="https://github.example/owner/repository.git",
        refspec="a" * 40 + ":refs/heads/main",
        force_with_lease="--force-with-lease=refs/heads/main:" + "0" * 40,
        credential_handle=created["handle"],
        provider_instance_id="git-publish.service",
        credential_scope="git.publish",
    )

    assert captured["username"] == "x-access-token"
    assert captured["secret"] == canary
    assert captured_identity == {"capture": "git"}
    assert result == "published [REDACTED]"
    assert canary not in result
    assert transport._consumed is True
    with pytest.raises(CredentialTransportDenied):
        transport.push_git_https(
            git_executable="/usr/bin/git",
            git_executable_identity={"capture": "git"},
            bare_repository=str(bare),
            remote_url="https://github.example/owner/repository.git",
            refspec="a" * 40 + ":refs/heads/main",
            force_with_lease="--force-with-lease=refs/heads/main:" + "0" * 40,
            credential_handle=created["handle"],
            provider_instance_id="git-publish.service",
            credential_scope="git.publish",
        )


@pytest.mark.parametrize(
    "response",
    (
        {"credential-canary": "value"},
        {"choices": [{"credential-canary": "nested-value"}]},
    ),
)
def test_host_transport_rejects_material_in_response_keys_without_observable_leak(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture[str],
    response: dict[str, Any],
) -> None:
    canary = "credential-canary"
    transport, arguments = _https_transport(tmp_path, secret=canary)
    audit: list[dict[str, Any]] = []
    transport._audit_sink = audit.append
    monkeypatch.setattr(
        transport, "_opener", lambda *_args, **_kwargs: _Response(response)
    )

    with pytest.raises(CredentialTransportDenied) as caught:
        transport.post_json(**arguments)

    error = caught.value
    assert error.code == "response_invalid"
    assert error.__cause__ is None
    assert error.__context__ is None
    print(json.dumps({"args": error.args, "audit": audit}, sort_keys=True))
    captured = capsys.readouterr()
    observable = "\n".join(
        (str(error), repr(error), caplog.text, captured.out, captured.err)
    )
    assert observable.count(canary) == 0


@pytest.mark.parametrize("failure_source", ("store", "provider"))
def test_host_transport_severs_material_bearing_exception_chains(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture[str],
    failure_source: str,
) -> None:
    canary = "exception-material-canary"
    transport, arguments = _https_transport(tmp_path, secret=canary)
    audit: list[dict[str, Any]] = []
    transport._audit_sink = audit.append

    def fail(*_args: object, **_kwargs: object) -> Any:
        try:
            raise ValueError(canary)
        except ValueError as cause:
            raise RuntimeError({canary: [canary]}) from cause

    if failure_source == "store":
        monkeypatch.setattr(transport._store, "resolve", fail)
    else:
        monkeypatch.setattr(transport, "_opener", fail)

    with pytest.raises(CredentialTransportDenied) as caught:
        transport.post_json(**arguments)

    error = caught.value
    expected = "store_failure" if failure_source == "store" else "provider_failure"
    assert error.code == expected
    assert error.__cause__ is None
    assert error.__context__ is None
    print(json.dumps({"args": error.args, "audit": audit}, sort_keys=True))
    captured = capsys.readouterr()
    observable = "\n".join(
        (str(error), repr(error), caplog.text, captured.out, captured.err)
    )
    assert observable.count(canary) == 0


def test_host_transport_sanitizes_values_and_isolates_material_bearing_audit_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture[str],
) -> None:
    canary = "successful-material-canary"
    transport, arguments = _https_transport(tmp_path, secret=canary)
    original_resolve = transport._store.resolve

    def resolve_with_sensitive_key(*args: object, **kwargs: object) -> dict[str, Any]:
        return {**original_resolve(*args, **kwargs), canary: {canary: canary}}

    def failing_audit(_event: Mapping[str, Any]) -> None:
        raise RuntimeError({canary: canary})

    monkeypatch.setattr(transport._store, "resolve", resolve_with_sensitive_key)
    monkeypatch.setattr(transport, "_audit_sink", failing_audit)
    monkeypatch.setattr(
        transport,
        "_opener",
        lambda *_args, **_kwargs: _Response(
            {
                "id": "response-1",
                "model": "model-1",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": f"ok {canary}"},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {
                    "prompt_tokens": 1,
                    "completion_tokens": 1,
                    "total_tokens": 2,
                },
            }
        ),
    )

    result = transport.post_json(**arguments)
    print(json.dumps(result, sort_keys=True))
    captured = capsys.readouterr()
    observable = "\n".join((repr(result), caplog.text, captured.out, captured.err))
    assert observable.count(canary) == 0
    assert result["id"] == "response-1"
    assert result["model"] == "model-1"
    assert result["choices"][0]["message"]["content"] == "ok [REDACTED]"


def test_global_client_normalizes_injected_transport_exception_without_chain(
    caplog: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture[str],
) -> None:
    canary = "adapter-exception-material-canary"

    class FailingTransport:
        def post_json(self, **_kwargs: object) -> dict[str, Any]:
            try:
                raise ValueError(canary)
            except ValueError as cause:
                raise RuntimeError({canary: canary}) from cause

    client = GlobalContractClient(
        session=object(),  # type: ignore[arg-type]
        allowed_contract_ids=frozenset(),
        consumer_pack_id="provider-adapter-pack",
        host_credential_transport=FailingTransport(),
    )
    with pytest.raises(HostCredentialTransportError) as caught:
        client.post_json_with_credential(
            endpoint="https://provider.example/v1/messages",
            headers={},
            body={},
            credential_handle="credential:opaque",
            provider_instance_id="provider.adapter-main",
            credential_scope="ai.generate",
            credential_scheme="bearer",
            deadline=9_999_999_999.0,
        )

    error = caught.value
    assert error.code == "host_credential_transport_failed"
    assert error.__cause__ is None
    assert error.__context__ is None
    print(json.dumps({"args": error.args}, sort_keys=True))
    captured = capsys.readouterr()
    observable = "\n".join(
        (str(error), repr(error), caplog.text, captured.out, captured.err)
    )
    assert observable.count(canary) == 0


def test_global_client_normalizes_injected_git_transport_exception_without_chain(
    caplog: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Git credential failures project the same material-free public error."""

    canary = "git-transport-exception-canary"

    class FailingTransport:
        def push_git_https(self, **_kwargs: object) -> str:
            raise RuntimeError(canary)

    client = GlobalContractClient(
        session=object(),  # type: ignore[arg-type]
        allowed_contract_ids=frozenset(),
        consumer_pack_id="rumi_git_publish_pack",
        host_credential_transport=FailingTransport(),
    )
    with pytest.raises(HostCredentialTransportError) as caught:
        client.push_git_https_with_credential(
            git_executable="/usr/bin/git",
            git_executable_identity={"capture": "git"},
            bare_repository="/private/host-state/transport.git",
            remote_url="https://github.example/owner/repository.git",
            refspec="a" * 40 + ":refs/heads/main",
            force_with_lease="--force-with-lease=refs/heads/main:" + "0" * 40,
            credential_handle="credential:opaque",
            provider_instance_id="git-publish.service",
            credential_scope="git.publish",
            workspace_id="workspace-a",
            selection_receipt="credential-selection:test",
        )

    error = caught.value
    assert error.code == "host_credential_transport_failed"
    assert error.__cause__ is None
    assert error.__context__ is None
    print(json.dumps({"args": error.args}, sort_keys=True))
    captured = capsys.readouterr()
    observable = "\n".join(
        (str(error), repr(error), caplog.text, captured.out, captured.err)
    )
    assert canary not in observable


def test_host_transport_rejects_missing_approval_and_revocation(
    tmp_path: Path,
) -> None:
    service = CredentialBrokerService(user_data_root=tmp_path)
    created = service.invoke(
        "create",
        {
            "secret_material": {"api_key": "epoch-sentinel"},
            "profile_id": "profile-1",
            "consumer_pack_id": "provider-adapter-pack",
            "provider_instance_id": "adapter-main",
            "scopes": ["generate"],
        },
    )
    authority, envelope = _dispatched_envelope(tmp_path)
    forged = replace(envelope, lease=type(envelope.lease)(b"forged"))
    with pytest.raises(CredentialTransportDenied, match="denied"):
        HostBoundCredentialTransport.from_authorized_envelope(
            forged,
            provider_principal=authority.target,
            store=service.store,
            authority_store=authority.store,
            credential_handle=created["handle"],
            credential_key_version=created["key_version"],
            provider_instance_id="provider.adapter-main",
            credential_scope="generate",
            credential_purpose="provider.invoke",
            endpoint_origin="https://provider.example",
            current_security_epoch=lambda: authority.store.security_epoch,
            consumer_pack_id="provider-adapter-pack",
        )

    transport = HostBoundCredentialTransport.from_authorized_envelope(
        envelope,
        provider_principal=authority.target,
        store=service.store,
        authority_store=authority.store,
        credential_handle=created["handle"],
        credential_key_version=created["key_version"],
        provider_instance_id="adapter-main",
        credential_scope="generate",
        credential_purpose="provider.invoke",
        endpoint_origin="https://provider.example",
        current_security_epoch=lambda: authority.store.security_epoch,
        consumer_pack_id="provider-adapter-pack",
    )
    authority.kernel.revoke(
        target_kind="function_principal",
        target_id=authority.target.principal_id,
        reason="test credential revoke",
    )
    with pytest.raises(CredentialTransportDenied, match="denied"):
        transport.post_json(
            endpoint="https://provider.example/v1/messages",
            headers={},
            body={},
            credential_handle=created["handle"],
            provider_instance_id="adapter-main",
            credential_scope="generate",
            credential_scheme="bearer",
            deadline=9_999_999_999.0,
        )


def test_credential_migration_is_atomic_redacted_and_reversible(
    tmp_path: Path,
) -> None:
    service = CredentialBrokerService(user_data_root=tmp_path)
    source = {
        "records": [
            {
                "consumer_pack_id": "rumi_provider_adapters_pack",
                "provider_instance_id": "provider.example",
                "scopes": ["ai.generate"],
                "profile_id": "profile-a",
                "secret_material": {"api_key": "not-returned"},
            }
        ]
    }
    raw = json.dumps(source, sort_keys=True, separators=(",", ":")).encode("utf-8")
    result = service.invoke(
        "migration.apply",
        {
            **source,
            "expected_source_hash": "sha256:" + hashlib.sha256(raw).hexdigest(),
        },
    )

    assert result["credentials"][0]["handle"].startswith("credential:")
    assert "not-returned" not in str(result)
    assert service.invoke(
        "migration.rollback", {"migration_id": result["migration_id"]}
    )["rolled_back"]
    assert service.invoke("list", {"profile_id": "profile-a"})["count"] == 0
