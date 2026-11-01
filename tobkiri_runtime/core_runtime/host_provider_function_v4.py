"""Exact capture adapter for a stateless, single-operation Host Function."""

from __future__ import annotations

from dataclasses import dataclass
import threading
from typing import Any, Callable, Mapping

from core_runtime.host_provider_backend_v4 import (
    CapturedHostProviderV4,
    HostProviderCaptureContextV4,
    HostProviderContributionV4,
    HostProviderInvocationContextV4,
)
from tobkiri_protocol.canonical import canonical_digest

HostFunction = Callable[
    [Mapping[str, Any], HostProviderInvocationContextV4], Mapping[str, Any]
]


@dataclass(frozen=True)
class SingleOperationHostFactoryV4:
    """Bind an owner's handler without adding contracts, authority or state."""

    function_id: str
    contract_id: str
    operation_id: str
    bind: Callable[[HostProviderCaptureContextV4], HostFunction]

    def capture(self, context: HostProviderCaptureContextV4) -> CapturedHostProviderV4:
        """Capture one exact operation and fence its handler when closed."""
        if len(context.provider_bindings) != 1:
            raise PermissionError("Host Function binding is unavailable")
        binding = context.provider_bindings[0]
        key = (self.contract_id, self.operation_id, binding.principal_ref.value)
        if (
            binding.function.function_id != self.function_id
            or binding.operation.contract_id != self.contract_id
            or binding.operation.operation_id != self.operation_id
            or binding.operation.contract_version != "1.0.0"
            or key not in context.domain_ids
        ):
            raise PermissionError("Host Function binding is invalid")
        identity = (
            context.profile_id,
            context.plan_digest,
            context.security_epoch,
            context.activation["activation_id"],
            canonical_digest(dict(context.activation)),
        )
        handler = self.bind(context)
        closed = threading.Event()

        def invoke(
            operation_id: str,
            payload: Mapping[str, Any],
            invocation: HostProviderInvocationContextV4,
        ) -> Mapping[str, Any]:
            invocation.assert_current()
            envelope = invocation.envelope
            actual = envelope.context
            if (
                closed.is_set()
                or operation_id != self.operation_id
                or envelope.contract_id != self.contract_id
                or envelope.contract_version != "1.0.0"
                or envelope.operation_id != self.operation_id
                or envelope.target_principal != binding.principal_ref
                or envelope.target_domain.value != context.domain_ids[key]
                or dict(envelope.payload) != dict(payload)
                or (
                    actual.profile_id, actual.plan_digest, actual.security_epoch,
                    actual.activation_id, actual.activation_digest,
                ) != identity
            ):
                raise PermissionError("Host Function invocation binding changed")
            result = handler(payload, invocation)
            invocation.assert_current()
            if closed.is_set():
                raise PermissionError("Host Function capture is closed")
            return result

        return CapturedHostProviderV4(
            (HostProviderContributionV4(
                contract_id=self.contract_id,
                contract_version="1.0.0",
                operation_id=self.operation_id,
                principal_id=key[2],
                artifact_digest=binding.artifact.digest,
                implementation_digest=binding.function.implementation_digest,
                domain_id=context.domain_ids[key],
                invoke=invoke,
            ),),
            closed.set,
        )
