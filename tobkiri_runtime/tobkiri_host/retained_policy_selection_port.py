"""Narrow retained selection staging and exact authenticated completion."""

from typing import Any, Callable

from tobkiri_host.committed_selection_receipt import CommittedSelectionReceiptController
from core_runtime.authority.v4 import AuthorityDenied, AuthorityScope, LeaseState


class RetainedPolicySelectionPort:
    """Bind one prepared selection; never choose a lease from audit ordering."""

    def __init__(self, policy: Any, command: Any, *, guard: Callable[[], None]) -> None:
        self.policy, self.command, self.guard = policy, command, guard
        self.selection_id = policy.prepared.normalized_payload["selection_id"]
        self.capture_key = "policy-selection-" + command.context.request_id

    def select_prepared(self, selection_id: str, invocation: Any) -> None:
        """Authenticate the exact dispatched native selection and retain lease ID."""
        self.guard()
        invocation.assert_current()
        envelope = invocation.envelope
        command = self.command
        capture = self.policy._validated_capture(command)
        if (
            selection_id != self.selection_id
            or dict(envelope.payload) != {"selection_id": self.selection_id}
            or envelope.request_digest != command.request_digest
            or envelope.context.request_id != command.context.request_id
            or envelope.target_principal != command.target_principal
            or envelope.context != command.context
            or invocation.presentation_owner_principal_id != capture["owner_principal"]
            or invocation.presentation_owner_session_id != capture["owner_session"]
        ):
            raise AuthorityDenied("retained policy selection invocation changed")
        lease, state = self.policy.authority_store.inspect_lease_token(
            envelope.lease.token.decode("ascii")
        )
        decision = self.policy.authority_store.get_interactive_approval_decision(
            command.context.request_id
        )
        if (
            decision is None
            or decision.decision != "approved"
            or not decision.ui_operator_digest
            or state is not LeaseState.DISPATCHED
            or lease.grant_id != decision.grant_id
            or lease.request_id != command.context.request_id
            or lease.request_digest != command.request_digest
            or lease.authorized_scope != AuthorityScope.from_dict(command.base_scope)
            or lease.target.principal_id != command.target_principal.value
        ):
            raise AuthorityDenied("retained native selection lease is invalid")
        record = self.policy.store.read(self.capture_key)
        if record is None or record[1].get("staged_lease_id") is not None:
            raise AuthorityDenied("retained policy selection was already staged")
        self.guard()
        # Capture record is a separate reserved namespace; policy revision stays
        # exactly bound to the human's native-approved receipt revision.
        physical = self.policy.store.physical_id(self.capture_key)
        self.policy.authority.compare_and_swap_host_pending_effect(
            physical,
            expected_revision=record[0],
            payload={
                **record[1],
                "staged_lease_id": lease.lease_id,
                "staged_lease_digest": lease.digest,
            },
        )

    def complete_after_broker(self) -> None:
        """Commit receipt from this selection's authenticated staged lease only."""
        self.guard()
        record = self.policy.store.read(self.capture_key)
        if record is None or not record[1].get("staged_lease_id"):
            raise AuthorityDenied("retained policy selection has no staged lease")
        lease_id = str(record[1]["staged_lease_id"])
        leased = self.policy.authority_store.get_lease(lease_id)
        if leased is None or leased[0].digest != record[1].get("staged_lease_digest"):
            raise AuthorityDenied("retained policy completion lease changed")
        capture = self.policy._validated_capture(self.command)
        CommittedSelectionReceiptController(self.policy).commit_after_execution(
            str(capture["key"]), self.command, lease_id
        )
