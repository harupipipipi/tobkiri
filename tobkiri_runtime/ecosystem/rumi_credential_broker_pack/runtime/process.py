"""Single-request credential broker process entrypoint."""

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

from ecosystem.rumi_credential_broker_pack.runtime.service import CredentialBrokerService


class CredentialManagementHostFactoryV4:
    """Bind credential management to a verified Host root and Profile.

    Admission and approval remain Broker responsibilities. This hook does not
    resolve material or expose migration operations through management routes.
    """

    def __init__(self, *, readonly: bool) -> None:
        self.function_id = (
            "rumi_credential_broker_pack.credential-broker."
            + ("status" if readonly else "manage")
        )
        self.contract_id = (
            "tobkiri.resource.credential.status.v1" if readonly
            else "tobkiri.action.credential.manage.v1"
        )
        self.operation_id = (
            "rumi_credential_broker_pack.credential-"
            + ("status" if readonly else "manage")
        )
        self.readonly = readonly

    def capture(
        self, context: HostProviderCaptureContextV4,
    ) -> CapturedHostProviderV4:
        """Capture only the exact resolved credential operation."""
        if (
            context.user_data_root is None
            or not context.profile_id
            or len(context.provider_bindings) != 1
        ):
            raise PermissionError("credential capture is incomplete")
        binding = context.provider_bindings[0]
        operation = binding.operation
        if (
            binding.function.function_id != self.function_id
            or operation.contract_id != self.contract_id
            or operation.operation_id != self.operation_id
        ):
            raise PermissionError("credential capture binding is invalid")
        domain_id = context.domain_ids.get((
            self.contract_id, self.operation_id, binding.principal_ref.value,
        ))
        if domain_id is None:
            raise PermissionError("credential capture domain is unavailable")
        service = CredentialBrokerService(user_data_root=context.user_data_root)

        def invoke(
            operation_id: str,
            payload: Mapping[str, Any],
            invocation: HostProviderInvocationContextV4,
        ) -> Mapping[str, Any]:
            del invocation
            action = payload.get("operation")
            fields = {
                "list": set(),
                "revoke": {"handle"},
                "create": {
                    "secret_material", "consumer_pack_id", "provider_instance_id",
                    "scopes", "resource_binding", "purpose", "label", "expires_at",
                },
            }
            allowed = {"list"} if self.readonly else {"create", "revoke"}
            if (
                operation_id != self.operation_id
                or payload.get("profile_id") != context.profile_id
                or not isinstance(action, str)
                or action not in allowed
                or set(payload) - fields[action] - {"operation", "profile_id"}
            ):
                raise PermissionError("credential request is invalid")
            return service.invoke(action, payload)

        return CapturedHostProviderV4((HostProviderContributionV4(
            contract_id=self.contract_id,
            contract_version=operation.contract_version,
            operation_id=self.operation_id,
            principal_id=binding.principal_ref.value,
            artifact_digest=binding.artifact.digest,
            implementation_digest=binding.function.implementation_digest,
            domain_id=domain_id,
            invoke=invoke,
        ),), lambda: None)


HOST_PROVIDER_FACTORY = {
    factory.function_id: factory
    for factory in (
        CredentialManagementHostFactoryV4(readonly=False),
        CredentialManagementHostFactoryV4(readonly=True),
    )
}


def main() -> int:
    """Invoke without emitting credential material in failure diagnostics."""
    try:
        request = json.loads(sys.stdin.read())
        if not isinstance(request, dict) or not isinstance(
            request.get("payload"),
            dict,
        ):
            raise ValueError("request is invalid")
        operation = str(request.get("operation") or "")
        if operation == "resolve":
            raise PermissionError(
                "credential resolution requires the bound Host broker channel"
            )
        value = CredentialBrokerService().invoke(operation, request["payload"])
        response = {"status": "ok", "value": value}
        code = 0
    except PermissionError:
        response = {
            "status": "denied",
            "error_code": "denied",
            "diagnostics": ["credential request denied"],
        }
        code = 3
    except KeyError:
        response = {
            "status": "unavailable",
            "error_code": "not_configured",
            "diagnostics": ["credential is not configured"],
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
