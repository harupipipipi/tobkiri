"""Compose actual first-tool native selection into a proof-bearing policy root."""

from __future__ import annotations

import time
import uuid
from typing import Any, Callable, Mapping

from tobkiri_host.models import InvocationFrame
from tobkiri_host.ports import InteractiveApprovalRequestCommand
from tobkiri_protocol.canonical import canonical_digest

from core_runtime.saved_tool_policy_selection_v4 import commit_native_policy_selection

CONTRACT = "tobkiri.action.host.approval-policy.v1"
OPERATION = "host.action_approval_policy.select"


class NativeSavedToolPolicyV4:
    """Build roots only after an actual signed route and native lease commit."""

    def __init__(
        self,
        *,
        broker: Any,
        authority: Any,
        store: Any,
        kernel: Any,
        catalog: Any,
        window: Any,
        selection_dispatch: Any,
        bind_nested: Callable[..., Any],
        finite_routes: Callable[[Any, Any], tuple],
        reviewer_capture: Callable[[Any, Any], Any],
        reviewer_adapter: Callable[[Any, Any], tuple[Any, Callable]],
        prepared_validator: Callable[[Any], None],
        current_guard: Callable[[Any, Mapping[str, Any]], None],
        saved_root_guard: Callable[[Any, Any, Mapping[str, Any]], None],
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.broker, self.authority, self.store = broker, authority, store
        self.kernel, self.catalog, self.window = kernel, catalog, window
        self.selection_dispatch = selection_dispatch
        self.bind_nested, self.finite_routes = bind_nested, finite_routes
        self.reviewer_capture = reviewer_capture
        self.reviewer_adapter = reviewer_adapter
        self.prepared_validator, self.current_guard = prepared_validator, current_guard
        self.saved_root_guard, self.clock = saved_root_guard, clock

    @staticmethod
    def receipt_key(context: Any) -> str:
        """Bind durable discovery to this actual saved occurrence and owner."""
        owner = canonical_digest(
            {
                "owner": context.root.caller_principal.value,
                "session": context.root.caller_session_id,
                "saved_request": context.root.request_id,
                "capture": context.capture_digest,
            }
        )
        return "saved-policy-root-" + canonical_digest(
            {
                "owner": owner,
                "conversation": context.conversation_id,
                "turn": context.turn_id,
                "mode": context.mode,
                "workspace": context.workspace_id,
            }
        ).removeprefix("sha256:")

    def restore(self, invocation: Any, context: Any) -> Any | None:
        """Rehydrate exact encrypted receipt; invalid prior state never recreates."""
        from tobkiri_host.action_approval_policy import ActionApprovalPolicyStore
        from tobkiri_host.policy_exact_authority_adapter import rehydrate_policy_root

        receipt_store = ActionApprovalPolicyStore(self.authority)
        record = receipt_store.read(self.receipt_key(context))
        if record is None:
            return None
        request_id = record[1].get("root_native_request")
        if not isinstance(request_id, str) or not request_id:
            raise PermissionError("native policy prior selection is incomplete")
        reviewer = self.reviewer_capture(invocation, context) if context.mode == "agent" else None
        if reviewer is not None:
            reviewer.refresh()
            reviewer.assert_current()
        review_port, review_verifier = (
            self.reviewer_adapter(reviewer, context) if reviewer else (None, None)
        )
        root = rehydrate_policy_root(
            selection_id=receipt_store.physical_id("policy-selection-" + request_id),
            authority=self.authority,
            authority_store=self.store,
            catalog=self.catalog,
            restore_prepared=self.broker.validate_prepared_snapshot,
            clock=self.clock,
            prepared_validator=self.prepared_validator,
            current_guard=lambda operation: self.current_guard(operation, root.capture()),
            saved_root_guard=self.saved_root_guard,
            reviewer=review_port,
            review_transport_verifier=review_verifier,
        )
        root.capture_current(invocation, context.root).assert_current()
        self.kernel.policy_roots[root.selection_id] = root
        return root

    def __call__(self, invocation: Any, context: Any) -> Any:
        """Native approve finite captured turn scope, consume it, then register root."""
        from tobkiri_host.policy_delegation_boundary import BoundedPolicySelectionController
        from tobkiri_host.policy_exact_authority_adapter import CommittedPolicyDerivationRoot
        from tobkiri_host.retained_policy_selection_port import RetainedPolicySelectionPort

        context.assert_current()
        reviewer = self.reviewer_capture(invocation, context) if context.mode == "agent" else None
        if reviewer is not None:
            reviewer.refresh()
            reviewer.assert_current()
        payload = {"selection_id": "host-policy-selection-" + uuid.uuid4().hex}
        nested = self.bind_nested(invocation, CONTRACT, OPERATION, payload)
        try:
            prepared = self.broker.prepare(
                InvocationFrame(
                    contract_id=CONTRACT,
                    version_range="==1.0.0",
                    operation_id=OPERATION,
                    payload=payload,
                ),
                nested.context,
            )
            policy = BoundedPolicySelectionController.create(
                authority=self.authority,
                authority_store=self.store,
                catalog=self.catalog,
                prepared=prepared,
                clock=self.clock,
                routes=self.finite_routes(invocation, context),
            )
            expected = {
                "mode": context.mode,
                "reviewer": dict(reviewer.native_facts) if reviewer else {},
                "workspace": str(context.workspace_binding.canonical_root),
                "conversation": context.conversation_id,
                "turn": context.turn_id,
            }
            owner = canonical_digest(
                {
                    "owner": context.root.caller_principal.value,
                    "session": context.root.caller_session_id,
                    "saved_request": context.root.request_id,
                    "capture": context.capture_digest,
                }
            )
            saved_envelope = context.saved_scope.envelope
            end = min(
                self.clock() + 300,
                self.clock()
                + max(
                    0,
                    saved_envelope.deadline_monotonic - time.monotonic(),
                ),
            )
            template = InteractiveApprovalRequestCommand(
                context=nested.context,
                target_principal=prepared.binding.principal_ref,
                request_digest=prepared.request_digest,
                base_scope=nested.ceiling,
                invocation_owner_id=owner,
                presentation_owner_principal_id=context.root.caller_principal.value,
                presentation_owner_session_id=context.root.caller_session_id,
                caller_publisher_lineage=nested.caller_publisher_lineage,
                target_publisher_lineage=prepared.binding.artifact.publisher_lineage,
                expires_at=end,
                redacted_metadata={},
            )
            key = self.receipt_key(context)
            command = policy.request(key, template, **expected)

            def guard() -> None:
                context.assert_current()
                if reviewer is not None:
                    reviewer.assert_current()

            retained = RetainedPolicySelectionPort(policy, command, guard=guard)
            commit_native_policy_selection(
                invocation=invocation,
                policy=policy,
                command=command,
                retained_port=retained,
                dispatch_port=self.selection_dispatch,
                broker=self.broker,
                authority=self.authority,
                window=self.window,
                assert_current=guard,
                cancellation_proof=nested.cancellation_proof,
                presentation_context=context.root,
                clock=self.clock,
            )
            review_port, review_verifier = (
                self.reviewer_adapter(reviewer, context) if reviewer else (None, None)
            )
            root = CommittedPolicyDerivationRoot(
                policy=policy,
                command=command,
                expected=expected,
                prepared_validator=self.prepared_validator,
                current_guard=lambda operation: self.current_guard(operation, root.capture()),
                saved_root_guard=self.saved_root_guard,
                reviewer=review_port,
                review_transport_verifier=review_verifier,
            )
            root.capture_current(invocation, context.root).assert_current()
            if root.selection_id in self.kernel.policy_roots:
                raise PermissionError("native selected root was already registered")
            self.kernel.policy_roots[root.selection_id] = root
            return root
        finally:
            nested.release()
