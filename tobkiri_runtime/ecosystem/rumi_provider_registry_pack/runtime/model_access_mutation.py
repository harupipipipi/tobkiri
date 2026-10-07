"""Captured Host entrypoints for approval-gated model access settings."""

from __future__ import annotations

from typing import Any, Mapping

from core_runtime.host_provider_backend_v4 import (
    CapturedHostProviderV4,
    HostProviderCaptureContextV4,
    HostProviderContributionV4,
    HostProviderInvocationContextV4,
)
from tobkiri_host.broker import RequestEnvelope
from ecosystem.rumi_provider_registry_pack.runtime.registry import ProviderRegistry
from ecosystem.rumi_provider_registry_pack.runtime.provider_filters import CAPABILITY_REVISION
from ecosystem.rumi_provider_registry_pack.runtime.model_access_configuration import (
    PREPARE_OPERATION,
    EXECUTE_OPERATION,
    prepare_model_access,
    execute_model_access,
)

CONTRACT_ID = "tobkiri.action.ai.provider.registry.manage.v1"


class ModelAccessMutationHostFactoryV4:
    """Bind one exact model policy operation to captured Profile authority."""

    def __init__(self, *, execute: bool = False) -> None:
        self.operation_id = EXECUTE_OPERATION if execute else PREPARE_OPERATION
        self.function_id = "rumi_provider_registry_pack.model-access." + (
            "execute" if execute else "prepare"
        )
        self.execute = execute

    def capture(self, context: HostProviderCaptureContextV4) -> CapturedHostProviderV4:
        """Reject missing or foreign targets before constructing any owner port."""
        if (
            not context.profile_id
            or context.user_data_root is None
            or len(context.provider_bindings) != 1
        ):
            raise PermissionError("model access capture is unavailable")
        binding = context.provider_bindings[0]
        if (
            binding.function.function_id != self.function_id
            or binding.operation.contract_id != CONTRACT_ID
            or binding.operation.operation_id != self.operation_id
        ):
            raise PermissionError("model access binding is invalid")
        key = (CONTRACT_ID, self.operation_id, binding.principal_ref.value)
        domain_id = context.domain_ids.get(key)
        if domain_id is None:
            raise PermissionError("model access domain is unavailable")
        registry = ProviderRegistry(context.profile_id, user_data_root=context.user_data_root)

        def invoke(
            operation_id: str,
            payload: Mapping[str, Any],
            invocation: HostProviderInvocationContextV4,
        ) -> Mapping[str, Any]:
            envelope = invocation.envelope
            if (
                operation_id != self.operation_id
                or not isinstance(envelope, RequestEnvelope)
                or envelope.contract_id != CONTRACT_ID
                or envelope.operation_id != operation_id
                or envelope.target_principal.value != binding.principal_ref.value
                or envelope.target_domain.value != domain_id
                or envelope.context.profile_id != context.profile_id
                or envelope.context.plan_digest != context.plan_digest
                or envelope.context.security_epoch != context.security_epoch
                or not isinstance(payload, Mapping)
            ):
                raise PermissionError("model access invocation changed")
            invocation.assert_current()
            request = {key: value for key, value in payload.items() if key != "_session_id"}
            # Reviewed built-in official schema evidence; callers cannot provide it.
            if self.execute:
                return execute_model_access(
                    registry, request, native_capability_revision=CAPABILITY_REVISION
                )
            return prepare_model_access(
                registry, request, native_capability_revision=CAPABILITY_REVISION
            )

        return CapturedHostProviderV4(
            (
                HostProviderContributionV4(
                    contract_id=CONTRACT_ID,
                    contract_version=binding.operation.contract_version,
                    operation_id=self.operation_id,
                    principal_id=binding.principal_ref.value,
                    artifact_digest=binding.artifact.digest,
                    implementation_digest=binding.function.implementation_digest,
                    domain_id=domain_id,
                    invoke=invoke,
                ),
            ),
            lambda: None,
        )


HOST_PROVIDER_FACTORY = {
    "rumi_provider_registry_pack.model-access.prepare": ModelAccessMutationHostFactoryV4(),
    "rumi_provider_registry_pack.model-access.execute": ModelAccessMutationHostFactoryV4(
        execute=True
    ),
}
