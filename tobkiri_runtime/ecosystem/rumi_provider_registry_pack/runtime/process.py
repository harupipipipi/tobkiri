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
    """Capture Profile-bound registry operations for authenticated Host dispatch."""

    def __init__(self, *, readonly: bool = True) -> None:
        self.readonly = readonly
        self.function_id = (
            "rumi_provider_registry_pack.provider-registry."
            + ("resource" if readonly else "manage")
        )
        self.contract_id = (
            "tobkiri.resource.ai.provider.registry.v1" if readonly
            else "tobkiri.action.ai.provider.registry.manage.v1"
        )
        base_operation = (
            "rumi_provider_registry_pack.provider-registry-"
            + ("resource" if readonly else "manage")
        )
        self.operations = (
            frozenset({base_operation, base_operation + ".generate", base_operation + ".stream"})
            if readonly else frozenset({base_operation})
        )

    def capture(
        self,
        context: HostProviderCaptureContextV4,
    ) -> CapturedHostProviderV4:
        """Bind the redacted registry service to exact resolved operations."""
        if (
            context.user_data_root is None
            or not context.profile_id
            or not context.provider_bindings
            or any(
            binding.function.function_id != self.function_id
            or binding.operation.contract_id != self.contract_id
            or binding.operation.operation_id not in self.operations
            for binding in context.provider_bindings
            )
        ):
            raise PermissionError("provider registry bindings are incomplete")
        service = ProviderRegistryService(user_data_root=context.user_data_root)

        def invoke(
            operation_id: str,
            payload: Mapping[str, Any],
            invocation: HostProviderInvocationContextV4,
        ) -> Mapping[str, Any]:
            del invocation
            if (
                operation_id not in self.operations
                or payload.get("profile_id", context.profile_id) != context.profile_id
            ):
                raise PermissionError("provider registry request binding is invalid")
            if self.readonly:
                if set(payload) - {"profile_id"}:
                    raise PermissionError("provider registry read payload is invalid")
                return service.invoke("list", {"profile_id": context.profile_id})
            action = payload.get("operation")
            if action == "save":
                fields = {"operation", "profile_id", "record", "expected_revision"}
                record = payload.get("record")
                if not isinstance(record, Mapping):
                    raise ValueError("provider registry record is required")
                if set(record) - {
                    "provider_instance_id", "adapter_id", "display_name",
                    "credential_handle", "endpoint", "enabled", "data_residency",
                    "metadata",
                }:
                    raise PermissionError("provider registry record fields are invalid")
            elif action == "delete":
                fields = {
                    "operation", "profile_id", "provider_instance_id", "expected_revision",
                }
            else:
                raise PermissionError("provider registry mutation is not permitted")
            revision = payload.get("expected_revision")
            if (
                set(payload) - fields
                or type(revision) is not int
                or revision < 0
            ):
                raise PermissionError("provider registry mutation payload is invalid")
            return service.invoke(action, {**payload, "profile_id": context.profile_id})

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


HOST_PROVIDER_FACTORY = {
    factory.function_id: factory
    for factory in (
        ProviderRegistryHostFactoryV4(),
        ProviderRegistryHostFactoryV4(readonly=False),
    )
}


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
