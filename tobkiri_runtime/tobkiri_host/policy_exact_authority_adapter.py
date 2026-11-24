"""Public Host-only settlement port over the actual v4 adapter and kernel."""

from __future__ import annotations

from dataclasses import replace
from typing import Any, Callable, Mapping

from core_runtime.authority.v4 import (
    AuthorityDenied,
    AuthorityScope,
    FunctionPrincipal,
    InvocationContext,
    LeaseState,
    authority_digest,
)
from tobkiri_host.committed_selection_receipt import CommittedSelectionReceiptController
from core_runtime.authority.policy_exact_grant import (
    RetainedExactOperation,
    KIND,
    PolicyExactGrantKernel,
)
from tobkiri_host.authority_v4 import (
    AuthorityV4Adapter,
    PrincipalReferenceResolver,
    TriggerAuthorityResolver,
)
from tobkiri_host.models import OpaqueAuthorityRef, RequestContext


from tobkiri_host.host_tool_policy_types import HostCapturedToolPolicy, SettledPolicyOperation


class CommittedPolicyDerivationRoot:
    """Rehydrate operations from authenticated encrypted derivation provenance.

    Host composition supplies actual prepared validation and current saved-root
    proof ports. Neither port creates authority; native committed receipt and
    finite verified ceilings are still required at every stage.
    """

    def __init__(
        self,
        *,
        policy: Any,
        command: Any,
        expected: Mapping[str, Any],
        prepared_validator: Callable[[RetainedExactOperation], None],
        current_guard: Callable[[RetainedExactOperation], None],
        saved_root_guard: Callable[[Any, Any, Mapping[str, Any]], None],
        review_transport_verifier: Callable[[Mapping[str, Any]], None] | None = None,
        reviewer: Any = None,
    ) -> None:
        self.policy, self.command = policy, command
        self.expected = dict(expected)
        self.prepared_validator = prepared_validator
        self.current_guard = current_guard
        self.saved_root_guard = saved_root_guard
        self.reviewer = reviewer
        self.review_transport_verifier = review_transport_verifier
        self.selection_id = policy.store.physical_id(
            "policy-selection-" + command.context.request_id
        )
        self.key = policy._validated_capture(command)["key"]

    def capture(self) -> dict[str, Any]:
        """Return authenticated exact native capture, requiring finite boundary."""
        capture = self.policy._validated_capture(self.command)
        if capture.get("delegation_boundary", {}).get("authorization_kind") != KIND:
            raise AuthorityDenied("native policy has no finite delegation boundary")
        return capture

    def receipt_record(self) -> Any:
        """Return the current encrypted committed root receipt."""
        record = self.policy.store.read(self.key)
        if record is None:
            raise AuthorityDenied("native root receipt unavailable")
        return record

    def receipt_digest(self) -> str:
        """Bind derivation to the exact selected receipt state."""
        return authority_digest(self.receipt_record()[1])

    def resolve_current(self) -> str:
        """Repeat real consumed native proof, expiry/revocation and capture checks."""
        return CommittedSelectionReceiptController(self.policy).resolve(
            self.key, self.command, **self.expected
        )

    def capture_current(self, invocation: Any, authenticated_root: Any) -> HostCapturedToolPolicy:
        """Match actual saved root and owner before any elevated mode branch."""
        capture = self.capture()
        self.saved_root_guard(invocation, authenticated_root, capture)
        from tobkiri_host.saved_tool_context import SAVED_TOOL_CAPTURE_FIELDS
        from tobkiri_protocol.canonical import canonical_digest

        root_digest = canonical_digest(
            {key: getattr(authenticated_root, key) for key in SAVED_TOOL_CAPTURE_FIELDS}
        )
        mode = self.resolve_current()

        def guard() -> None:
            self.saved_root_guard(invocation, authenticated_root, self.capture())
            current_digest = canonical_digest(
                {key: getattr(authenticated_root, key) for key in SAVED_TOOL_CAPTURE_FIELDS}
            )
            if self.resolve_current() != mode or current_digest != root_digest:
                raise AuthorityDenied("native selected mode changed")

        return HostCapturedToolPolicy(
            mode,
            self.selection_id,
            capture["workspace"],
            root_digest,
            capture["turn"],
            guard,
            authority_digest(capture),
        )

    def validate_prepared(self, operation: RetainedExactOperation) -> None:
        """Ask the captured actual Broker to authenticate its entire snapshot."""
        self.prepared_validator(operation)

    def review_exact(self, operation: RetainedExactOperation) -> Any:
        """Run only independently authorized captured reviewer, never settings."""
        if self.reviewer is None:
            raise AuthorityDenied("captured independent reviewer unavailable")
        return self.reviewer.review_exact(operation)

    def validate_review_evidence(self, evidence: Any, operation: RetainedExactOperation) -> None:
        """Require independently authorized real committed reviewer transport."""
        if self.review_transport_verifier is None:
            raise AuthorityDenied("independent reviewer transport verifier unavailable")
        capture = self.capture()
        if (
            evidence.operation_capture_digest != operation.digest
            or evidence.reviewer_boundary_digest != authority_digest(capture["reviewer"])
            or evidence.provenance.get("operation_capture_digest") != operation.digest
        ):
            raise AuthorityDenied("reviewer exact operation provenance changed")
        self.review_transport_verifier(evidence.provenance)

    def retain_operation(self, grant_id: str, operation: RetainedExactOperation) -> None:
        """Verify atomic settlement already persisted the exact operation."""
        if self.retained_operation(grant_id).digest != operation.digest:
            raise AuthorityDenied("durable policy operation changed")

    def _entry(self, grant_id: str) -> Mapping[str, Any]:
        record = self.policy.authority_store.get_host_pending_effect(self.selection_id)
        entry = record[1].get("policy_derived_grants", {}).get(grant_id) if record else None
        if not entry:
            raise AuthorityDenied("durable policy operation unavailable")
        return entry

    def has_operation(self, grant_id: str) -> bool:
        """Report exact authenticated provenance presence, not authority."""
        record = self.policy.authority_store.get_host_pending_effect(self.selection_id)
        return bool(record and grant_id in record[1].get("policy_derived_grants", {}))

    def retained_operation(self, grant_id: str) -> RetainedExactOperation:
        """Rehydrate exact operation after restart with a newly captured jail guard."""
        data = self._entry(grant_id)["retained_operation"]
        context = dict(data["context"])
        context["target"] = FunctionPrincipal.from_dict(context["target"])
        context["call_chain"] = tuple(context.get("call_chain", ()))
        operation = RetainedExactOperation(
            data["prepared_snapshot"],
            InvocationContext(**context),
            AuthorityScope.from_dict(data["scope"]),
            data["contract"],
            data["operation"],
            data["operation_class"],
            data["workspace_root"],
            lambda: None,
        )
        return replace(operation, assert_current=lambda: self.current_guard(operation))


class PolicyExactAuthorityV4Adapter(AuthorityV4Adapter):
    """Expose exact policy settlement without exposing internal query conversion."""

    _kernel: PolicyExactGrantKernel

    def __init__(
        self,
        kernel: PolicyExactGrantKernel,
        principal_resolver: PrincipalReferenceResolver,
        *,
        trigger_resolver: TriggerAuthorityResolver | None = None,
    ) -> None:
        """Retain the exact-grant kernel supplied by Host composition."""
        super().__init__(kernel, principal_resolver, trigger_resolver=trigger_resolver)

    def _policy_operation(
        self,
        *,
        prepared_snapshot: Any,
        request_context: RequestContext,
        binding: Any,
        base_ceiling: Mapping[str, Any],
        workspace_root: str,
        assert_current: Callable[[], None],
        parent_lease: Any = None,
        pending_effect_id: str | None = None,
    ) -> RetainedExactOperation:
        snapshot = (
            prepared_snapshot.to_dict()
            if hasattr(prepared_snapshot, "to_dict")
            else dict(prepared_snapshot)
        )
        if pending_effect_id is not None:
            original = AuthorityScope.from_dict(base_ceiling)
            base_ceiling = replace(
                original,
                dimensions={**original.dimensions, "pending_effect_id": (pending_effect_id,)},
            ).to_dict()
        invocation, scope = super()._translate_query(
            request_context, binding.principal_ref, snapshot["request_digest"], base_ceiling
        )
        if parent_lease is not None:
            token = self._decode_transport(parent_lease)
            parent, state = self._kernel.store.inspect_lease_token(token)
            if state is not LeaseState.DISPATCHED:
                raise AuthorityDenied("nested policy parent is not dispatched")
            chain = parent.call_chain + (parent.caller.principal_id,)
            if invocation.call_chain and invocation.call_chain != chain:
                raise AuthorityDenied("supplied context ancestry differs from actual parent")
            invocation = replace(invocation, parent_lease_id=parent.lease_id, call_chain=chain)
        return RetainedExactOperation(
            snapshot,
            invocation,
            scope,
            binding.operation.contract_id,
            binding.operation.operation_id,
            binding.operation.effect_class.value,
            workspace_root,
            assert_current,
        )

    def check_policy_derived_operation(self, *, selection_id: str, **kwargs: Any) -> None:
        """Authenticate retained future admission without creating a Grant or review."""
        root, _ = self._kernel._root(selection_id)
        operation = self._policy_operation(**kwargs)
        self._kernel._validate_operation(root, operation)

    def settle_policy_derived_operation(
        self, *, selection_id: str, **kwargs: Any
    ) -> SettledPolicyOperation:
        """Settle one retained exact operation using actual native root proof."""
        operation = self._policy_operation(**kwargs)
        assert_current = kwargs["assert_current"]
        grant = self._kernel.derive_exact_operation_grant(selection_id, operation)
        root, _ = self._kernel._root(selection_id)
        retained = root.retained_operation(grant.grant_id)

        def guard() -> None:
            assert_current()
            current = root.retained_operation(grant.grant_id)
            self._kernel._derived(grant, current.context, current.scope)

        return SettledPolicyOperation(grant.scope.to_dict(), grant.grant_id, retained.digest, guard)

    def assert_policy_derived_operation_current(self, *, selection_id: str, grant_id: str) -> None:
        """Repeat exact policy proof without reserving or extending a Grant use."""
        root, _ = self._kernel._root(selection_id)
        grant = self._kernel.store.get_grant(grant_id)
        if grant is None:
            raise AuthorityDenied("policy-derived exact Grant unavailable")
        operation = root.retained_operation(grant_id)
        self._kernel._derived(grant, operation.context, operation.scope)

    def _translate_query(
        self,
        context: RequestContext,
        target_ref: OpaqueAuthorityRef,
        request_digest: str,
        effect_scope: Mapping[str, object],
    ) -> Any:
        invocation, scope = super()._translate_query(
            context, target_ref, request_digest, effect_scope
        )
        if scope.dimensions.get("authorization_kind") == (KIND,):
            identifiers = scope.dimensions.get("policy_derivation_id", ())
            if len(identifiers) != 1:
                raise AuthorityDenied("derived exact operation reference invalid")
            roots = [
                root
                for root in self._kernel.policy_roots.values()
                if root.has_operation(identifiers[0])
            ]
            if len(roots) != 1:
                raise AuthorityDenied("derived native root unavailable")
            expected = roots[0].retained_operation(identifiers[0]).context
            if expected.parent_lease_id:
                result = self._kernel.store.get_lease(expected.parent_lease_id)
                if result is None or result[1] is not LeaseState.DISPATCHED:
                    raise AuthorityDenied("derived actual parent no longer dispatched")
                parent = result[0]
                chain = parent.call_chain + (parent.caller.principal_id,)
                if invocation.call_chain and invocation.call_chain != chain:
                    raise AuthorityDenied("Broker actual parent ancestry changed")
                invocation = replace(invocation, call_chain=chain)
            invocation = replace(invocation, parent_lease_id=expected.parent_lease_id)
            if invocation != expected:
                raise AuthorityDenied("Broker derived exact operation context changed")
        return invocation, scope


def rehydrate_policy_root(
    *,
    selection_id: str,
    authority: Any,
    authority_store: Any,
    catalog: Any,
    restore_prepared: Callable[[Any, RequestContext], Any],
    clock: Callable[[], float],
    prepared_validator: Callable[[RetainedExactOperation], None],
    current_guard: Callable[[RetainedExactOperation], None],
    saved_root_guard: Callable[[Any, Any, Mapping[str, Any]], None],
    reviewer: Any = None,
    review_transport_verifier: Any = None,
) -> CommittedPolicyDerivationRoot:
    """Reauthenticate persisted selection against current actual Host topology.

    A changed boot/session/Profile/capture or unavailable source route denies;
    no missing registry entry silently downgrades previously selected policy.
    """
    from tobkiri_host.broker import PreparedInvocationSnapshot
    from tobkiri_host.interactive_effects import _context_from_dict
    from tobkiri_host.ports import InteractiveApprovalRequestCommand
    from tobkiri_host.policy_delegation_boundary import BoundedPolicySelectionController

    record = authority_store.get_host_pending_effect(selection_id)
    if record is None or record[1].get("record_kind") != "action_approval_policy_selection_v1":
        raise AuthorityDenied("durable native policy root unavailable")
    data = record[1]
    capture = data["capture"]
    command_data = dict(data["retained_native_command"])
    command_data["context"] = _context_from_dict(command_data["context"])
    command_data["target_principal"] = OpaqueAuthorityRef(command_data["target_principal"])
    command = InteractiveApprovalRequestCommand(**command_data)
    snapshot = PreparedInvocationSnapshot.from_dict(data["retained_native_prepared"])
    prepared = restore_prepared(snapshot, command.context)
    policy = BoundedPolicySelectionController(
        authority,
        authority_store,
        catalog=catalog,
        prepared=prepared,
        clock=clock,
        routes=tuple(capture["delegation_boundary"]["routes"]),
    )
    if policy.store.physical_id("policy-selection-" + command.context.request_id) != selection_id:
        raise AuthorityDenied("durable selection identity changed")
    expected = {
        key: capture[key] for key in ("mode", "reviewer", "workspace", "conversation", "turn")
    }
    root = CommittedPolicyDerivationRoot(
        policy=policy,
        command=command,
        expected=expected,
        prepared_validator=prepared_validator,
        current_guard=current_guard,
        saved_root_guard=saved_root_guard,
        reviewer=reviewer,
        review_transport_verifier=review_transport_verifier,
    )
    root.resolve_current()
    return root
