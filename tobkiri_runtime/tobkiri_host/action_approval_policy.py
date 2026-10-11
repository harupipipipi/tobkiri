"""Experimental Host policy controller using real v4 durable native decisions."""

from __future__ import annotations

import json
import hashlib
import time
from collections.abc import Callable
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any, Mapping

from core_runtime.authority.v4 import AuthorityDenied, AuthorityScope, authority_digest
from core_runtime.authority.v4_store import AuthorityStore
from tobkiri_host.authority_v4 import AuthorityV4Adapter
from tobkiri_host.ports import (
    InteractiveApprovalDecisionCommand,
    InteractiveApprovalGrantAttestation,
    InteractiveApprovalRequestCommand,
)


class ActionApprovalPolicyStore:
    """Persist receipts in the encrypted Host pending-effect store, with CAS."""

    def __init__(self, authority: AuthorityV4Adapter) -> None:
        self.authority = authority

    def read(self, key: str) -> tuple[int, Mapping[str, object]] | None:
        """Read authenticated Host-owned policy state."""
        return self.authority.get_host_pending_effect(self.physical_id(key))

    @staticmethod
    def physical_id(key: str) -> str:
        """Keep receipt rows in reserved typed namespaces, never effect IDs."""
        prefix = (
            "action-approval-policy-selection-v1-"
            if key.startswith("policy-selection-")
            else "action-approval-policy-v1-"
        )
        return prefix + hashlib.sha256(key.encode("utf-8")).hexdigest()

    def initialize(self, key: str) -> None:
        """Create default ask state once; an existing record remains intact."""
        if self.read(key) is None:
            identifier = self.physical_id(key)
            self.authority.create_host_pending_effect(
                identifier,
                {
                    "effect_id": identifier,
                    "state": "policy",
                    "mode": "ask",
                    "record_kind": "action_approval_policy_v1",
                },
            )

    def commit(self, key: str, revision: int, receipt: Mapping[str, object]) -> None:
        """Commit a selection only against the revision confirmed by the user."""
        self.authority.compare_and_swap_host_pending_effect(
            self.physical_id(key),
            expected_revision=revision,
            payload={
                **receipt,
                "effect_id": self.physical_id(key),
                "state": receipt.get("state", "policy"),
                "record_kind": "action_approval_policy_v1",
            },
        )


class ActionApprovalPolicyController:
    """Accept elevation only through the actual signed native approve port.

    Commands and workspace roots must be constructed by Host composition, never
    deserialized directly from browser or guest inputs. The policy capability is
    deliberately separate from all executable tool capabilities.
    """

    CAPABILITY = "operation.invoke"

    def __init__(
        self,
        authority: AuthorityV4Adapter,
        authority_store: AuthorityStore,
        *,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.authority = authority
        self.authority_store = authority_store
        self.clock = clock
        self.store = ActionApprovalPolicyStore(authority)

    def _capture(
        self,
        command: InteractiveApprovalRequestCommand,
        *,
        mode: str,
        reviewer: Mapping[str, Any],
        workspace: str,
        conversation: str,
        turn: str,
    ) -> dict[str, Any]:
        if mode not in {"ask", "agent", "full"}:
            raise AuthorityDenied("invalid action approval mode")
        root = Path(workspace)
        if not root.is_absolute() or "*" in workspace or root.resolve() != root:
            raise AuthorityDenied("workspace scope must be a canonical absolute root")
        if not conversation or not turn or (mode == "agent" and not reviewer):
            raise AuthorityDenied("policy capture is incomplete")
        context = json.loads(json.dumps(asdict(command.context)))
        context.pop("request_id")
        return {
            "mode": mode,
            "reviewer": json.loads(json.dumps(dict(reviewer))),
            "workspace": workspace,
            "conversation": conversation,
            "turn": turn,
            "context": context,
            "owner_principal": command.presentation_owner_principal_id,
            "owner_session": command.presentation_owner_session_id,
            "target": command.target_principal.value,
            "invocation_owner": command.invocation_owner_id,
            "caller_lineage": command.caller_publisher_lineage,
            "target_lineage": command.target_publisher_lineage,
            "expires_at": command.expires_at,
        }

    def request(
        self,
        key: str,
        template: InteractiveApprovalRequestCommand,
        *,
        mode: str,
        reviewer: Mapping[str, Any],
        workspace: str,
        conversation: str,
        turn: str,
    ) -> InteractiveApprovalRequestCommand:
        """Create an immutable selection request and return its Host command."""
        if not self.clock() < template.expires_at <= self.clock() + 900:
            raise AuthorityDenied("policy selection expiry must be bounded")
        self.store.initialize(key)
        record = self.store.read(key)
        if record is None:
            raise AuthorityDenied("policy state is unavailable after initialization")
        revision, _ = record
        capture = self._capture(
            template,
            mode=mode,
            reviewer=reviewer,
            workspace=workspace,
            conversation=conversation,
            turn=turn,
        )
        capture.update({"key": key, "expected_revision": revision})
        capture_digest = authority_digest(capture)
        digest = self._native_request_digest(capture_digest)
        scope = self._selection_scope(template, capture, digest)
        phrase = f"SELECT {mode.upper()}"
        command = replace(
            template,
            request_digest=digest,
            base_scope=scope.to_dict(),
            redacted_metadata={
                "action": "approval_mode_selection",
                "summary": f"Select {mode} for this turn",
                "confirmation_phrase": phrase,
                "policy_capture_digest": capture_digest,
                "root_expires_at": str(template.expires_at),
                "mode": mode,
                "workspace_root": workspace,
                "conversation_id": conversation,
                "turn_id": turn,
                "reviewer_config_digest": authority_digest(dict(reviewer)),
                "reviewer_model": str(reviewer.get("model") or "none"),
                **self._native_boundary_metadata(capture),
            },
            typed_confirmation_phrase=phrase,
        )
        self.authority.request_interactive_approval(command)
        selection_key = "policy-selection-" + command.context.request_id
        selection_id = self.store.physical_id(selection_key)
        self.authority.create_host_pending_effect(
            selection_id,
            {
                "effect_id": selection_id,
                "state": "policy_selection",
                "capture": capture,
                "retained_native_command": self._retained_native_command(command),
                "retained_native_prepared": (
                    self.prepared.to_snapshot().to_dict() if hasattr(self, "prepared") else None
                ),
                "record_kind": "action_approval_policy_selection_v1",
            },
        )
        return command

    def _selection_scope(
        self, template: Any, capture: Mapping[str, Any], digest: str
    ) -> AuthorityScope:
        """Require the catalog-bound subclass; custom capability minting is forbidden."""
        raise AuthorityDenied("a signed finite policy selection route is required")

    def _native_request_digest(self, capture_digest: str) -> str:
        """Prototype uses capture; route-bound controller pins Broker digest."""
        return capture_digest

    def cancel(self, key: str) -> None:
        """Invalidate a cancelled turn's policy receipt through Host-owned CAS."""
        record = self.store.read(key)
        if record is not None:
            self.store.commit(key, record[0], {**record[1], "state": "cancelled"})

    def consume_signed_native_selection(
        self,
        command: InteractiveApprovalRequestCommand,
        decision: InteractiveApprovalDecisionCommand,
    ) -> None:
        """Verify native proof through AuthorityV4Adapter before persisting selection."""
        if decision.request_id != command.context.request_id:
            raise AuthorityDenied("policy decision does not match request")
        self.authority.approve_interactive_approval(decision)
        self.record_native_approved_selection(command)

    def resolve(self, key: str, command: Any, **expected: Any) -> str:
        """Executable modes require consumed committed selection receipt proof."""
        raise AuthorityDenied("use CommittedSelectionReceiptController for executable modes")

    def _validated_capture(self, command: InteractiveApprovalRequestCommand) -> dict[str, Any]:
        record = self.store.read("policy-selection-" + command.context.request_id)
        if record is None:
            raise AuthorityDenied("policy selection is unavailable")
        stored_capture = record[1]["capture"]
        if not isinstance(stored_capture, Mapping):
            raise AuthorityDenied("policy selection capture is malformed")
        capture = dict(stored_capture)
        scope = AuthorityScope.from_dict(command.base_scope)
        if (
            authority_digest(capture) != command.redacted_metadata["policy_capture_digest"]
            or scope.capability != self.CAPABILITY
            or scope.exact_request_digest != command.request_digest
        ):
            raise AuthorityDenied("invalid policy selection request")
        return capture

    def _assert(self, command: InteractiveApprovalRequestCommand) -> None:
        decision = self.authority_store.get_interactive_approval_decision(
            command.context.request_id
        )
        if decision is None or not decision.ui_operator_digest:
            raise AuthorityDenied("native policy decision is unavailable")
        approval = self.authority_store.get_approval(decision.approval_id or "")
        if (
            approval is None
            or approval.snapshot_digest != decision.request_snapshot_digest
            or approval.actor_id != decision.actor_id
            or approval.decision != "approved"
            or approval.security_epoch != command.context.security_epoch
            or self.authority_store.is_revoked("grant", decision.grant_id or "")
        ):
            raise AuthorityDenied("native policy approval has been revoked")
        self.authority.assert_interactive_approval_grant(
            InteractiveApprovalGrantAttestation(
                request_id=command.context.request_id,
                context=command.context,
                target_principal=command.target_principal,
                request_digest=command.request_digest,
                base_scope=command.base_scope,
                invocation_owner_id=command.invocation_owner_id,
                caller_publisher_lineage=command.caller_publisher_lineage,
                target_publisher_lineage=command.target_publisher_lineage,
                expires_at=command.expires_at,
            )
        )

    @staticmethod
    def _retained_native_command(command: Any) -> dict[str, Any]:
        """Retain only Host-created command inputs inside authenticated encryption."""
        from tobkiri_host.interactive_effects import _context_to_dict

        value = asdict(command)
        value["context"] = _context_to_dict(command.context)
        value["target_principal"] = command.target_principal.value
        return value

    def _native_boundary_metadata(self, capture: Mapping[str, Any]) -> dict[str, Any]:
        """Allow bounded selection subclasses to expose captured native scope."""
        return {}

    def record_native_approved_selection(self, command: InteractiveApprovalRequestCommand) -> None:
        """Record an actual native-window decision without replaying its UI proof."""
        capture = self._validated_capture(command)
        self._assert(command)
        existing = self.store.read(str(capture["key"]))
        if (
            existing is not None
            and existing[0] == int(capture["expected_revision"]) + 1
            and existing[1].get("state") == "native_approved"
            and existing[1].get("capture") == capture
            and existing[1].get("root_native_request") == command.context.request_id
        ):
            return
        self.store.commit(
            str(capture["key"]),
            int(capture["expected_revision"]),
            {
                "mode": capture["mode"],
                "state": "native_approved",
                "root_native_request": command.context.request_id,
                "capture": capture,
                "request_snapshot_digest": self.authority.interactive_approval_status(
                    command.context.request_id
                ).request_snapshot_digest,
            },
        )
