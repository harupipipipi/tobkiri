"""Protocol v4 Host Provider integration hook for Workflow."""

from __future__ import annotations

from typing import Any, Mapping, Sequence

from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError

from core_runtime.host_provider_backend_v4 import (
    CapturedHostProviderV4,
    HostProviderCaptureContextV4,
    HostProviderContributionV4,
    HostProviderInvocationContextV4,
)
from tobkiri_protocol.canonical import canonical_digest

from .engine import WorkflowEngineV4
from .models import (
    AuthorityReservation,
    DispatchAuthority,
    InvocationOutcome,
    WorkflowDenied,
)
from .provider import WORKFLOW_FUNCTION_PRINCIPAL, WorkflowProviderV4
from .store import WorkflowStoreV4


class _ResolvedCatalog:
    """Project the immutable operation catalog captured by the Host."""

    def __init__(self, context: HostProviderCaptureContextV4) -> None:
        operations: dict[str, dict[str, Any]] = {}
        self.schemas: dict[str, Mapping[str, Any]] = {}
        for binding in context.catalog_bindings:
            input_digest = canonical_digest(binding.operation.input_schema)
            self.schemas[input_digest] = binding.operation.input_schema
            operation = {
                "contract_id": binding.operation.contract_id,
                "contract_revision_digest": binding.operation.revision_digest,
                "operation_id": binding.operation.operation_id,
                "function_principal_id": binding.principal_ref.value,
                "provider_id": binding.function.function_id,
                "input_schema_digest": input_digest,
                "effect_ceiling": [binding.operation.effect_class.value],
            }
            # Caller edges can share an identical target operation projection.
            operations[canonical_digest(operation)] = operation
        body = {
            "security_epoch": context.security_epoch,
            "activation": {
                "activation_id": str(context.activation["activation_id"]),
                "activation_digest": canonical_digest(context.activation),
            },
            "operations": sorted(
                operations.values(),
                key=lambda item: (
                    item["contract_id"],
                    item["operation_id"],
                    item["function_principal_id"],
                ),
            ),
        }
        self._snapshot = {**body, "catalog_digest": canonical_digest(body)}

    def snapshot(self) -> Mapping[str, Any]:
        """Return the exact activation-scoped Contract catalog."""
        return self._snapshot


class _SchemaValidator:
    """Validate only schemas captured from the resolved operation catalog."""

    def __init__(self, schemas: Mapping[str, Mapping[str, Any]]) -> None:
        self._schemas = dict(schemas)

    def validate(
        self,
        schema_digest: str,
        value: Mapping[str, Any],
    ) -> Sequence[str]:
        """Return validation errors without network schema discovery."""
        schema = self._schemas.get(schema_digest)
        if schema is None:
            return ("input schema is outside the captured catalog",)
        try:
            Draft202012Validator(schema).validate(value)
        except ValidationError as error:
            return (error.message,)
        return ()


class _UnavailableAttemptAuthority:
    """Fail closed until an attempt-scoped Authority adapter is supplied."""

    def reserve(self, request: Mapping[str, Any]) -> AuthorityReservation:
        del request
        raise WorkflowDenied("Workflow attempt Authority integration is unavailable")

    def inspect(self, reservation_id: str) -> AuthorityReservation:
        del reservation_id
        raise WorkflowDenied("Workflow attempt Authority integration is unavailable")

    def commit(
        self,
        reservation_id: str,
        *,
        request_digest: str,
        security_epoch: int,
    ) -> DispatchAuthority:
        del reservation_id, request_digest, security_epoch
        raise WorkflowDenied("Workflow attempt Authority integration is unavailable")

    def finish(self, reservation_id: str, *, outcome_digest: str, state: str) -> None:
        del reservation_id, outcome_digest, state
        raise WorkflowDenied("Workflow attempt Authority integration is unavailable")

    def revoke(self, reservation_id: str, *, reason: str) -> None:
        del reservation_id, reason
        raise WorkflowDenied("Workflow attempt Authority integration is unavailable")


class _UnavailableContractInvoker:
    """Never substitute a direct Provider callback for Broker dispatch."""

    def invoke(
        self,
        request: Mapping[str, Any],
        *,
        authority: DispatchAuthority,
    ) -> InvocationOutcome:
        del request, authority
        raise WorkflowDenied("Workflow Contract invocation integration is unavailable")

    def cancel(self, request_id: str) -> None:
        del request_id


class WorkflowHostProviderFactoryV4:
    """Capture Workflow operations from exact resolved Function bindings."""

    function_id = WORKFLOW_FUNCTION_PRINCIPAL

    def capture(
        self,
        context: HostProviderCaptureContextV4,
    ) -> CapturedHostProviderV4:
        """Build a local-first provider without registry or HTTP fallback."""
        if not context.provider_bindings or any(
            binding.function.function_id != self.function_id
            for binding in context.provider_bindings
        ):
            raise WorkflowDenied("Workflow Provider bindings are incomplete")
        catalog = _ResolvedCatalog(context)
        store = WorkflowStoreV4(
            context.state_root / context.profile_id / "workflow-v4.sqlite3"
        )
        provider = WorkflowProviderV4(
            WorkflowEngineV4(
                store=store,
                catalog=catalog,
                authority=_UnavailableAttemptAuthority(),
                invoker=_UnavailableContractInvoker(),
                validator=_SchemaValidator(catalog.schemas),
            )
        )
        contributions: list[HostProviderContributionV4] = []

        def invoke(
            operation_id: str,
            payload: Mapping[str, Any],
            invocation: HostProviderInvocationContextV4,
        ) -> Mapping[str, Any]:
            del invocation
            return provider.invoke(operation_id, payload)

        for binding in context.provider_bindings:
            domain_id = context.domain_ids.get(
                (
                    binding.operation.contract_id,
                    binding.operation.operation_id,
                    binding.principal_ref.value,
                )
            )
            if domain_id is None:
                store.close()
                raise WorkflowDenied("Workflow Provider domain binding is unavailable")
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
        return CapturedHostProviderV4(tuple(contributions), store.close)


WORKFLOW_HOST_PROVIDER_FACTORY = WorkflowHostProviderFactoryV4()


__all__ = ["WORKFLOW_HOST_PROVIDER_FACTORY", "WorkflowHostProviderFactoryV4"]
