"""Host-only committed selection receipts; no future tool-grant delegation."""

from __future__ import annotations

from typing import Any, Mapping

from tobkiri_host.action_approval_policy import ActionApprovalPolicyController
from core_runtime.authority.v4 import AuthorityDenied, GrantLifetime, LeaseState


class CommittedSelectionReceiptController:
    """Authenticate native consent and actual consumed selection-effect success."""

    def __init__(self, policy: ActionApprovalPolicyController) -> None:
        self.policy = policy
        self.store = policy.authority_store

    def _proof(self, command: Any, lease_id: str) -> None:
        request = self.store.get_interactive_approval_request(command.context.request_id)
        decision = self.store.get_interactive_approval_decision(command.context.request_id)
        if (
            request is None
            or decision is None
            or decision.decision != "approved"
            or not decision.ui_operator_digest
            or decision.request_snapshot_digest != request.digest
            or request.expires_at <= self.policy.clock()
            or request.security_epoch != self.store.security_epoch
        ):
            raise AuthorityDenied("selected policy native proof is invalid or expired")
        approval = self.store.get_approval(decision.approval_id or "")
        grant = self.store.get_grant(decision.grant_id or "")
        leased = self.store.get_lease(lease_id)
        if approval is None or grant is None or leased is None:
            raise AuthorityDenied("selected policy durable proof is incomplete")
        lease, state = leased
        if (
            state is not LeaseState.COMMITTED
            or lease.grant_id != grant.grant_id
            or lease.request_id != request.request_id
            or lease.request_digest != request.request_digest
            or lease.authorized_scope != request.base_scope
            or lease.caller != request.caller
            or lease.target != request.target
            or lease.caller_domain_id != request.caller_domain_id
            or lease.caller_boot_epoch != request.caller_boot_epoch
            or lease.target_domain_id != request.target_domain_id
            or lease.target_boot_epoch != request.target_boot_epoch
            or grant.scope != request.base_scope
            or grant.approval_id != approval.approval_id
            or grant.caller != request.caller
            or grant.target != request.target
            or grant.profile_id != request.profile_id
            or grant.activation_id != request.activation_id
            or grant.profile_authority_digest != request.profile_authority_digest
            or grant.security_epoch != request.security_epoch
            or grant.session_id != request.caller_session_id
            or grant.lifetime is not GrantLifetime.ONE_SHOT
            or grant.max_uses != 1
            or grant.expires_at != request.expires_at
            or grant.revoked
            or approval.snapshot_digest != request.digest
            or approval.actor_id != decision.actor_id
            or approval.decision != "approved"
            or approval.decided_at != decision.decided_at
            or approval.effect_bundle_digest != request.base_scope.digest
            or approval.caller != request.caller
            or approval.target != request.target
            or approval.profile_id != request.profile_id
            or approval.security_epoch != request.security_epoch
            or self.store.grant_usage(grant.grant_id) != (0, 1)
        ):
            raise AuthorityDenied("selected policy execution proof is invalid")
        for field in (
            "profile_id",
            "activation_id",
            "activation_digest",
            "plan_digest",
            "profile_authority_digest",
            "fencing_token",
            "security_epoch",
        ):
            if getattr(lease, field) != getattr(request, field):
                raise AuthorityDenied("selected policy lease capture changed")
        for kind, identifier in (
            ("grant", grant.grant_id),
            ("profile", request.profile_id),
            ("activation", request.activation_id),
            ("function_principal", request.caller.principal_id),
            ("function_principal", request.target.principal_id),
            ("provider_authority", lease.provider_authority_id),
            ("host_extension", lease.host_extension_id),
        ):
            if self.store.is_revoked(kind, identifier):
                raise AuthorityDenied("selected policy authority was revoked")
        for session, principal, domain_id, boot_epoch in (
            (
                request.caller_session_id,
                request.caller.principal_id,
                request.caller_domain_id,
                request.caller_boot_epoch,
            ),
            (
                request.presentation_owner_session_id,
                request.presentation_owner_principal_id,
                None,
                None,
            ),
        ):
            domain, actual = self.store.resolve_authenticated_session(session)
            if (
                actual != principal
                or domain.profile_id != request.profile_id
                or domain.activation_id != request.activation_id
                or domain.security_epoch != request.security_epoch
                or domain.fencing_token != request.fencing_token
                or (domain_id is not None and domain.domain_id != domain_id)
                or (boot_epoch is not None and domain.boot_epoch != boot_epoch)
            ):
                raise AuthorityDenied("selected policy owner session is stale")
        target = self.store.get_domain(request.target_domain_id)
        if (
            target is None
            or target.state.value != "active"
            or target.boot_epoch != request.target_boot_epoch
            or target.profile_id != request.profile_id
            or target.activation_id != request.activation_id
            or target.security_epoch != request.security_epoch
            or target.fencing_token != request.fencing_token
            or request.target.principal_id not in target.principal_ids
        ):
            raise AuthorityDenied("selected policy target is stale")
        # Reauthenticate the full expected immutable capture, including revision.
        if (
            request.request_digest != command.request_digest
            or request.base_scope.to_dict() != dict(command.base_scope)
            or request.profile_revision != command.context.profile_revision
        ):
            raise AuthorityDenied("selected policy request binding changed")

    def commit_after_execution(self, key: str, command: Any, lease_id: str) -> None:
        """Persist once only after authenticated Broker execution has committed."""
        self._proof(command, lease_id)
        capture = self.policy._validated_capture(command)
        if capture["key"] != key:
            raise AuthorityDenied("selected policy receipt key changed")
        record = self.policy.store.read(key)
        expected_revision = int(capture["expected_revision"]) + 1
        if (
            record is None
            or record[0] != expected_revision
            or record[1].get("state") != "native_approved"
            or record[1].get("capture") != capture
            or record[1].get("root_native_request") != command.context.request_id
            or record[1].get("mode") != capture["mode"]
            or record[1].get("committed_selection_lease") is not None
        ):
            raise AuthorityDenied("selected policy receipt is unavailable or replayed")
        self.policy.store.commit(
            key,
            expected_revision,
            {
                "mode": capture["mode"],
                "state": "committed_selection",
                "capture": capture,
                "root_native_request": command.context.request_id,
                "committed_selection_lease": lease_id,
            },
        )

    def resolve(self, key: str, command: Any, **expected: Any) -> str:
        """Return default ask only if never selected; invalid selection raises."""
        record = self.policy.store.read(key)
        if record is None or (
            record[1].get("mode") == "ask" and not record[1].get("committed_selection_lease")
        ):
            return "ask"
        receipt = record[1]
        if receipt.get("state") != "committed_selection":
            raise AuthorityDenied("selected policy has no committed receipt")
        capture = self.policy._validated_capture(command)
        live = self.policy._capture(command, **expected)
        live.update({"key": key, "expected_revision": capture["expected_revision"]})
        if (
            capture != live
            or receipt.get("capture") != live
            or receipt.get("root_native_request") != command.context.request_id
        ):
            raise AuthorityDenied("selected policy receipt capture changed")
        self._proof(command, str(receipt["committed_selection_lease"]))
        return str(receipt["mode"])

    def cancel(self, key: str) -> None:
        """Retain selected state provenance but make all later resolution fail."""
        record = self.policy.store.read(key)
        if record is not None:
            self.policy.store.commit(key, record[0], {**record[1], "state": "cancelled"})
