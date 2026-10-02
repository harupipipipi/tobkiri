"""Bind local inference to Host operator allowlists and the live Broker lease."""

from __future__ import annotations

import hashlib
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from core_runtime.authority.v4 import AuthorityStore, FunctionPrincipal, LeaseState
from core_runtime.local_model_transport import LocalModelBinding, LocalModelTransport
from tobkiri_host.broker import RequestEnvelope
from tobkiri_host.contracts import ResolvedOperationBinding
from tobkiri_protocol.canonical import strict_loads
from tobkiri_protocol.secure_persistence import SecureDirectory

LOCAL_ADAPTER = "local-openai-compatible"
ALLOWLIST_VERSION = "tobkiri.host.local-models.v1"
LOCAL_REGISTRY_CONTRACT = "tobkiri.resource.ai.provider.registry.v1"
_LOCAL_CONTRACTS = frozenset({
    "tobkiri.service.ai.provider.generate.v1",
    "tobkiri.service.ai.provider.stream.v1",
})


@dataclass(frozen=True)
class LocalModelRequest:
    """Static local-inference declaration from a verified Host factory."""

    contract_id: str
    registry_operation_id: str


@dataclass(frozen=True)
class LocalModelInvocationBinding:
    """Exact captured Host function and its caller-bound registry dependency."""

    principal: FunctionPrincipal
    contract_id: str
    contract_version: str
    registry_principal: FunctionPrincipal
    registry_operation_id: str


def capture_local_model_binding(
    binding: ResolvedOperationBinding,
    registry_binding: ResolvedOperationBinding,
) -> LocalModelInvocationBinding:
    """Validate canonical semantics after the Host resolves the exact Plan edge."""
    if (
        binding.operation.contract_id not in _LOCAL_CONTRACTS
        or binding.operation.contract_version != "1.0.0"
        or registry_binding.operation.contract_id != LOCAL_REGISTRY_CONTRACT
        or registry_binding.operation.contract_version != "1.0.0"
    ):
        raise PermissionError("local model contract binding is invalid")
    principal = FunctionPrincipal(
        parent_artifact_digest=binding.artifact.digest,
        function_implementation_digest=binding.function.implementation_digest,
        function_id=binding.function.function_id,
        contract_revision_digest=binding.operation.revision_digest,
        operation_id=binding.operation.operation_id,
    )
    if principal.principal_id != binding.principal_ref.value:
        raise PermissionError("local model principal binding is invalid")
    registry_principal = FunctionPrincipal(
        parent_artifact_digest=registry_binding.artifact.digest,
        function_implementation_digest=registry_binding.function.implementation_digest,
        function_id=registry_binding.function.function_id,
        contract_revision_digest=registry_binding.operation.revision_digest,
        operation_id=registry_binding.operation.operation_id,
    )
    if registry_principal.principal_id != registry_binding.principal_ref.value:
        raise PermissionError("local model registry principal binding is invalid")
    return LocalModelInvocationBinding(
        principal, binding.operation.contract_id,
        binding.operation.contract_version, registry_principal,
        registry_binding.operation.operation_id,
    )


def create_local_model_transport(
    *,
    envelope: RequestEnvelope,
    principal: FunctionPrincipal,
    authority_store: AuthorityStore,
    user_data_root: Path,
    registry_snapshot: Callable[[], Mapping[str, Any]],
    assert_current: Callable[[], None],
    binding: LocalModelInvocationBinding | None = None,
) -> LocalModelTransport | None:
    """Give only an exact captured Host function its credential-free capability."""
    if (
        binding is None
        or principal != binding.principal
        or binding.contract_id not in _LOCAL_CONTRACTS
        or envelope.contract_id != binding.contract_id
        or envelope.contract_version != binding.contract_version
        or principal.operation_id != envelope.operation_id
        or envelope.target_principal.value != principal.principal_id
    ):
        return None

    def guard() -> None:
        assert_current()
        context = envelope.context
        try:
            durable, state = authority_store.inspect_lease_token(
                envelope.lease.token.decode("ascii")
            )
            if (
                state is not LeaseState.DISPATCHED
                or durable.target != principal
                or durable.caller.principal_id != context.caller_principal.value
                or durable.profile_id != context.profile_id
                or durable.activation_id != context.activation_id
                or durable.security_epoch != context.security_epoch
                or authority_store.security_epoch != context.security_epoch
                or durable.target_domain_id != envelope.target_domain.value
                or durable.target_domain_id != context.target_domain_id
                or durable.target_boot_epoch != context.target_boot_epoch
                or durable.request_id != context.request_id
                or durable.request_digest != envelope.request_digest
                or envelope.cancellation_requested.is_set()
                or envelope.deadline_monotonic <= time.monotonic()
            ):
                raise PermissionError("local model invocation is no longer authorized")
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
            if any(authority_store.is_revoked(kind, identity) for kind, identity in targets):
                raise PermissionError("local model invocation was revoked")
        except Exception:  # noqa: BLE001 - fail closed at the Host authority boundary
            raise PermissionError("local model authority is unavailable") from None

    def resolve(provider_instance_id: str) -> LocalModelBinding:
        guard()
        snapshot = registry_snapshot()
        if snapshot.get("profile_id") != envelope.context.profile_id:
            raise PermissionError("local model registry Profile changed")
        records = snapshot.get("providers")
        if not isinstance(records, list):
            raise PermissionError("local model registry is invalid")
        configured = [
            item
            for item in records
            if isinstance(item, Mapping)
            and item.get("provider_instance_id") == provider_instance_id
        ]
        if len(configured) != 1:
            raise PermissionError("local model connection is unavailable")
        guard()
        # Read operator approval after the potentially blocking registry call.
        # A registration withdrawn during that call must prevent the effect.
        # This file belongs to the Host operator, not the Pack connection store.
        # Installing a provider record alone never grants loopback access.
        directory = SecureDirectory(user_data_root / "host_local_models", create=False)
        raw = directory.read_bytes_bounded("allowlist.json", max_bytes=65536)
        document = strict_loads(raw)
        if (
            not isinstance(document, dict)
            or set(document) != {"version", "registrations"}
            or document["version"] != ALLOWLIST_VERSION
            or not isinstance(document["registrations"], list)
        ):
            raise PermissionError("local model allowlist is invalid")
        matches = [
            item
            for item in document["registrations"]
            if isinstance(item, dict)
            and item.get("profile_id") == envelope.context.profile_id
            and item.get("provider_instance_id") == provider_instance_id
        ]
        if len(matches) != 1 or set(matches[0]) != {
            "profile_id",
            "provider_instance_id",
            "endpoint",
            "model_ids",
        }:
            raise PermissionError("local model is not approved by the Host operator")
        approved = matches[0]
        if not isinstance(approved["model_ids"], list):
            raise PermissionError("local model allowlist is invalid")
        record = configured[0]
        if (
            record.get("enabled") is not True
            or record.get("adapter_id") != LOCAL_ADAPTER
            or record.get("credential_handle") is not None
            or record.get("endpoint") != approved["endpoint"]
        ):
            raise PermissionError("local model connection is not approved")
        return LocalModelBinding(
            profile_id=envelope.context.profile_id,
            provider_instance_id=provider_instance_id,
            endpoint=approved["endpoint"],
            model_ids=tuple(approved["model_ids"]),
            record_revision=record.get("record_revision", 0),
            allowlist_digest=hashlib.sha256(raw).hexdigest(),
        )

    return LocalModelTransport(
        profile_id=envelope.context.profile_id,
        resolve_binding=resolve,
        assert_current=guard,
        deadline_monotonic=envelope.deadline_monotonic,
        cancellation=envelope.cancellation_requested,
    )
