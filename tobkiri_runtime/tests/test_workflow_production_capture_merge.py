"""Exact multi-operation admission for the merged captured Workflow port."""
from types import SimpleNamespace as NS

import pytest

from core_runtime.authority.v4 import AuthorityDenied
from core_runtime.workflow_v4.attempt_port import WorkflowAttemptDeclarationV4
from core_runtime.workflow_v4.production_capture import capture_workflow_port_assembly_v4
from core_runtime.workflow_v4.provider import WORKFLOW_STOP_FUNCTION_PRINCIPAL
from tobkiri_host.models import OpaqueAuthorityRef


@pytest.fixture
def capture_case():
    artifact = NS(pack_id="workflow-pack", publisher_lineage="publisher", digest="artifact-a")
    function = NS(function_id="tobkiri.workflow.provider", implementation_digest="implementation-a")
    factory = NS(function_id=function.function_id, workflow_attempt_declaration=WorkflowAttemptDeclarationV4())
    bindings = []
    for operation in ("run.advance", "run.step.execute", "run.step.resume", "run.cancel", "definition.get"):
        bindings.append(NS(
            artifact=artifact, function=function,
            operation=NS(contract_id="tobkiri.workflow.v4", revision_digest="revision-a", operation_id=operation),
            principal_ref=OpaqueAuthorityRef("principal." + operation),
        ))
    target = NS(
        operation=NS(contract_id="target.v1", revision_digest="target-revision", operation_id="execute"),
        principal_ref=OpaqueAuthorityRef("target-principal"),
    )
    edges = [NS(caller=NS(principal_id=item.principal_ref.value), resolved_binding=target,
                ceilings=NS(caller_effect="same-ceiling"), authority_mode="profile_grant")
             for item in bindings]
    factories = [(function.function_id, tuple(bindings), factory, "host")]
    return factories, edges, bindings, factory, artifact


def test_equivalent_distinct_operation_principals_keep_canonical_caller(capture_case):
    factories, edges, bindings, factory, _ = capture_case
    result = capture_workflow_port_assembly_v4(factories, edges)
    assert result.binding is bindings[0]
    assert result.factory is factory
    assert result.routes[0].caller_principal == bindings[0].principal_ref
    assert result.admitted_invocations == tuple(sorted(
        ("tobkiri.workflow.v4", item.operation.operation_id, item.principal_ref.value)
        for item in bindings[:-1]
    ))
    assert result.supplies(factory)
    assert not result.supplies(NS(**vars(factory)))


@pytest.mark.parametrize("changed", ["artifact", "function", "revision", "contract"])
def test_mismatched_execution_identity_is_denied(capture_case, changed):
    factories, edges, bindings, _, artifact = capture_case
    candidate = bindings[2]
    if changed == "artifact":
        candidate.artifact = NS(**{**vars(artifact), "digest": "artifact-b"})
    elif changed == "function":
        candidate.function = NS(function_id="foreign.function", implementation_digest="implementation-a")
    elif changed == "revision":
        candidate.operation.revision_digest = "revision-b"
    else:
        candidate.operation.contract_id = "foreign.v4"
    with pytest.raises(AuthorityDenied):
        capture_workflow_port_assembly_v4(factories, edges)


@pytest.mark.parametrize("changed", ["target", "ceiling", "mode", "missing", "extra"])
def test_each_admitted_operation_requires_identical_signed_routes(capture_case, changed):
    factories, edges, _, _, _ = capture_case
    candidate = edges[2]
    if changed == "target":
        candidate.resolved_binding = NS(
            operation=NS(contract_id="foreign.v1", revision_digest="foreign", operation_id="execute"),
            principal_ref=OpaqueAuthorityRef("foreign-principal"),
        )
    elif changed == "ceiling":
        candidate.ceilings = NS(caller_effect="wider-ceiling")
    elif changed == "mode":
        candidate.authority_mode = "interactive_only"
    elif changed == "missing":
        edges.remove(candidate)
    else:
        edges.append(NS(
            caller=candidate.caller,
            resolved_binding=NS(operation=NS(contract_id="extra.v1", revision_digest="extra", operation_id="execute"),
                                principal_ref=OpaqueAuthorityRef("extra-principal")),
            ceilings=NS(caller_effect="same-ceiling"), authority_mode="profile_grant",
        ))
    with pytest.raises(AuthorityDenied):
        capture_workflow_port_assembly_v4(factories, edges)


def _add_stop(factories, artifact):
    function = NS(function_id=WORKFLOW_STOP_FUNCTION_PRINCIPAL, implementation_digest="stop-implementation")
    factory = NS(function_id=WORKFLOW_STOP_FUNCTION_PRINCIPAL,
                 cancellation_group="workflow-v4", cancellation_role="stop")
    binding = NS(
        artifact=artifact, function=function,
        operation=NS(contract_id="tobkiri.workflow.stop.v4", operation_id="run.stop"),
        principal_ref=OpaqueAuthorityRef("exact-stop-principal"),
    )
    factories.append((WORKFLOW_STOP_FUNCTION_PRINCIPAL, (binding,), factory, "host"))
    return binding, factory


def test_only_exact_stop_factory_receives_port(capture_case):
    factories, edges, _, _, artifact = capture_case
    binding, factory = _add_stop(factories, artifact)
    result = capture_workflow_port_assembly_v4(factories, edges)
    assert result.stop_principals == (binding.principal_ref,)
    assert result.supplies(factory)
    assert not result.supplies(NS(**vars(factory)))
    assert all(principal != binding.principal_ref.value for _, _, principal in result.admitted_invocations)


@pytest.mark.parametrize("changed", ["artifact_digest", "pack", "publisher", "contract", "operation", "role", "group", "function"])
def test_stop_capture_rejects_identity_or_role_substitution(capture_case, changed):
    factories, edges, _, _, artifact = capture_case
    binding, factory = _add_stop(factories, NS(**vars(artifact)))
    if changed == "artifact_digest":
        binding.artifact.digest = "different-artifact"
    elif changed == "pack":
        binding.artifact.pack_id = "different-pack"
    elif changed == "publisher":
        binding.artifact.publisher_lineage = "different-publisher"
    elif changed == "contract":
        binding.operation.contract_id = "different.contract"
    elif changed == "operation":
        binding.operation.operation_id = "run.execute"
    elif changed == "role":
        factory.cancellation_role = "execute"
    elif changed == "group":
        factory.cancellation_group = "foreign"
    else:
        binding.function.function_id = "foreign.function"
    with pytest.raises(AuthorityDenied):
        capture_workflow_port_assembly_v4(factories, edges)


@pytest.mark.parametrize("changed", ["lookalike_declaration", "wrong_function", "empty_bindings", "duplicate_canonical", "duplicate_coordinator"])
def test_malformed_or_ambiguous_declaration_fails_closed(capture_case, changed):
    factories, edges, bindings, factory, _ = capture_case
    if changed == "lookalike_declaration":
        factory.workflow_attempt_declaration = NS(function_id="tobkiri.workflow.provider")
    elif changed == "wrong_function":
        factory.function_id = "foreign.function"
    elif changed == "empty_bindings":
        factories[0] = (factories[0][0], (), factory, "host")
    elif changed == "duplicate_canonical":
        factories[0] = (factories[0][0], (*bindings, bindings[0]), factory, "host")
    else:
        factories.append(factories[0])
    with pytest.raises(AuthorityDenied):
        capture_workflow_port_assembly_v4(factories, edges)


def test_absent_declaration_exposes_no_attempt_port():
    assert capture_workflow_port_assembly_v4([], []) is None


@pytest.mark.parametrize("operations,expected", [
    (("run.advance", "run.step.resume"), "run.advance"),
    (("run.step.execute", "run.step.resume"), "run.step.execute"),
    (("run.step.resume", "run.step.retry"), "run.step.resume"),
    (("run.step.retry",), "run.step.retry"),
])
def test_canonical_selection_uses_only_existing_dispatch_operations(capture_case, operations, expected):
    factories, edges, bindings, factory, artifact = capture_case
    retry = NS(
        artifact=artifact, function=bindings[0].function,
        operation=NS(contract_id="tobkiri.workflow.v4", revision_digest="revision-a", operation_id="run.step.retry"),
        principal_ref=OpaqueAuthorityRef("principal.run.step.retry"),
    )
    candidates = [*bindings, retry]
    selected = tuple(item for item in reversed(candidates) if item.operation.operation_id in operations)
    edges.append(NS(caller=NS(principal_id=retry.principal_ref.value), resolved_binding=edges[0].resolved_binding,
                    ceilings=edges[0].ceilings, authority_mode=edges[0].authority_mode))
    factories[0] = (factories[0][0], selected, factory, "host")
    result = capture_workflow_port_assembly_v4(factories, edges)
    assert result.binding.operation.operation_id == expected
    assert result.binding in selected
    assert {operation for _, operation, _ in result.admitted_invocations} == set(operations)
    assert all(route.caller_principal == result.binding.principal_ref for route in result.routes)


@pytest.mark.parametrize("operations", [("definition.get",), ("definition.get", "run.cancel")])
def test_read_only_selection_leaves_attempt_port_unavailable(capture_case, operations):
    factories, edges, bindings, factory, _ = capture_case
    selected = tuple(item for item in bindings if item.operation.operation_id in operations)
    factories[0] = (factories[0][0], selected, factory, "host")
    assert capture_workflow_port_assembly_v4(factories, edges) is None
