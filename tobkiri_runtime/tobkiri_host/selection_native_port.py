"""TEMP selection-aware native port; ordinary file approvals delegate unchanged."""

from __future__ import annotations

from typing import Any

from tobkiri_host.action_approval_policy import ActionApprovalPolicyController
from tobkiri_host.models import OpaqueAuthorityRef, RequestContext
from tobkiri_host.ports import InteractiveApprovalRequestCommand


class SelectionAwareNativeApprovalPort:
    """Route only encrypted Host selection records to the policy controller.

    Install this narrow port into HostProviderCaptureContextV4, not a browser
    API. Existing signed get/approve/deny operation contracts remain the only
    native decision surface. The controller calls the undecorated adapter.
    """

    def __init__(self, controller: ActionApprovalPolicyController) -> None:
        self.controller = controller
        self.authority = controller.authority

    def _selection(self, request_id: str) -> InteractiveApprovalRequestCommand | None:
        record = self.controller.store.read("policy-selection-" + request_id)
        if record is None:
            return None
        capture = record[1]["capture"]
        request = self.controller.authority_store.get_interactive_approval_request(request_id)
        if request is None:
            raise PermissionError("selection native request is unavailable")
        context = dict(capture["context"])
        context["request_id"] = request_id
        context["caller_principal"] = OpaqueAuthorityRef(context["caller_principal"]["value"])
        context["delegation_chain"] = tuple(
            OpaqueAuthorityRef(item["value"]) for item in context["delegation_chain"]
        )
        return InteractiveApprovalRequestCommand(
            context=RequestContext(**context),
            target_principal=OpaqueAuthorityRef(capture["target"]),
            request_digest=request.request_digest,
            base_scope=request.base_scope.to_dict(),
            invocation_owner_id=request.invocation_owner_id,
            presentation_owner_principal_id=request.presentation_owner_principal_id,
            presentation_owner_session_id=request.presentation_owner_session_id,
            caller_publisher_lineage=request.caller_publisher_lineage,
            target_publisher_lineage=request.target_publisher_lineage,
            expires_at=request.expires_at,
            redacted_metadata=request.redacted_metadata,
        )

    def approve_interactive_approval(self, decision: Any) -> Any:
        """Use exact native proof to commit policy, or delegate ordinary approval."""
        selection = self._selection(decision.request_id)
        if selection is None:
            return self.authority.approve_interactive_approval(decision)
        self.controller.consume_signed_native_selection(selection, decision)
        return self.authority.interactive_approval_status(decision.request_id)

    def deny_interactive_approval(self, decision: Any) -> Any:
        """Native deny settles without changing an existing policy receipt."""
        return self.authority.deny_interactive_approval(decision)

    def get_interactive_approval(self, query: Any) -> Any:
        """Preserve existing authenticated owner presentation checks."""
        return self.authority.get_interactive_approval(query)

    def list_interactive_approvals(self, query: Any) -> Any:
        """Preserve owner-filtered redacted listing."""
        return self.authority.list_interactive_approvals(query)

    def request_interactive_approval(self, command: Any) -> Any:
        """Preserve existing exact file-operation approval request creation."""
        return self.authority.request_interactive_approval(command)

    def interactive_approval_status(self, request_id: str) -> Any:
        """Preserve redacted lifecycle projection."""
        return self.authority.interactive_approval_status(request_id)

    def assert_interactive_approval_grant(self, attestation: Any) -> None:
        """Preserve exact underlying authority assertion."""
        self.authority.assert_interactive_approval_grant(attestation)
