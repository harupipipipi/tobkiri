"""Selection controller restricted to an actual catalog-pinned finite route."""

from typing import Any, Mapping

from tobkiri_host.action_approval_policy import ActionApprovalPolicyController
from core_runtime.authority.v4 import AuthorityDenied, AuthorityScope, FunctionPrincipal
from tobkiri_host.models import ExecutionKind, PackageKind

CONTRACT = "tobkiri.action.host.approval-policy.v1"
OPERATION = "host.action_approval_policy.select"
FUNCTION = "rumi_host_authority_bridge_pack.host-authority.approval-policy"


class RouteBoundActionApprovalPolicyController(ActionApprovalPolicyController):
    """Require Host-captured verified binding, never a caller capability string."""

    CAPABILITY = "operation.invoke"

    def __init__(
        self, authority: Any, authority_store: Any, *, catalog: Any, prepared: Any, clock: Any
    ) -> None:
        super().__init__(authority, authority_store, clock=clock)
        self.binding = catalog.resolve_pinned(CONTRACT, OPERATION)
        if prepared.binding != self.binding:
            raise AuthorityDenied("prepared selection route changed")
        self.prepared_request_digest = prepared.request_digest
        self.prepared = prepared
        binding = self.binding
        principal = FunctionPrincipal(
            parent_artifact_digest=binding.artifact.digest,
            function_implementation_digest=binding.function.implementation_digest,
            function_id=binding.function.function_id,
            contract_revision_digest=binding.operation.revision_digest,
            operation_id=binding.operation.operation_id,
        )
        if (
            binding.artifact.package_kind is not PackageKind.HOST_EXTENSION
            or binding.function.function_id != FUNCTION
            or binding.variant.execution_kind is not ExecutionKind.HOST_EXTENSION
            or binding.variant.backend != "tobkiri.python-host-v4"
            or principal.principal_id != binding.principal_ref.value
        ):
            raise AuthorityDenied("signed policy selection route is unavailable")

    def _native_request_digest(self, capture_digest: str) -> str:
        return self.prepared_request_digest

    def _selection_scope(
        self, template: Any, capture: Mapping[str, Any], digest: str
    ) -> AuthorityScope:
        if template.target_principal != self.binding.principal_ref:
            raise AuthorityDenied("native selection target is outside signed route")
        return AuthorityScope(
            capability="operation.invoke",
            semantics_digest=self.binding.operation.revision_digest,
            dimensions={
                "contract": (CONTRACT,),
                "operation": (OPERATION,),
                "invocation_owner_id": (template.invocation_owner_id,),
                "caller_session_id": (template.context.caller_session_id,),
                "plan_digest": (template.context.plan_digest,),
                "workspace_root": (capture["workspace"],),
                "turn_id": (capture["turn"],),
                "conversation_id": (capture["conversation"],),
                "mode": (capture["mode"],),
            },
            quotas={"operations": 1},
            exact_request_digest=digest,
            opaque=False,
        )
