"""Single-request conservative provider health entrypoint."""

from __future__ import annotations

import json
import sys
from typing import Any, Mapping

from core_runtime.host_provider_backend_v4 import (
    CapturedHostProviderV4,
    HostProviderCaptureContextV4,
    HostProviderContributionV4,
    HostProviderInvocationContextV4,
)
from ecosystem.rumi_provider_registry_pack.runtime.service import (
    ProviderRegistryService,
)


_PACK_ID = "rumi_provider_registry_pack"
_FUNCTION_ID = "rumi_provider_registry_pack.provider-registry.health"


class ProviderHealthHostFactoryV4:
    """Bind conservative provider health reads to verified Host dispatch."""

    function_id = _FUNCTION_ID

    def capture(
        self,
        context: HostProviderCaptureContextV4,
    ) -> CapturedHostProviderV4:
        """Capture exact health operations and the active Profile data root."""

        if (
            context.user_data_root is None
            or not context.provider_bindings
            or any(
                binding.function.function_id != self.function_id
                for binding in context.provider_bindings
            )
        ):
            raise PermissionError("provider health bindings are incomplete")
        service = ProviderRegistryService(user_data_root=context.user_data_root)
        allowed_operations = frozenset(
            binding.operation.operation_id for binding in context.provider_bindings
        )

        def invoke(
            operation_id: str,
            payload: Mapping[str, Any],
            invocation: HostProviderInvocationContextV4,
        ) -> Mapping[str, Any]:
            if operation_id not in allowed_operations:
                raise PermissionError("provider health operation is unavailable")
            client = invocation.contract_client(
                allowed_contract_ids=frozenset(),
                consumer_pack_id=_PACK_ID,
            )
            del client
            return service.invoke("health", payload)

        contributions: list[HostProviderContributionV4] = []
        for binding in context.provider_bindings:
            key = (
                binding.operation.contract_id,
                binding.operation.operation_id,
                binding.principal_ref.value,
            )
            domain_id = context.domain_ids.get(key)
            if domain_id is None:
                raise PermissionError("provider health domain binding is unavailable")
            contributions.append(
                HostProviderContributionV4(
                    contract_id=binding.operation.contract_id,
                    contract_version=binding.operation.contract_version,
                    operation_id=binding.operation.operation_id,
                    principal_id=binding.principal_ref.value,
                    artifact_digest=binding.artifact.digest,
                    implementation_digest=binding.function.implementation_digest,
                    domain_id=domain_id,
                    invoke=invoke,
                )
            )
        return CapturedHostProviderV4(tuple(contributions), lambda: None)


HOST_PROVIDER_FACTORY = ProviderHealthHostFactoryV4()


def main() -> int:
    """Map generic health resource reads to verified registry evidence."""
    try:
        request = json.loads(sys.stdin.read())
        if not isinstance(request, dict) or not isinstance(
            request.get("payload"), dict
        ):
            raise ValueError("request is invalid")
        operation = str(request.get("operation") or "")
        if operation not in {
            "get",
            "list",
            "health",
            "rumi_provider_registry_pack.provider-registry-health.generate",
            "rumi_provider_registry_pack.provider-registry-health.stream",
        }:
            raise ValueError("provider health operation is invalid")
        value = ProviderRegistryService().invoke("health", request["payload"])
        response = {"status": "ok", "value": value}
        code = 0
    except Exception as exc:
        response = {
            "status": "unavailable",
            "error_code": type(exc).__name__,
            "diagnostics": [type(exc).__name__],
        }
        code = 2
    sys.stdout.write(json.dumps(response, ensure_ascii=False))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
