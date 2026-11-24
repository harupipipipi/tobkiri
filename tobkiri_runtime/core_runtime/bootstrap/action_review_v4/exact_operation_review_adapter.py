"""Bind one retained Broker operation to canonical reviewer transport evidence."""

from __future__ import annotations

import time
from typing import Any, Callable, Mapping

from core_runtime.authority.v4 import AuthorityDenied
from .canonical_reviewer_port import CanonicalReviewerGeneratePort, CapturedReviewerBoundary
from .host_operation_reviewer import HostOperationReviewer


class ExactOperationReviewAdapter:
    """Root-only retained-operation bridge; neither this bridge nor AI grants.

    The authenticated root constructs this adapter with the approved immutable
    reviewer boundary and its independent signed generate port. The evidence
    constructor is the kernel's CapturedReviewEvidence class. Transport
    authentication must independently read the native committed reviewer lease.
    """

    def __init__(
        self,
        port: CanonicalReviewerGeneratePort,
        boundary: CapturedReviewerBoundary,
        *,
        turn_id: str,
        evidence_factory: Callable[..., Any],
        authenticate_transport: Callable[[Mapping[str, Any]], None],
        timeout_seconds: float = 15,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._clock = clock
        self._port = port
        self._boundary = boundary
        self._turn_id = turn_id
        self._evidence_factory = evidence_factory
        self._authenticate = authenticate_transport
        self._reviewer = HostOperationReviewer(port, timeout_seconds=timeout_seconds)

    def review_exact(self, retained: Any) -> Any:
        """Return one-use recommendation evidence for the actual retained payload."""
        retained.assert_current()
        facts = self._boundary.snapshot()
        operation_digest = retained.digest
        snapshot = retained.prepared_snapshot
        arguments = snapshot.get("normalized_payload")
        if (
            not isinstance(arguments, Mapping)
            or snapshot.get("contract_id") != retained.contract
            or snapshot.get("operation_id") != retained.operation
            or not retained.workspace_root
            or not self._turn_id
        ):
            raise AuthorityDenied("retained reviewer operation snapshot is incomplete")
        arguments = review_arguments(retained.contract, retained.operation, arguments)
        # The jail root and quotas originate the prepared Host operation. Never
        # derive permitted destinations from the proposed payload's paths.
        review_scope = {
            "allowed_operations": [retained.operation],
            "allowed_roots": [retained.workspace_root],
            "scope_dimensions": {
                key: list(values)
                for key, values in retained.scope.dimensions.items()
                if key not in {"authorization_kind", "policy_derivation_id"}
            },
            "limits": {
                **dict(retained.scope.quotas),
                "operation_class": retained.operation_class,
                "contract_id": retained.contract,
            },
        }
        scope = {
            "profile_id": retained.context.profile_id,
            "operation_capture_digest": operation_digest,
            "reviewer_boundary_digest": self._boundary.boundary_digest,
        }

        def current() -> bool:
            try:
                retained.assert_current()
                self._boundary.snapshot()
                return retained.digest == operation_digest
            except Exception:
                return False

        verdict = self._reviewer.review(
            retained.operation,
            arguments,
            scope=scope,
            turn_id=self._turn_id,
            model_reference=facts["model_reference"],
            review_scope=review_scope,
            capture_is_current=current,
        )
        if (
            not current()
            or not verdict.matches(
                retained.operation,
                arguments,
                scope=scope,
                turn_id=self._turn_id,
                model_reference=facts["model_reference"],
                review_scope=review_scope,
            )
            or verdict.decision == "unavailable"
        ):
            raise AuthorityDenied("independent reviewer unavailable or retained capture changed")
        provenance = dict(self._port.consume_transport_evidence(verdict.provider_request_id))
        self._authenticate(provenance)
        if (
            not current()
            or provenance.get("reviewer_boundary_digest") != self._boundary.boundary_digest
            or provenance.get("model_id") != verdict.model_id
            or provenance.get("provider_instance_id") != verdict.provider_instance_id
            or provenance.get("expires_at", 0) <= self._clock()
        ):
            raise AuthorityDenied("reviewer transport provenance is stale or rebound")
        provenance.update(
            {
                "operation_capture_digest": operation_digest,
                "review_id": verdict.review_id,
                "reviewer_reason": verdict.reason,
                "arguments_digest": verdict.arguments_digest,
                "review_scope_digest": verdict.review_scope_digest,
            }
        )
        return self._evidence_factory(
            evidence_id=verdict.review_id,
            operation_capture_digest=operation_digest,
            reviewer_boundary_digest=self._boundary.boundary_digest,
            expires_at=provenance["expires_at"],
            outcome="safe" if verdict.decision == "approve" else "danger",
            provenance=provenance,
        )

    def __call__(self, retained: Any) -> Any:
        """Compatibility alias for the root's public review_exact operation."""
        return self.review_exact(retained)

    def validate_review_evidence(self, evidence: Any, retained: Any) -> None:
        """Reauthenticate backing authority without re-consuming settled proof."""
        retained.assert_current()
        facts = self._boundary.snapshot()
        provenance = evidence.provenance
        if (
            evidence.operation_capture_digest != retained.digest
            or provenance.get("operation_capture_digest") != retained.digest
            or provenance.get("review_id") != evidence.evidence_id
            or evidence.reviewer_boundary_digest != self._boundary.boundary_digest
            or provenance.get("reviewer_boundary_digest") != self._boundary.boundary_digest
            or evidence.expires_at != provenance.get("expires_at")
            or evidence.expires_at <= self._clock()
            or evidence.outcome not in {"safe", "danger"}
            or provenance.get("model_id") != facts["route_binding"]["model_id"]
            or provenance.get("provider_instance_id")
            != facts["route_binding"]["provider_instance_id"]
        ):
            raise AuthorityDenied("retained reviewer evidence is stale or rebound")
        self._authenticate(provenance)
        retained.assert_current()

    def close(self) -> None:
        """Interrupt pending local waits without claiming transport termination."""
        self._reviewer.close()


def review_arguments(
    contract: str, operation: str, arguments: Mapping[str, Any]
) -> Mapping[str, Any]:
    """Project only known Host-private file request keys from model presentation.

    The complete original prepared snapshot remains bound by retained.digest.
    Unknown fields are rejected, never silently removed from operation review.
    """
    if contract != "tobkiri.service.file.create.v1":
        return arguments
    if operation == "rumi_default_tools_pack.file-create-prepare":
        request = arguments
        container = None
    elif operation == "rumi_default_tools_pack.file-create":
        if set(arguments) != {"request", "plan"}:
            raise AuthorityDenied("reviewer file create execute shape unavailable")
        request = arguments["request"]
        container = arguments
    else:
        raise AuthorityDenied("reviewer file create operation unavailable")
    keys = {"workspace_id", "expected_mount_revision", "path", "content", "invocation_key"}
    if not isinstance(request, Mapping) or set(request) != keys:
        raise AuthorityDenied("reviewer file create request shape unavailable")
    presented = {key: value for key, value in request.items() if key != "invocation_key"}
    if container is None:
        return presented
    return {"request": presented, "plan": container["plan"]}
