"""Host-local inference authority stays narrower than writable Pack routing."""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, replace
from pathlib import Path
from threading import Event
from types import SimpleNamespace
from typing import Any

import pytest

from core_runtime import local_model_transport
from core_runtime.authority.v4 import FunctionPrincipal, LeaseState
from core_runtime.global_contract_dispatch import GlobalContractInvocationError
from core_runtime.local_model_authority import (
    ALLOWLIST_VERSION,
    LOCAL_ADAPTER,
    LocalModelInvocationBinding,
    create_local_model_transport,
)
from ecosystem.rumi_provider_adapters_pack.runtime.adapter import (
    create_generate_operation,
    create_stream_operation,
)
from tobkiri_protocol.secure_persistence import SecureDirectory

PROFILE = "defaults"
CONNECTION = "provider.local-liquid"
MODEL = "LiquidAI/LFM2-1.2B"
ENDPOINT = "http://127.0.0.1:18080/v1"
ADAPTER_FUNCTION = "rumi_provider_adapters_pack.provider.compatibility."
ADAPTER_OPERATION = "rumi_provider_adapters_pack.provider-"


def _digest(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode()).hexdigest()


def _principal(kind: str = "generate") -> FunctionPrincipal:
    return FunctionPrincipal(
        parent_artifact_digest=_digest("adapter-artifact"),
        function_implementation_digest=_digest("adapter-implementation"),
        function_id=ADAPTER_FUNCTION + kind,
        contract_revision_digest=_digest("contract-" + kind),
        operation_id=ADAPTER_OPERATION + kind,
    )


def _captured_binding(kind: str = "generate") -> LocalModelInvocationBinding:
    return LocalModelInvocationBinding(
        _principal(kind), f"tobkiri.service.ai.provider.{kind}.v1", "1.0.0",
        _principal("registry"), "fixture.registry.read",
    )


class AuthorityStoreDouble:
    """Expose mutable durable authorization without inventing an active grant."""

    def __init__(self, envelope: Any, principal: FunctionPrincipal) -> None:
        context = envelope.context
        self.security_epoch = context.security_epoch
        self.state = LeaseState.DISPATCHED
        self.revoked: set[tuple[str, str]] = set()
        self.tokens: list[str] = []
        self.inspection_error: Exception | None = None
        self.durable = SimpleNamespace(
            target=principal,
            caller=SimpleNamespace(principal_id=context.caller_principal.value),
            profile_id=context.profile_id,
            activation_id=context.activation_id,
            security_epoch=context.security_epoch,
            caller_domain_id="domain.caller",
            target_domain_id=context.target_domain_id,
            target_boot_epoch=context.target_boot_epoch,
            request_id=context.request_id,
            request_digest=envelope.request_digest,
            grant_id="grant.adapter.generate",
            provider_authority_id="authority.adapter.generate",
        )

    def inspect_lease_token(self, token: str) -> tuple[Any, LeaseState]:
        self.tokens.append(token)
        if self.inspection_error is not None:
            raise self.inspection_error
        return self.durable, self.state

    def is_revoked(self, kind: str, identity: str) -> bool:
        return (kind, identity) in self.revoked


@dataclass
class AuthorityHarness:
    root: Path
    principal: FunctionPrincipal
    envelope: Any
    store: AuthorityStoreDouble
    snapshot: dict[str, Any]
    directory: SecureDirectory
    document: dict[str, Any]
    binding: LocalModelInvocationBinding
    current_error: Exception | None = None
    current_checks: int = 0

    def assert_current(self) -> None:
        self.current_checks += 1
        if self.current_error is not None:
            raise self.current_error

    def write_allowlist(self) -> None:
        self.directory.write_bytes_atomic("allowlist.json", json.dumps(self.document).encode())

    def transport(self) -> Any:
        return create_local_model_transport(
            envelope=self.envelope,
            principal=self.principal,
            authority_store=self.store,
            user_data_root=self.root,
            registry_snapshot=lambda: self.snapshot,
            assert_current=self.assert_current,
            binding=self.binding,
        )

    def post(self, transport: Any | None = None, **body_changes: Any) -> Any:
        if transport is None:
            transport = self.transport()
        assert transport is not None, "the real adapter operation needs local transport"
        return transport.post_chat(
            provider_instance_id=CONNECTION,
            body={
                "model": MODEL,
                "messages": [{"role": "user", "content": "Hello"}],
                "stream": False,
                **body_changes,
            },
            deadline=time.time() + 30,
        )


@pytest.fixture
def authority(tmp_path: Path) -> AuthorityHarness:
    principal = _principal()
    context = SimpleNamespace(
        caller_principal=SimpleNamespace(value="principal.gateway"),
        profile_id=PROFILE,
        activation_id="activation.fixture",
        security_epoch=3,
        target_domain_id="domain.adapter",
        target_boot_epoch=2,
        request_id="request.local-model",
    )
    envelope = SimpleNamespace(
        context=context,
        operation_id=principal.operation_id,
        contract_id="tobkiri.service.ai.provider.generate.v1",
        contract_version="1.0.0",
        target_principal=SimpleNamespace(value=principal.principal_id),
        target_domain=SimpleNamespace(value=context.target_domain_id),
        request_digest=_digest("request"),
        lease=SimpleNamespace(token=b"lease.fixture"),
        cancellation_requested=Event(),
        deadline_monotonic=time.monotonic() + 30,
    )
    document = {
        "version": ALLOWLIST_VERSION,
        "registrations": [
            {
                "profile_id": PROFILE,
                "provider_instance_id": CONNECTION,
                "endpoint": ENDPOINT,
                "model_ids": [MODEL],
            }
        ],
    }
    snapshot = {
        "profile_id": PROFILE,
        "revision": 1,
        "providers": [
            {
                "provider_instance_id": CONNECTION,
                "adapter_id": LOCAL_ADAPTER,
                "endpoint": ENDPOINT,
                "credential_handle": None,
                "enabled": True,
                "record_revision": 1,
            }
        ],
    }
    directory = SecureDirectory(tmp_path / "host_local_models")
    harness = AuthorityHarness(
        tmp_path,
        principal,
        envelope,
        AuthorityStoreDouble(envelope, principal),
        snapshot,
        directory,
        document,
        _captured_binding(),
    )
    harness.write_allowlist()
    return harness


@pytest.fixture
def wire(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Record every network effect; never open a real loopback listener."""
    state = SimpleNamespace(created=[], connected=[], sent=[], connect_hook=None)

    class SocketDouble:
        def shutdown(self, _how: int) -> None:
            pass

    class ResponseDouble:
        status = 200

        def __enter__(self) -> Any:
            return self

        def __exit__(self, *_args: object) -> None:
            pass

        def read(self, _limit: int) -> bytes:
            return json.dumps(
                {
                    "choices": [
                        {
                            "message": {"content": "Hello from the local model"},
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {"prompt_tokens": 1, "completion_tokens": 2},
                }
            ).encode()

    class ConnectionDouble:
        def __init__(self, host: str, port: int, *, timeout: float) -> None:
            state.created.append((host, port, timeout))
            self.sock = None

        def connect(self) -> None:
            state.connected.append(True)
            self.sock = SocketDouble()
            if state.connect_hook is not None:
                state.connect_hook()

        def request(
            self,
            method: str,
            path: str,
            body: bytes,
            headers: dict[str, str],
        ) -> None:
            state.sent.append((method, path, json.loads(body), headers))

        def getresponse(self) -> Any:
            return ResponseDouble()

        def close(self) -> None:
            pass

    monkeypatch.setattr(local_model_transport.http.client, "HTTPConnection", ConnectionDouble)
    return state


@pytest.mark.parametrize("kind", ["generate", "stream"])
def test_real_adapter_function_and_operation_receive_local_transport(
    authority: AuthorityHarness,
    wire: Any,
    kind: str,
) -> None:
    authority.principal = _principal(kind)
    authority.binding = _captured_binding(kind)
    authority.envelope.operation_id = authority.principal.operation_id
    authority.envelope.contract_id = f"tobkiri.service.ai.provider.{kind}.v1"
    authority.envelope.target_principal.value = authority.principal.principal_id
    authority.store.durable.target = authority.principal
    result = authority.post()
    assert result["choices"][0]["message"]["content"]
    assert wire.created[0][:2] == ("127.0.0.1", 18080)
    assert wire.sent[0][:2] == ("POST", "/v1/chat/completions")
    assert not any(key.lower() == "authorization" for key in wire.sent[0][3])
    assert set(authority.store.tokens) == {"lease.fixture"}


@pytest.mark.parametrize("kind", ["embedding", "image", "foreign"])
def test_other_functions_never_receive_local_transport(
    authority: AuthorityHarness,
    wire: Any,
    kind: str,
) -> None:
    authority.principal = _principal(kind)
    authority.envelope.operation_id = authority.principal.operation_id
    authority.envelope.contract_id = f"tobkiri.service.ai.provider.{kind}.v1"
    authority.envelope.target_principal.value = authority.principal.principal_id
    assert authority.transport() is None
    assert not wire.created


def test_captured_text_provider_does_not_require_a_product_pack_name(
    authority: AuthorityHarness, wire: Any,
) -> None:
    principal = replace(
        authority.principal, function_id="third_party.text.generate",
        operation_id="third_party.text.infer",
    )
    authority.principal = principal
    authority.binding = replace(authority.binding, principal=principal)
    authority.envelope.operation_id = principal.operation_id
    authority.envelope.target_principal.value = principal.principal_id
    authority.store.durable.target = principal
    assert authority.post()["choices"]
    assert len(wire.sent) == 1


@pytest.mark.parametrize("field", [
    "parent_artifact_digest", "function_implementation_digest",
    "contract_revision_digest", "operation_id",
])
def test_changed_identity_cannot_reuse_captured_local_transport(
    authority: AuthorityHarness, wire: Any, field: str,
) -> None:
    value = "third_party.other-operation" if field == "operation_id" else _digest(field)
    principal = replace(authority.principal, **{field: value})
    authority.principal = principal
    authority.envelope.operation_id = principal.operation_id
    authority.envelope.target_principal.value = principal.principal_id
    authority.store.durable.target = principal
    assert authority.transport() is None
    assert not wire.created


def test_contract_version_must_match_the_captured_binding(
    authority: AuthorityHarness, wire: Any,
) -> None:
    authority.envelope.contract_version = "2.0.0"
    assert authority.transport() is None
    assert not wire.created


def test_absent_capture_never_receives_local_transport(
    authority: AuthorityHarness, wire: Any,
) -> None:
    assert create_local_model_transport(
        envelope=authority.envelope, principal=authority.principal,
        authority_store=authority.store, user_data_root=authority.root,
        registry_snapshot=lambda: authority.snapshot,
        assert_current=authority.assert_current,
    ) is None
    assert not wire.created


@pytest.mark.parametrize(
    "mismatch", ["operation", "principal", "function_alias", "contract", "principal_operation"]
)
def test_function_operation_and_target_principal_must_match_exactly(
    authority: AuthorityHarness,
    wire: Any,
    mismatch: str,
) -> None:
    if mismatch == "operation":
        authority.envelope.operation_id = ADAPTER_OPERATION + "stream"
    elif mismatch == "principal":
        authority.envelope.target_principal.value = _digest("foreign-principal")
    elif mismatch == "contract":
        authority.envelope.contract_id = "tobkiri.service.ai.provider.stream.v1"
    elif mismatch == "principal_operation":
        authority.principal = replace(
            authority.principal, operation_id=ADAPTER_OPERATION + "stream"
        )
        authority.envelope.target_principal.value = authority.principal.principal_id
    else:
        authority.envelope.operation_id = authority.principal.function_id
    assert authority.transport() is None
    assert not wire.created


@pytest.mark.parametrize(
    "state",
    [
        LeaseState.ISSUED,
        LeaseState.COMMITTED,
        LeaseState.FAILED,
        LeaseState.AMBIGUOUS,
        LeaseState.REVOKED,
        LeaseState.EXPIRED,
    ],
)
def test_only_live_dispatched_lease_can_send(
    authority: AuthorityHarness,
    wire: Any,
    state: LeaseState,
) -> None:
    transport = authority.transport()
    authority.store.state = state
    with pytest.raises(PermissionError):
        authority.post(transport)
    assert not wire.created


@pytest.mark.parametrize(
    "field,value",
    [
        ("profile_id", "foreign"),
        ("activation_id", "activation.foreign"),
        ("security_epoch", 2),
        ("target_domain_id", "domain.foreign"),
        ("target_boot_epoch", 1),
        ("request_id", "request.foreign"),
        ("request_digest", _digest("foreign-request")),
    ],
)
def test_original_lease_binding_cannot_be_substituted(
    authority: AuthorityHarness,
    wire: Any,
    field: str,
    value: Any,
) -> None:
    transport = authority.transport()
    setattr(authority.store.durable, field, value)
    with pytest.raises(PermissionError):
        authority.post(transport)
    assert not wire.created


@pytest.mark.parametrize("target", ["caller", "target", "epoch"])
def test_caller_target_and_current_security_epoch_remain_bound(
    authority: AuthorityHarness,
    wire: Any,
    target: str,
) -> None:
    transport = authority.transport()
    if target == "caller":
        authority.store.durable.caller.principal_id = "principal.foreign"
    elif target == "target":
        authority.store.durable.target = _principal("stream")
    else:
        authority.store.security_epoch += 1
    with pytest.raises(PermissionError):
        authority.post(transport)
    assert not wire.created


@pytest.mark.parametrize(
    "kind,field",
    [
        ("function_principal", "caller"),
        ("function_principal", "target"),
        ("execution_domain", "caller_domain_id"),
        ("execution_domain", "target_domain_id"),
        ("profile", "profile_id"),
        ("activation", "activation_id"),
        ("grant", "grant_id"),
        ("provider_authority", "provider_authority_id"),
    ],
)
def test_individual_revocation_denies_without_security_epoch_change(
    authority: AuthorityHarness,
    wire: Any,
    kind: str,
    field: str,
) -> None:
    transport = authority.transport()
    target = getattr(authority.store.durable, field)
    identity = target.principal_id if field in {"caller", "target"} else target
    authority.store.revoked.add((kind, identity))
    with pytest.raises(PermissionError):
        authority.post(transport)
    assert authority.store.security_epoch == 3
    assert not wire.created


@pytest.mark.parametrize("reason", ["cancelled", "expired", "stale", "missing_store"])
def test_failed_or_ended_authority_does_not_connect(
    authority: AuthorityHarness,
    wire: Any,
    reason: str,
) -> None:
    transport = authority.transport()
    if reason == "cancelled":
        authority.envelope.cancellation_requested.set()
    elif reason == "expired":
        authority.envelope.deadline_monotonic = time.monotonic() - 1
    elif reason == "stale":
        authority.current_error = PermissionError("captured activation changed")
    else:
        authority.store.inspection_error = RuntimeError("private-store-detail")
    with pytest.raises(PermissionError) as error:
        authority.post(transport)
    assert "private-store-detail" not in str(error.value)
    assert not wire.created


@pytest.mark.parametrize(
    "mutation",
    [
        "missing",
        "duplicate",
        "foreign_profile",
        "foreign_provider",
        "invalid_schema",
    ],
)
def test_registry_record_alone_cannot_grant_host_loopback_access(
    authority: AuthorityHarness,
    wire: Any,
    mutation: str,
) -> None:
    if mutation == "missing":
        authority.directory.unlink("allowlist.json")
    elif mutation == "duplicate":
        authority.document["registrations"].append(dict(authority.document["registrations"][0]))
        authority.write_allowlist()
    else:
        if mutation == "invalid_schema":
            authority.document["trusted"] = True
        else:
            field = "profile_id" if mutation == "foreign_profile" else "provider_instance_id"
            authority.document["registrations"][0][field] = "foreign"
        authority.write_allowlist()
    with pytest.raises((OSError, PermissionError)):
        authority.post()
    assert not wire.created


@pytest.mark.parametrize(
    "field,value",
    [
        ("adapter_id", "openai-compatible"),
        ("enabled", False),
        ("enabled", 1),
        ("credential_handle", "credential:injected"),
        ("credential_handle", ""),
        ("endpoint", "http://127.0.0.1:18081/v1"),
        ("endpoint", "http://169.254.169.254/v1"),
        ("record_revision", 0),
    ],
)
def test_pack_registry_mutation_cannot_expand_owner_approval(
    authority: AuthorityHarness,
    wire: Any,
    field: str,
    value: Any,
) -> None:
    authority.snapshot["providers"][0][field] = value
    with pytest.raises((PermissionError, ValueError)):
        authority.post()
    assert not wire.created


def test_foreign_profile_registry_cannot_select_local_runner(
    authority: AuthorityHarness,
    wire: Any,
) -> None:
    authority.snapshot["profile_id"] = "foreign"
    with pytest.raises(PermissionError):
        authority.post()
    assert not wire.created


@pytest.mark.parametrize(
    "endpoint",
    [
        "http://localhost:18080/v1",
        "http://127.0.0.2:18080/v1",
        "https://127.0.0.1:18080/v1",
        "http://127.0.0.1:65536/v1",
        "http://127.0.0.1:18080/v1/",
        "http://127.0.0.1:18080/v1?x=1",
    ],
)
def test_even_operator_registration_requires_canonical_loopback_endpoint(
    authority: AuthorityHarness,
    wire: Any,
    endpoint: str,
) -> None:
    authority.document["registrations"][0]["endpoint"] = endpoint
    authority.snapshot["providers"][0]["endpoint"] = endpoint
    authority.write_allowlist()
    with pytest.raises(ValueError):
        authority.post()
    assert not wire.created


def test_unapproved_model_never_opens_socket(
    authority: AuthorityHarness,
    wire: Any,
) -> None:
    with pytest.raises(PermissionError):
        authority.post(model="foreign-model")
    assert not wire.created


@pytest.mark.parametrize("mutation", ["allowlist", "registry", "revoked"])
def test_binding_or_authority_change_after_connect_prevents_sending(
    authority: AuthorityHarness,
    wire: Any,
    mutation: str,
) -> None:
    def change() -> None:
        if mutation == "allowlist":
            authority.document["registrations"][0]["model_ids"].append("other-model")
            authority.write_allowlist()
        elif mutation == "registry":
            authority.snapshot["providers"][0]["record_revision"] += 1
        else:
            authority.store.revoked.add(("grant", authority.store.durable.grant_id))

    wire.connect_hook = change
    with pytest.raises(PermissionError):
        authority.post()
    assert wire.connected
    assert not wire.sent


def test_symlinked_owner_allowlist_is_not_trusted(
    authority: AuthorityHarness,
    wire: Any,
) -> None:
    target = authority.root / "untrusted.json"
    target.write_text(json.dumps(authority.document))
    authority.directory.unlink("allowlist.json")
    try:
        (authority.directory.root / "allowlist.json").symlink_to(target)
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation is unavailable")
    with pytest.raises((OSError, PermissionError)):
        authority.post()
    assert not wire.created


class LocalAdapterClient:
    """Exercise adapter selection through the real Host-local transport."""

    def __init__(self, harness: AuthorityHarness) -> None:
        self.harness = harness
        self.transport = harness.transport()
        self.local_calls: list[dict[str, Any]] = []
        self.remote_calls = 0

    def invoke(self, *_args: Any, **_kwargs: Any) -> dict[str, Any]:
        return self.harness.snapshot

    def post_local_model_chat(self, **kwargs: Any) -> Any:
        self.local_calls.append(kwargs)
        assert self.transport is not None
        return self.transport.post_chat(**kwargs)

    def post_json_with_credential(self, **_kwargs: Any) -> Any:
        self.remote_calls += 1
        raise AssertionError("local route must not invoke cloud credential transport")


def _adapter_request(**changes: Any) -> dict[str, Any]:
    return {
        "provider_connection_id": CONNECTION,
        "model_id": MODEL,
        "messages": [{"role": "user", "content": "Hello"}],
        "deadline": time.time() + 30,
        **changes,
    }


@pytest.mark.parametrize("streaming", [False, True])
def test_local_adapter_uses_credential_free_host_transport(
    authority: AuthorityHarness,
    wire: Any,
    streaming: bool,
) -> None:
    if streaming:
        authority.principal = _principal("stream")
        authority.binding = _captured_binding("stream")
        authority.envelope.operation_id = authority.principal.operation_id
        authority.envelope.contract_id = "tobkiri.service.ai.provider.stream.v1"
        authority.envelope.target_principal.value = authority.principal.principal_id
        authority.store.durable.target = authority.principal
    client = LocalAdapterClient(authority)
    operation = create_stream_operation(client) if streaming else create_generate_operation(client)
    result = operation("stream" if streaming else "generate", _adapter_request())
    if streaming:
        assert result["events"][0]["type"] == "text_delta"
    else:
        assert result["output"] == "Hello from the local model"
        assert result["tool_intents"] == []
    assert client.remote_calls == 0
    assert client.local_calls[0]["provider_instance_id"] == CONNECTION
    assert wire.sent[0][2]["model"] == MODEL
    assert wire.sent[0][2]["stream"] is False


@pytest.mark.parametrize(
    "override",
    [
        {"credential_handle": "credential:injected"},
        {"parameters": {"model": "foreign-model"}},
        {"parameters": {"messages": []}},
        {"parameters": {"stream": True}},
        {"tools": [{"type": "function", "function": {"name": "dangerous"}}]},
    ],
)
def test_local_adapter_denies_credential_routing_overrides_and_tools_before_send(
    authority: AuthorityHarness,
    wire: Any,
    override: dict[str, Any],
) -> None:
    client = LocalAdapterClient(authority)
    with pytest.raises(GlobalContractInvocationError) as error:
        create_generate_operation(client)("generate", _adapter_request(**override))
    assert error.value.code in {"denied", "incompatible"}
    assert not client.local_calls
    assert client.remote_calls == 0
    assert not wire.created


def test_local_adapter_does_not_treat_remote_http_as_credentialless_local(
    authority: AuthorityHarness,
    wire: Any,
) -> None:
    authority.snapshot["providers"][0].update(
        adapter_id="openai-compatible",
        credential_handle="credential:cloud",
    )
    client = LocalAdapterClient(authority)
    with pytest.raises(GlobalContractInvocationError) as error:
        create_generate_operation(client)("generate", _adapter_request())
    assert error.value.code == "denied"
    assert not client.local_calls
    assert client.remote_calls == 0
    assert not wire.created


def test_local_adapter_flattens_only_saved_text_blocks():
    from ecosystem.rumi_provider_adapters_pack.runtime.adapter import _local_text_messages

    assert _local_text_messages(
        [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "17+"},
                    {"type": "text", "text": "25"},
                ],
            }
        ]
    ) == [{"role": "user", "content": "17+25"}]
    for content in (
        [{"type": "image_url", "image_url": {"url": "http://127.0.0.1/private"}}],
        [{"type": "text", "text": "test", "url": "http://127.0.0.1/private"}],
        [],
    ):
        with pytest.raises(GlobalContractInvocationError):
            _local_text_messages([{"role": "user", "content": content}])


@pytest.mark.parametrize("snapshot_number", [3, 4])
@pytest.mark.parametrize(
    "mutation",
    ["lease", "cancelled", "expired", "allowlist_deleted", "allowlist_changed"],
)
def test_withdrawal_during_nested_registry_read_is_fenced(
    authority: AuthorityHarness,
    wire: Any,
    snapshot_number: int,
    mutation: str,
) -> None:
    """A blocking child lookup cannot preserve withdrawn parent authority."""
    reads = 0

    def snapshot() -> dict[str, Any]:
        nonlocal reads
        reads += 1
        if reads == snapshot_number:
            if mutation == "lease":
                authority.store.state = LeaseState.REVOKED
            elif mutation == "cancelled":
                authority.envelope.cancellation_requested.set()
            elif mutation == "expired":
                authority.envelope.deadline_monotonic = time.monotonic() - 1
            elif mutation == "allowlist_deleted":
                authority.directory.unlink("allowlist.json")
            else:
                authority.document["registrations"][0]["model_ids"] = ["other"]
                authority.write_allowlist()
        return authority.snapshot

    transport = create_local_model_transport(
        envelope=authority.envelope,
        principal=authority.principal,
        authority_store=authority.store,
        user_data_root=authority.root,
        registry_snapshot=snapshot,
        assert_current=authority.assert_current,
        binding=authority.binding,
    )
    expected_error = FileNotFoundError if mutation == "allowlist_deleted" else PermissionError
    with pytest.raises(expected_error):
        authority.post(transport)
    assert reads == snapshot_number
    # The third read occurs after connect and before POST; the fourth occurs
    # after response IO and must suppress its release to the caller.
    assert len(wire.sent) == (0 if snapshot_number == 3 else 1)


@pytest.mark.parametrize("allowlist_read_number", [3, 4])
def test_lease_revocation_during_owner_read_is_fenced_before_effect_or_release(
    authority: AuthorityHarness,
    wire: Any,
    monkeypatch: pytest.MonkeyPatch,
    allowlist_read_number: int,
) -> None:
    """The transport rechecks authority after its complete binding resolver."""
    original_read = SecureDirectory.read_bytes_bounded
    reads = 0

    def read(
        directory: SecureDirectory,
        name: str,
        *,
        max_bytes: int,
    ) -> bytes:
        nonlocal reads
        result = original_read(directory, name, max_bytes=max_bytes)
        if name == "allowlist.json":
            reads += 1
            if reads == allowlist_read_number:
                authority.store.state = LeaseState.REVOKED
        return result

    monkeypatch.setattr(SecureDirectory, "read_bytes_bounded", read)
    with pytest.raises(PermissionError):
        authority.post()
    assert reads == allowlist_read_number
    assert len(wire.sent) == (0 if allowlist_read_number == 3 else 1)
