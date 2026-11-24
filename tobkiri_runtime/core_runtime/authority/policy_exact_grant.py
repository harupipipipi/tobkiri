"""Isolated kernel candidate for native-policy-derived exact operation authority.

No future native ApprovalRecord is produced. Existing kernels reject these
approval-less one-shot grants. Only this explicit authorization-kind verifier
accepts authenticated derivation provenance, repeating native root proof at
mint, authorization and dispatch. Production composition is intentionally absent.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
import hashlib
from typing import Any, Callable, Mapping

from core_runtime.authority.v4 import (
    AuthorityDenied,
    AuthorityScope,
    FunctionPrincipal,
    GrantLifetime,
    GrantRecord,
    InvocationContext,
    authority_digest,
    intersect_scopes,
)
from core_runtime.authority.v4_kernel import AuthorityKernel
from core_runtime.authority.policy_consumed_selection import assert_consumed_native_policy_selection

KIND = "native_selected_policy_exact_v1"
PREFIX = "policy-derived-v1-"


@dataclass(frozen=True)
class CapturedReviewEvidence:
    """Host reviewer result; side model grants no authority independently."""

    evidence_id: str
    operation_capture_digest: str
    reviewer_boundary_digest: str
    expires_at: float
    outcome: str
    provenance: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class RetainedExactOperation:
    """Prepared Broker snapshot plus actual kernel context and guarded jail path."""

    prepared_snapshot: Mapping[str, Any]
    context: InvocationContext
    scope: AuthorityScope
    contract: str
    operation: str
    operation_class: str
    workspace_root: str
    assert_current: Callable[[], None]

    @property
    def digest(self) -> str:
        """Bind review and provenance to every prepared argument and context."""
        return authority_digest(
            {
                "snapshot": dict(self.prepared_snapshot),
                "context": asdict(self.context),
                "scope": self.scope.to_dict(),
                "contract": self.contract,
                "operation": self.operation,
                "class": self.operation_class,
                "workspace": self.workspace_root,
            }
        )


class PolicyExactGrantKernel(AuthorityKernel):
    """Formal candidate with explicit tagged authority and current root proof.

    ``policy_roots`` is Host-only composition: native selection capture ID maps
    to its authenticated receipt resolver, finite boundary, retained command and
    reviewer port. Unknown or unregistered provenance fails closed after reload.
    """

    policy_roots: dict[str, Any]

    def _root(self, selection_id: str) -> Any:
        root = self.policy_roots.get(selection_id)
        if root is None:
            raise AuthorityDenied("policy derivation root is unavailable")
        selection = self.store.get_host_pending_effect(selection_id)
        if (
            selection is None
            or selection[1].get("record_kind") != "action_approval_policy_selection_v1"
        ):
            raise AuthorityDenied("native policy selection capture unavailable")
        capture = selection[1].get("capture")
        if not isinstance(capture, dict):
            raise AuthorityDenied("native policy capture malformed")
        expected_receipt_id = (
            "action-approval-policy-v1-" + hashlib.sha256(capture["key"].encode()).hexdigest()
        )
        receipt = self.store.get_host_pending_effect(expected_receipt_id)
        request = self.store.get_interactive_approval_request(root.command.context.request_id)
        if (
            receipt is None
            or receipt[1].get("state") != "committed_selection"
            or receipt[1].get("capture") != capture
            or root.capture() != capture
            or receipt[1].get("root_native_request") != root.command.context.request_id
            or request is None
            or request.redacted_metadata.get("policy_capture_digest") != authority_digest(capture)
            or request.base_scope.capability != "operation.invoke"
            or request.base_scope.dimensions.get("contract")
            != ("tobkiri.action.host.approval-policy.v1",)
            or request.base_scope.dimensions.get("operation")
            != ("host.action_approval_policy.select",)
            or capture.get("delegation_boundary", {}).get("authorization_kind") != KIND
            or receipt[1].get("mode") != capture.get("mode")
        ):
            raise AuthorityDenied("policy delegation is not covered by actual native boundary")
        assert_consumed_native_policy_selection(
            self.store, root.command, receipt[1]["committed_selection_lease"], self._clock
        )
        mode = root.resolve_current()
        if mode != capture["mode"]:
            raise AuthorityDenied("policy mode differs from native approved capture")
        if mode not in {"agent", "full"}:
            raise AuthorityDenied("native selection does not permit delegation")
        return root, mode

    def _validate_operation(self, root: Any, operation: RetainedExactOperation) -> Any:
        operation.assert_current()
        root.validate_prepared(operation)
        context, scope = operation.context, operation.scope
        capture = root.capture()
        if (
            scope.capability != "operation.invoke"
            or scope.exact_request_digest != context.request_digest
            or context.effect_digest != scope.digest
            or operation.prepared_snapshot.get("request_digest") != context.request_digest
            or Path(operation.workspace_root).resolve() != Path(capture["workspace"])
            or operation.workspace_root != capture["workspace"]
        ):
            raise AuthorityDenied("derived operation is outside exact captured scope")
        for field_name in (
            "profile_id",
            "activation_id",
            "activation_digest",
            "plan_digest",
            "profile_authority_digest",
            "security_epoch",
            "fencing_token",
        ):
            if getattr(context, field_name) != capture["context"][field_name]:
                raise AuthorityDenied("derived operation activation changed")
        domain, principal_id = self.store.resolve_authenticated_session(context.caller_session_id)
        caller = self._principal_in_domain(domain, principal_id)
        if (
            domain.profile_id != context.profile_id
            or domain.activation_id != context.activation_id
            or domain.fencing_token != context.fencing_token
            or domain.security_epoch != context.security_epoch
        ):
            raise AuthorityDenied("derived operation caller changed")
        routes = capture["delegation_boundary"]["routes"]
        matches = [
            route
            for route in routes
            if (
                route["contract"] == operation.contract
                and route["operation"] == operation.operation
                and route["target_principal_id"] == context.target.principal_id
                and route["caller_principal_id"] == caller.principal_id
                and route["operation_class"] == operation.operation_class
                and tuple(route["ancestor_chain"]) == context.call_chain
                and route["artifact_digest"] == context.target.parent_artifact_digest
                and route["implementation_digest"] == context.target.function_implementation_digest
                and scope.is_subset_of(AuthorityScope.from_dict(route["ceiling"]))
            )
        ]
        if len(matches) != 1:
            raise AuthorityDenied("derived operation has no exact native ceiling")
        self._validate_policy_parent(context, caller)
        target_domain = self.store.get_domain(context.target_domain_id)
        if target_domain is None:
            raise AuthorityDenied("derived target domain unavailable")
        self._validate_target_domain(context, target_domain)
        binding = self._binding_resolver.resolve_authority_binding(
            context=context, caller=caller, target=context.target
        )
        if not binding.validates_context(context):
            raise AuthorityDenied("derived signed authority binding changed")
        provider = self._select_provider_authority(
            context=context, target_domain=target_domain, request_scope=scope, now=self._clock()
        )
        if not scope.is_subset_of(
            intersect_scopes(
                binding.caller_effect_ceiling,
                binding.runtime_safety_ceiling,
                binding.profile_admin_ceiling,
                provider.scope,
                AuthorityScope.from_dict(matches[0]["ceiling"]),
            )
        ):
            raise AuthorityDenied("derived operation exceeds current signed ceilings")
        caller_lineage = matches[0].get("caller_publisher_lineage") or capture["caller_lineage"]
        if context.parent_lease_id:
            parent_result = self.store.get_lease(context.parent_lease_id)
            if parent_result is None:
                raise AuthorityDenied("policy descendant parent unavailable")
            parent = parent_result[0]
            if caller_lineage != parent.target_publisher_lineage:
                raise AuthorityDenied("native finite caller publisher lineage changed")
        elif caller.principal_id != root.command.context.caller_principal.value:
            raise AuthorityDenied("immediate policy caller is not the native captured caller")
        return caller, capture, provider, caller_lineage

    def _validate_policy_parent(
        self, context: InvocationContext, caller: FunctionPrincipal
    ) -> None:
        """Authenticate exact finite descendant chain from actual dispatched parent."""
        if not context.call_chain:
            if context.parent_lease_id is not None:
                raise AuthorityDenied("policy parent supplied without ancestry")
            return
        if not context.parent_lease_id:
            raise AuthorityDenied("policy descendant has no authenticated parent")
        result = self.store.get_lease(context.parent_lease_id)
        if result is None:
            raise AuthorityDenied("policy descendant parent unavailable")
        parent, state = result
        from core_runtime.authority.v4 import LeaseState

        if (
            state is not LeaseState.DISPATCHED
            or parent.target != caller
            or parent.call_chain + (parent.caller.principal_id,) != context.call_chain
            or parent.expires_at <= self._clock()
        ):
            raise AuthorityDenied("policy descendant ancestry changed")
        for field_name in (
            "profile_id",
            "activation_id",
            "activation_digest",
            "plan_digest",
            "profile_authority_digest",
            "security_epoch",
            "fencing_token",
        ):
            if getattr(parent, field_name) != getattr(context, field_name):
                raise AuthorityDenied("policy parent activation changed")
        for kind, identifier in (
            ("grant", parent.grant_id),
            ("provider_authority", parent.provider_authority_id),
            ("host_extension", parent.host_extension_id),
            ("function_principal", parent.caller.principal_id),
            ("function_principal", parent.target.principal_id),
            ("execution_domain", parent.caller_domain_id),
            ("execution_domain", parent.target_domain_id),
        ):
            if self.store.is_revoked(kind, identifier):
                raise AuthorityDenied("policy descendant parent revoked")

    def derive_exact_operation_grant(
        self, selection_id: str, operation: RetainedExactOperation
    ) -> GrantRecord:
        """Consume fresh bound reviewer evidence and mint one exact tagged grant."""
        if self._emergency_stop:
            raise AuthorityDenied("authority kernel is emergency-fenced")
        root, mode = self._root(selection_id)
        caller, capture, provider, caller_lineage = self._validate_operation(root, operation)
        if (
            "authorization_kind" in operation.scope.dimensions
            or "policy_derivation_id" in operation.scope.dimensions
        ):
            raise AuthorityDenied("Host settlement scope markers already present")
        locator = selection_id.removeprefix("action-approval-policy-selection-v1-")
        grant_id = PREFIX + locator + "-" + operation.digest.removeprefix("sha256:")
        delegated_scope = replace(
            operation.scope,
            dimensions={
                **operation.scope.dimensions,
                "authorization_kind": (KIND,),
                "policy_derivation_id": (grant_id,),
            },
        )
        operation = replace(
            operation,
            scope=delegated_scope,
            context=replace(operation.context, effect_digest=delegated_scope.digest),
        )
        self._validate_operation(root, operation)
        operation_digest = operation.digest
        record = self.store.get_host_pending_effect(selection_id)
        if record is None or record[1].get("record_kind") != "action_approval_policy_selection_v1":
            raise AuthorityDenied("native selection capture is unavailable")
        revision, payload = record
        entries = dict(payload.get("policy_derived_grants", {}))
        if grant_id in entries:
            raise AuthorityDenied("exact policy settlement replayed")
        evidence = None
        if mode == "agent":
            evidence = root.review_exact(operation)
            if (
                not isinstance(evidence, CapturedReviewEvidence)
                or evidence.operation_capture_digest != operation_digest
                or evidence.reviewer_boundary_digest != authority_digest(capture["reviewer"])
                or evidence.expires_at <= self._clock()
                or any(
                    item.get("review_evidence_id") == evidence.evidence_id
                    for item in entries.values()
                )
            ):
                raise AuthorityDenied("agent review is denied, stale, changed or replayed")
            root.validate_review_evidence(evidence, operation)
            transport = evidence.provenance.get("transport_proof", {})
            lease_id = transport.get("lease_id") if isinstance(transport, Mapping) else None
            transport_digest = evidence.provenance.get("transport_request_digest") or (
                transport.get("request_digest") if isinstance(transport, Mapping) else None
            )
            if lease_id and any(
                item.get("review_transport_lease_id") == lease_id for item in entries.values()
            ):
                raise AuthorityDenied("review transport was already consumed")
            if transport_digest and any(
                item.get("review_transport_request_digest") == transport_digest
                for item in entries.values()
            ):
                raise AuthorityDenied("review transport was already consumed")
            if evidence.outcome == "danger":
                from tobkiri_host.policy_review_errors import AuthenticatedPolicyReviewDenied

                raise AuthenticatedPolicyReviewDenied(
                    evidence.provenance.get("reviewer_reason", "")
                )
            if evidence.outcome != "safe":
                raise AuthorityDenied("reviewer result is unavailable")
        expires = min(
            capture["expires_at"],
            evidence.expires_at if evidence else capture["expires_at"],
            provider.expires_at or capture["expires_at"],
        )
        grant = GrantRecord(
            grant_id=grant_id,
            caller=caller,
            target=operation.context.target,
            profile_id=operation.context.profile_id,
            activation_id=operation.context.activation_id,
            profile_authority_digest=operation.context.profile_authority_digest,
            caller_publisher_lineage=caller_lineage,
            target_publisher_lineage=provider.publisher_lineage,
            scope=operation.scope,
            lifetime=GrantLifetime.ONE_SHOT,
            security_epoch=operation.context.security_epoch,
            approval_id=None,
            issued_at=self._clock(),
            expires_at=expires,
            max_uses=1,
            session_id=operation.context.caller_session_id,
        )
        root_receipt = root.receipt_record()
        native_result = self.store.get_lease(root_receipt[1]["committed_selection_lease"])
        native_decision = self.store.get_interactive_approval_decision(
            root.command.context.request_id
        )
        if native_result is None or native_decision is None:
            raise AuthorityDenied("native policy selection authority unavailable")
        native_lease, _ = native_result
        entries[grant_id] = {
            "authorization_kind": KIND,
            "grant_digest": grant.digest,
            "operation_capture_digest": operation_digest,
            "root_native_request": root.command.context.request_id,
            "root_receipt_digest": root.receipt_digest(),
            "review_evidence_id": evidence.evidence_id if evidence else None,
            "review_transport_lease_id": lease_id if evidence else None,
            "review_transport_request_digest": transport_digest if evidence else None,
            "reviewer_boundary_digest": authority_digest(capture["reviewer"]),
            "state": "settled",
            "root_receipt_id": root_receipt[1]["effect_id"],
            "root_expires_at": capture["expires_at"],
            "root_revocation_targets": [
                [kind, identifier]
                for kind, identifier in (
                    ("grant", native_decision.grant_id),
                    ("function_principal", native_lease.caller.principal_id),
                    ("function_principal", native_lease.target.principal_id),
                    ("execution_domain", native_lease.caller_domain_id),
                    ("execution_domain", native_lease.target_domain_id),
                    ("pack_artifact", native_lease.caller.parent_artifact_digest),
                    ("pack_artifact", native_lease.target.parent_artifact_digest),
                    ("profile", native_lease.profile_id),
                    ("activation", native_lease.activation_id),
                    ("provider_authority", native_lease.provider_authority_id),
                    ("host_extension", native_lease.host_extension_id),
                    ("publisher", native_lease.caller_publisher_lineage),
                    ("publisher", native_lease.target_publisher_lineage),
                )
            ],
            "retained_operation": {
                "prepared_snapshot": dict(operation.prepared_snapshot),
                "context": {
                    **asdict(operation.context),
                    "target": operation.context.target.to_dict(),
                },
                "scope": operation.scope.to_dict(),
                "contract": operation.contract,
                "operation": operation.operation,
                "operation_class": operation.operation_class,
                "workspace_root": operation.workspace_root,
            },
            "review_evidence_provenance": dict(evidence.provenance) if evidence else None,
            "review_evidence_expires_at": evidence.expires_at if evidence else None,
            "parent_lease_id": operation.context.parent_lease_id,
        }
        effects = operation.scope.dimensions.get("pending_effect_id", ())
        if effects:
            if len(effects) != 1:
                raise AuthorityDenied("policy effect reference is not finite")
            entries[grant_id]["pending_effect_id"] = effects[0]
        self.store.commit_policy_derived_grant(
            grant=grant,
            selection_id=selection_id,
            expected_revision=revision,
            payload={**payload, "policy_derived_grants": entries},
        )
        root.retain_operation(grant_id, operation)
        return grant

    def _derived(
        self, grant: GrantRecord, context: InvocationContext, scope: AuthorityScope
    ) -> None:
        matches = []
        for selection_id, root in self.policy_roots.items():
            record = self.store.get_host_pending_effect(selection_id)
            entry = (
                record[1].get("policy_derived_grants", {}).get(grant.grant_id) if record else None
            )
            if entry:
                matches.append((selection_id, root, entry))
        if len(matches) != 1:
            raise AuthorityDenied("policy grant provenance missing or ambiguous")
        selection_id, root, entry = matches[0]
        _, mode = self._root(selection_id)
        operation = root.retained_operation(grant.grant_id)
        self._validate_operation(root, operation)
        if mode == "agent":
            expires_at = entry.get("review_evidence_expires_at")
            if not isinstance(expires_at, (int, float)) or expires_at <= self._clock():
                raise AuthorityDenied("derived agent review expired")
            evidence = CapturedReviewEvidence(
                entry["review_evidence_id"],
                operation.digest,
                entry["reviewer_boundary_digest"],
                expires_at,
                "safe",
                entry.get("review_evidence_provenance") or {},
            )
            root.validate_review_evidence(evidence, operation)
        if (
            entry.get("authorization_kind") != KIND
            or entry.get("state") != "settled"
            or entry.get("grant_digest") != grant.digest
            or entry.get("operation_capture_digest") != operation.digest
            or entry.get("root_receipt_digest") != root.receipt_digest()
            or entry.get("root_native_request") != root.command.context.request_id
            or operation.context != context
            or operation.scope != scope
            or grant.approval_id is not None
            or grant.lifetime is not GrantLifetime.ONE_SHOT
            or grant.expires_at is None
            or grant.expires_at <= self._clock()
            or grant.security_epoch != self.store.security_epoch
            or self.store.is_revoked("grant", grant.grant_id)
        ):
            raise AuthorityDenied("policy grant exact provenance changed")

    def _select_grant(
        self,
        *,
        context: InvocationContext,
        caller: FunctionPrincipal,
        request_scope: AuthorityScope,
        now: float,
    ) -> GrantRecord:
        kind = request_scope.dimensions.get("authorization_kind")
        if kind is not None:
            identifiers = request_scope.dimensions.get("policy_derivation_id", ())
            if kind != (KIND,) or len(identifiers) != 1:
                raise AuthorityDenied("policy authorization-kind marker is invalid")
            grant = self.store.get_grant(identifiers[0])
            if grant is None or grant.caller != caller or grant.target != context.target:
                raise AuthorityDenied("exact policy authorization reference unavailable")
            self._derived(grant, context, request_scope)
            return grant
        ordinary = None
        try:
            ordinary = super()._select_grant(
                context=context, caller=caller, request_scope=request_scope, now=now
            )
        except AuthorityDenied:
            # Never mask ordinary ambiguity: count all applicable non-derived grants.
            candidates = [
                g
                for g in self.store.list_grants()
                if not g.grant_id.startswith(PREFIX)
                and g.caller == caller
                and g.target == context.target
                and not g.revoked
                and not self.store.is_revoked("grant", g.grant_id)
                and g.issued_at <= now
                and (g.expires_at is None or now < g.expires_at)
                and request_scope.is_subset_of(g.scope)
            ]
            if candidates:
                raise
        derived = [
            g
            for g in self.store.list_grants()
            if g.grant_id.startswith(PREFIX)
            and g.caller == caller
            and g.target == context.target
            and g.scope == request_scope
            and g.scope.exact_request_digest == context.request_digest
        ]
        if ordinary is not None:
            if derived:
                raise AuthorityDenied("ordinary and derived exact grant are ambiguous")
            return ordinary
        if len(derived) != 1:
            raise AuthorityDenied("exact policy-derived grant missing or ambiguous")
        self._derived(derived[0], context, request_scope)
        return derived[0]

    def _validate_call_chain(
        self,
        context: InvocationContext,
        caller: FunctionPrincipal,
        grant: GrantRecord,
        request_scope: AuthorityScope,
    ) -> None:
        """Derived child authority comes from native finite scope plus exact ancestry."""
        if grant.grant_id.startswith(PREFIX):
            self._validate_policy_parent(context, caller)
            return
        super()._validate_call_chain(context, caller, grant, request_scope)

    def dispatch(self, lease_token: str, **kwargs: Any) -> Any:
        """Repeat native receipt and current jail/binding checks before effects."""
        lease, _ = self.store.inspect_lease_token(lease_token)
        if lease.grant_id.startswith(PREFIX):
            grant = self.store.get_grant(lease.grant_id)
            if grant is None:
                raise AuthorityDenied("derived dispatch grant is unavailable")
            root = next(
                (root for root in self.policy_roots.values() if root.has_operation(grant.grant_id)),
                None,
            )
            if root is None:
                raise AuthorityDenied("derived retained snapshot unavailable")
            operation = root.retained_operation(grant.grant_id)
            self._derived(grant, operation.context, lease.authorized_scope)
        return super().dispatch(lease_token, **kwargs)
