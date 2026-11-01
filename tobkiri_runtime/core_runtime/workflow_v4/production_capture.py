"""Join verified Workflow factory declarations to captured signed edges."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Sequence

from core_runtime.authority.v4 import AuthorityDenied, AuthorityScope
from tobkiri_host.contracts import ResolvedOperationBinding
from tobkiri_host.models import OpaqueAuthorityRef

from .attempt_port import CapturedWorkflowAttemptRouteV4, WorkflowAttemptDeclarationV4
from .provider import WORKFLOW_STOP_FUNCTION_PRINCIPAL


@dataclass(frozen=True)
class CapturedWorkflowPortAssemblyV4:
    """Host-only assembly, selected from already verified factory instances."""

    factory: Any
    binding: ResolvedOperationBinding
    admitted_invocations: tuple[tuple[str, str, str], ...]
    stop_factories: tuple[Any, ...]
    stop_principals: tuple[OpaqueAuthorityRef, ...]
    routes: tuple[CapturedWorkflowAttemptRouteV4, ...]

    def supplies(self, factory: Any) -> bool:
        """Do not authorize a look-alike factory by a caller-provided name."""
        return factory is self.factory or any(factory is item for item in self.stop_factories)


def capture_workflow_port_assembly_v4(
    factories: Sequence[tuple[str, tuple[ResolvedOperationBinding, ...], Any, str]],
    edges: Sequence[Any],
) -> CapturedWorkflowPortAssemblyV4 | None:
    """Capture only the declared coordinator and its signed outgoing routes."""
    selected = []
    for function_id, bindings, factory, _backend in factories:
        declaration = getattr(factory, "workflow_attempt_declaration", None)
        if declaration is None:
            continue
        if (
            type(declaration) is not WorkflowAttemptDeclarationV4
            or declaration.function_id != function_id
            or factory.function_id != function_id
            or not bindings
            or any(item.function.function_id != function_id for item in bindings)
        ):
            raise AuthorityDenied("Workflow attempt declaration is invalid")
        selected.append((bindings, factory))
    if not selected:
        return None
    if len(selected) != 1:
        raise AuthorityDenied("Workflow attempt coordinator is ambiguous")
    bindings, factory = selected[0]
    declaration = WorkflowAttemptDeclarationV4()
    reference = bindings[0]
    if any(
        item.operation.contract_id != declaration.contract_id
        or item.operation.revision_digest != reference.operation.revision_digest
        or item.function != reference.function
        or item.artifact != reference.artifact
        for item in bindings
    ):
        raise AuthorityDenied("Workflow invocation bindings differ from canonical capture")
    # Use only already selected public operations, with deterministic priority.
    # Read-only Profiles need no execution port and no additional public edge.
    canonical = []
    for operation_id in ("run.advance", "run.step.execute", "run.step.resume", "run.step.retry"):
        canonical = [item for item in bindings if item.operation.operation_id == operation_id]
        if canonical:
            break
    if not canonical:
        return None
    if len(canonical) != 1:
        raise AuthorityDenied("Workflow canonical execution binding is ambiguous")
    binding = canonical[0]
    admitted = tuple(sorted(
        (item.operation.contract_id, item.operation.operation_id, item.principal_ref.value)
        for item in bindings if item.operation.operation_id in declaration.operation_ids
    ))
    routes: dict[tuple[str, str, str, str], CapturedWorkflowAttemptRouteV4] = {}
    for edge in edges:
        if edge.caller.principal_id != binding.principal_ref.value:
            continue
        route = CapturedWorkflowAttemptRouteV4(
            caller_principal=binding.principal_ref,
            binding=edge.resolved_binding,
            caller_effect_ceiling=edge.ceilings.caller_effect,
            authority_mode=edge.authority_mode,
        )
        if route.key in routes and routes[route.key] != route:
            raise AuthorityDenied("Workflow outgoing route is ambiguous")
        routes[route.key] = route
    canonical_routes = {
        key: (route.binding, route.caller_effect_ceiling, route.authority_mode)
        for key, route in routes.items()
    }
    for _contract, _operation, principal in admitted:
        candidate_routes: dict[
            tuple[str, str, str, str],
            tuple[ResolvedOperationBinding, AuthorityScope, str],
        ] = {}
        for edge in edges:
            if edge.caller.principal_id != principal:
                continue
            operation = edge.resolved_binding.operation
            key = (operation.contract_id, operation.revision_digest,
                   operation.operation_id, edge.resolved_binding.principal_ref.value)
            value = (edge.resolved_binding, edge.ceilings.caller_effect, edge.authority_mode)
            if key in candidate_routes and candidate_routes[key] != value:
                raise AuthorityDenied("Workflow admitted route is ambiguous")
            candidate_routes[key] = value
        if candidate_routes != canonical_routes:
            raise AuthorityDenied("Workflow operation changes its canonical dispatch ceiling")
    stop_factories = []
    stop_principals: set[str] = set()
    for function_id, stop_bindings, stop_factory, _backend in factories:
        if function_id != WORKFLOW_STOP_FUNCTION_PRINCIPAL:
            continue
        if (
            not stop_bindings
            or stop_factory.function_id != WORKFLOW_STOP_FUNCTION_PRINCIPAL
            or getattr(stop_factory, "cancellation_group", None) != "workflow-v4"
            or getattr(stop_factory, "cancellation_role", None) != "stop"
            or any(
                item.artifact != binding.artifact
                or item.function.function_id != WORKFLOW_STOP_FUNCTION_PRINCIPAL
                or item.operation.contract_id != "tobkiri.workflow.stop.v4"
                or item.operation.operation_id != "run.stop"
                for item in stop_bindings
            )
        ):
            raise AuthorityDenied("Workflow stop binding is invalid")
        stop_factories.append(stop_factory)
        stop_principals.update(item.principal_ref.value for item in stop_bindings)
    return CapturedWorkflowPortAssemblyV4(
        factory, binding, admitted, tuple(stop_factories),
        tuple(OpaqueAuthorityRef(value) for value in sorted(stop_principals)),
        tuple(routes[key] for key in sorted(routes)),
    )
