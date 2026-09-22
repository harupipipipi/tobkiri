"""Single-request model registry process entrypoint."""

from __future__ import annotations

import json
import sys
from typing import Any, Callable, Mapping

from core_runtime.host_provider_backend_v4 import (
    CapturedHostProviderV4,
    HostProviderCaptureContextV4,
    HostProviderContributionV4,
    HostProviderInvocationContextV4,
)

from ecosystem.rumi_model_registry_pack.runtime.service import ModelRegistryService


_PACK_ID = "rumi_model_registry_pack"
_FUNCTION_IDS = frozenset(
    {
        "rumi_model_registry_pack.model-registry.manage",
        "rumi_model_registry_pack.model-registry.migrate",
        "rumi_model_registry_pack.model-registry.profile",
    }
)
_PROFILE_OPERATION = "rumi_model_registry_pack.model-profile-resource"
_PROFILE_GENERATE_OPERATION = f"{_PROFILE_OPERATION}.generate"
_PROFILE_STREAM_OPERATION = f"{_PROFILE_OPERATION}.stream"
_MANAGE_OPERATION = "rumi_model_registry_pack.model-profile-manage"
_MIGRATE_OPERATION = "rumi_model_registry_pack.model-registry-migrate"
_MANAGE_SERVICE_OPERATIONS = frozenset({"save", "delete", "alias.set"})
_MIGRATE_SERVICE_OPERATIONS = frozenset(
    {"migration.apply", "migration.rollback"}
)
_PROFILE_SERVICE_OPERATIONS = frozenset({"list", "get", "resolve"})


class ModelRegistryHostFactoryV4:
    """Capture one verified model-registry Host extension function.

    The registry owns no cross-pack dependencies. It still creates the
    invocation-bound contract client with an empty allow-list so its local
    service cannot accidentally acquire an ambient Host capability.
    """

    def __init__(self, function_id: str) -> None:
        if function_id not in _FUNCTION_IDS:
            raise ValueError("model registry function is not registered")
        self.function_id = function_id

    def capture(
        self,
        context: HostProviderCaptureContextV4,
    ) -> CapturedHostProviderV4:
        """Bind the exact resolved registry operations and data root."""

        if (
            context.user_data_root is None
            or not context.provider_bindings
            or any(
                binding.function.function_id != self.function_id
                for binding in context.provider_bindings
            )
        ):
            raise PermissionError("model registry bindings are incomplete")
        service = ModelRegistryService(user_data_root=context.user_data_root)
        allowed_operation_ids = frozenset(
            binding.operation.operation_id for binding in context.provider_bindings
        )

        def invoke(
            operation_id: str,
            payload: Mapping[str, Any],
            invocation: HostProviderInvocationContextV4,
        ) -> Mapping[str, Any]:
            if operation_id not in allowed_operation_ids:
                raise PermissionError("model registry operation is unavailable")
            client = invocation.contract_client(
                allowed_contract_ids=frozenset(),
                consumer_pack_id=_PACK_ID,
            )
            del client
            return service.invoke(
                _service_operation(operation_id, payload),
                payload,
            )

        return CapturedHostProviderV4(
            tuple(_contributions(context, invoke)),
            lambda: None,
        )


def _service_operation(operation_id: str, payload: Mapping[str, Any]) -> str:
    """Translate a public registry contract to a narrow owner operation."""

    if operation_id in {_PROFILE_GENERATE_OPERATION, _PROFILE_STREAM_OPERATION}:
        return operation_id
    if operation_id == _PROFILE_OPERATION:
        if str(payload.get("identifier") or "").strip():
            return "resolve"
        return _payload_operation(payload, _PROFILE_SERVICE_OPERATIONS)
    if operation_id == _MANAGE_OPERATION:
        return _payload_operation(payload, _MANAGE_SERVICE_OPERATIONS)
    if operation_id == _MIGRATE_OPERATION:
        return _payload_operation(payload, _MIGRATE_SERVICE_OPERATIONS)
    raise PermissionError("model registry operation is not recognized")


def _payload_operation(
    payload: Mapping[str, Any],
    allowed_operations: frozenset[str],
) -> str:
    """Return one exact declared owner action, never an arbitrary method."""

    operation = str(payload.get("operation") or payload.get("action") or "")
    if operation not in allowed_operations:
        raise ValueError("model registry operation payload is invalid")
    return operation


def _contributions(
    context: HostProviderCaptureContextV4,
    invoke: Callable[
        [str, Mapping[str, Any], HostProviderInvocationContextV4], Mapping[str, Any]
    ],
) -> list[HostProviderContributionV4]:
    """Project only verified registry bindings into exact contributions."""

    contributions: list[HostProviderContributionV4] = []
    for binding in context.provider_bindings:
        key = (
            binding.operation.contract_id,
            binding.operation.operation_id,
            binding.principal_ref.value,
        )
        domain_id = context.domain_ids.get(key)
        if domain_id is None:
            raise PermissionError("model registry domain binding is unavailable")
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
    return contributions


HOST_PROVIDER_FACTORY = {
    function_id: ModelRegistryHostFactoryV4(function_id)
    for function_id in _FUNCTION_IDS
}


def main() -> int:
    """Return provider-neutral, path-free operation envelopes."""
    try:
        request = json.loads(sys.stdin.read())
        if not isinstance(request, dict) or not isinstance(
            request.get("payload"),
            dict,
        ):
            raise ValueError("request is invalid")
        value = ModelRegistryService().invoke(
            str(request.get("operation") or ""),
            request["payload"],
        )
        response = {"status": "ok", "value": value}
        code = 0
    except PermissionError:
        response = {"status": "denied", "error_code": "denied", "diagnostics": ["model registry request denied"]}
        code = 3
    except KeyError:
        response = {"status": "unavailable", "error_code": "unknown", "diagnostics": ["model registry item is unknown"]}
        code = 2
    except Exception as exc:
        response = {"status": "unavailable", "error_code": type(exc).__name__, "diagnostics": [type(exc).__name__]}
        code = 2
    sys.stdout.write(json.dumps(response, ensure_ascii=False))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
