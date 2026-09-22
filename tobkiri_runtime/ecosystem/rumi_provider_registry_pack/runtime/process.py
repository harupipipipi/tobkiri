"""Single-request provider registry process entrypoint."""

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


class ProviderRegistryHostFactoryV4:
    """Capture read-only registry operations for authenticated Host dispatch."""

    function_id = "rumi_provider_registry_pack.provider-registry.resource"

    def capture(
        self,
        context: HostProviderCaptureContextV4,
    ) -> CapturedHostProviderV4:
        """Bind the redacted registry service to exact resolved operations."""
        if context.user_data_root is None or any(
            binding.function.function_id != self.function_id
            for binding in context.provider_bindings
        ):
            raise PermissionError("provider registry bindings are incomplete")
        service = ProviderRegistryService(user_data_root=context.user_data_root)

        def invoke(
            operation_id: str,
            payload: Mapping[str, Any],
            invocation: HostProviderInvocationContextV4,
        ) -> Mapping[str, Any]:
            del invocation
            return service.invoke(operation_id, payload)

        contributions = []
        for binding in context.provider_bindings:
            key = (
                binding.operation.contract_id,
                binding.operation.operation_id,
                binding.principal_ref.value,
            )
            domain_id = context.domain_ids.get(key)
            if domain_id is None:
                raise PermissionError("provider registry domain binding is unavailable")
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


HOST_PROVIDER_FACTORY = ProviderRegistryHostFactoryV4()


def main() -> int:
    """Return a redacted, path-free result envelope."""
    try:
        request = json.loads(sys.stdin.read())
        if not isinstance(request, dict) or not isinstance(
            request.get("payload"), dict
        ):
            raise ValueError("request is invalid")
        value = ProviderRegistryService().invoke(
            str(request.get("operation") or ""), request["payload"]
        )
        response = {"status": "ok", "value": value}
        code = 0
    except PermissionError:
        response = {
            "status": "denied",
            "error_code": "denied",
            "diagnostics": ["provider registry request denied"],
        }
        code = 3
    except KeyError:
        response = {
            "status": "unavailable",
            "error_code": "unknown",
            "diagnostics": ["provider registry item is unknown"],
        }
        code = 2
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
