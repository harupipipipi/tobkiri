"""Protocol v4 Host Provider integration hook for Workflow."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from jsonschema import Draft202012Validator, validators
from jsonschema.exceptions import ValidationError
from referencing import Registry, Resource
from referencing.exceptions import Unresolvable
from referencing.jsonschema import DRAFT202012

from core_runtime.host_provider_backend_v4 import (
    CapturedHostProviderV4,
    HostProviderCaptureContextV4,
    HostProviderContributionV4,
    HostInvocationEvidenceContributionV4,
    HostProviderInvocationContextV4,
)
from tobkiri_protocol.canonical import canonical_digest
from tobkiri_protocol.flow_values import SOUND_TYPE, VALUE_TYPE_KEY, validate_sound

from .engine import WorkflowEngineV4
from .models import WorkflowDenied
from .attempt_adapter import CapturedWorkflowAttemptAdapterV4
from .attempt_port import WorkflowAttemptDeclarationV4
from .evidence import EVIDENCE_KIND, WorkflowAttemptEvidence
from ..invocation_evidence import EvidenceBinding, EvidenceDenied
from .provider import (
    WORKFLOW_FUNCTION_PRINCIPAL, WORKFLOW_STOP_FUNCTION_PRINCIPAL,
    WorkflowProviderV4, WorkflowStopProviderV4,
)
from .store import WorkflowStoreV4
from .run_ownership import captured_run_owner_scope_digest


class _ResolvedCatalog:
    """Project the immutable operation catalog captured by the Host."""

    def __init__(self, context: HostProviderCaptureContextV4) -> None:
        operations: dict[str, dict[str, Any]] = {}
        self.schemas: dict[str, Mapping[str, Any]] = {}
        output_schema_bindings: dict[tuple[str, str, str, str], str] = {}
        outbound = getattr(context, "outbound_catalog_bindings", None)
        # Production supplies exact outgoing edges. Legacy fixture contexts
        # retain their explicit catalog; dispatch still requires the Host port.
        palette_bindings = context.catalog_bindings if outbound is None else outbound
        for binding in palette_bindings:
            input_digest = canonical_digest(binding.operation.input_schema)
            output_digest = canonical_digest(binding.operation.output_schema)
            self.schemas[input_digest] = binding.operation.input_schema
            self.schemas[output_digest] = binding.operation.output_schema
            operation: dict[str, Any] = {
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
            output_schema_bindings[
                (
                    operation["contract_id"],
                    operation["contract_revision_digest"],
                    operation["operation_id"],
                    operation["function_principal_id"],
                )
            ] = output_digest
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
        self._snapshot = {
            **body,
            "catalog_digest": canonical_digest(body),
            # Display-only schema metadata: deliberately outside `body`, so the
            # persisted catalog digest and compiled/run snapshots never change
            # for projection additions.  `schemas` maps schema digest to the
            # captured canonical document; `operation_output_schemas` pins each
            # exact operation identity to its captured output schema digest.
            "schemas": self.schemas,
            "operation_output_schemas": [
                {
                    "contract_id": key[0],
                    "contract_revision_digest": key[1],
                    "operation_id": key[2],
                    "function_principal_id": key[3],
                    "output_schema_digest": digest_value,
                }
                for key, digest_value in sorted(output_schema_bindings.items())
            ],
        }

    def snapshot(self) -> Mapping[str, Any]:
        """Return the exact activation-scoped Contract catalog."""
        return self._snapshot


def _validate_value_type(validator: Any, type_id: Any, instance: Any, schema: Any):
    """Standard value semantics augment, never replace, the captured schema."""
    del validator, schema
    if type_id == SOUND_TYPE:
        try:
            validate_sound(instance)
        except ValueError:
            yield ValidationError("sound bytes do not satisfy Sound v1")


_ValueValidator = validators.extend(
    Draft202012Validator, {VALUE_TYPE_KEY: _validate_value_type},
)


class _SchemaValidator:
    """Validate only schemas captured from the resolved operation catalog."""

    def __init__(self, schemas: Mapping[str, Mapping[str, Any]]) -> None:
        self._schemas = dict(schemas)
        # Explicit registry disables jsonschema's implicit remote retrieval.
        # Only documents captured by this Host catalog may satisfy references.
        registry: Registry[Any] = Registry()
        resources: dict[str, Resource[Any]] = {}
        for schema in self._schemas.values():
            schema_id = schema.get("$id")
            if isinstance(schema_id, str) and schema_id:
                resource = Resource.from_contents(dict(schema), default_specification=DRAFT202012)
                if schema_id in resources and resources[schema_id].contents != resource.contents:
                    raise WorkflowDenied("captured schema identity is ambiguous")
                resources[schema_id] = resource
        self._registry = registry.with_resources(resources.items())

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
            _ValueValidator(schema, registry=self._registry).validate(value)
        except ValidationError:
            return ("input does not satisfy the captured schema",)
        except Unresolvable:
            return ("schema reference is outside the captured catalog",)
        return ()


class WorkflowHostProviderFactoryV4:
    """Capture Workflow operations from exact resolved Function bindings."""

    function_id = WORKFLOW_FUNCTION_PRINCIPAL
    workflow_attempt_declaration = WorkflowAttemptDeclarationV4()
    cancellation_group = "workflow-v4"
    cancellation_role = "execute"

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
        store = WorkflowStoreV4(context.state_root / context.profile_id / "workflow-v4.sqlite3")
        contributions: list[HostProviderContributionV4] = []

        def invoke(
            operation_id: str,
            payload: Mapping[str, Any],
            invocation: HostProviderInvocationContextV4,
        ) -> Mapping[str, Any]:
            invocation.assert_current()
            # A fresh immutable adapter per request prevents parallel runs from
            # replacing a mutable "current invocation" or reusing an old lease.
            adapter = CapturedWorkflowAttemptAdapterV4(
                store=store,
                port=getattr(context, "workflow_attempt_port", None),
                invocation=invocation,
            )
            provider = WorkflowProviderV4(
                WorkflowEngineV4(
                    store=store,
                    catalog=catalog,
                    authority=adapter,
                    invoker=adapter,
                    validator=_SchemaValidator(catalog.schemas),
                    owner_scope_digest=captured_run_owner_scope_digest(invocation),
                )
            )
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
        def resolve_evidence(reference: str, binding: EvidenceBinding) -> WorkflowAttemptEvidence:
            if binding.profile_id != context.profile_id or binding.plan_digest != context.plan_digest:
                raise EvidenceDenied("Workflow evidence belongs to another capture")
            return WorkflowAttemptEvidence(store, reference, binding)

        evidence = tuple(
            HostInvocationEvidenceContributionV4(
                EVIDENCE_KIND, principal, resolve_evidence, data_fields=("value",),
            )
            for principal in sorted({item.principal_ref.value for item in context.provider_bindings})
        )
        return CapturedHostProviderV4(tuple(contributions), store.close, evidence)


class WorkflowStopHostProviderFactoryV4:
    """Capture the verified stop surface for Workflow run cancellation.

    The stop principal shares the same Pack artifact and the same
    ``workflow-v4`` cancellation group, but is bound with the ``stop``
    role — the only role which may signal a tracked in-flight child and
    observe its verified drain proof.
    """

    function_id = WORKFLOW_STOP_FUNCTION_PRINCIPAL
    cancellation_group = "workflow-v4"
    cancellation_role = "stop"

    def capture(
        self,
        context: HostProviderCaptureContextV4,
    ) -> CapturedHostProviderV4:
        """Build the stop surface over the same durable workflow store."""
        if not context.provider_bindings or any(
            binding.function.function_id != self.function_id
            for binding in context.provider_bindings
        ):
            raise WorkflowDenied("Workflow stop bindings are incomplete")
        catalog = _ResolvedCatalog(context)
        store = WorkflowStoreV4(
            context.state_root / context.profile_id / "workflow-v4.sqlite3"
        )
        validator = _SchemaValidator(catalog.schemas)
        contributions: list[HostProviderContributionV4] = []

        def invoke(
            operation_id: str,
            payload: Mapping[str, Any],
            invocation: HostProviderInvocationContextV4,
        ) -> Mapping[str, Any]:
            invocation.assert_current()
            adapter = CapturedWorkflowAttemptAdapterV4(
                store=store, port=context.workflow_attempt_port, invocation=invocation,
            )
            engine = WorkflowEngineV4(
                store=store, catalog=catalog, authority=adapter,
                invoker=adapter, validator=validator,
                owner_scope_digest=captured_run_owner_scope_digest(invocation),
            )
            return WorkflowStopProviderV4(engine).invoke(operation_id, payload)

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
                raise WorkflowDenied("Workflow stop domain binding is unavailable")
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
WORKFLOW_STOP_HOST_PROVIDER_FACTORY = WorkflowStopHostProviderFactoryV4()


__all__ = [
    "WORKFLOW_HOST_PROVIDER_FACTORY",
    "WORKFLOW_STOP_HOST_PROVIDER_FACTORY",
    "WorkflowHostProviderFactoryV4",
    "WorkflowStopHostProviderFactoryV4",
]
