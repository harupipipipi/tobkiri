"""Narrow actual V4 generate adapter for an independently approved Host reviewer.

No settings lookup, global legacy gateway, parent operation Grant, or network
transport is implemented here. The production capture supplies one signed edge
and a separately authorized Host session using the real V4 dispatch API.
"""

from __future__ import annotations

import json
import math
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Protocol

from core_runtime.authority.v4 import AuthorityDenied, authority_digest

GENERATE_TARGET = ("tobkiri.service.ai.generate.v1", "rumi_ai_gateway_pack.ai-gateway.generate")
REVIEW_SURFACE = "approval-review"


class GenerateDispatch(Protocol):
    """The actual V4DispatchSession.invoke subset used by this port."""

    def invoke(
        self, contract_id: str, operation_id: str, payload: Mapping[str, Any], **kwargs: Any
    ) -> Mapping[str, Any]: ...


@dataclass(frozen=True)
class CapturedReviewerBoundary:
    """Native-approved facts; the production root checks authentication/currentness."""

    facts: Mapping[str, Any]
    boundary_digest: str
    assert_current: Callable[[], None]

    def snapshot(self) -> dict[str, Any]:
        """Verify and copy the exact approved model/provider route facts."""
        self.assert_current()
        captured = json.loads(json.dumps(dict(self.facts), allow_nan=False))
        required = {
            "profile_id",
            "model_reference",
            "caller_principal_id",
            "authority_capture_digest",
            "route_binding",
        }
        if set(captured) != required or authority_digest(captured) != self.boundary_digest:
            raise AuthorityDenied("reviewer native capture differs from approved boundary")
        if not all(
            isinstance(captured[k], str) and captured[k] for k in required - {"route_binding"}
        ):
            raise AuthorityDenied("reviewer capture has missing authenticated facts")
        route = captured["route_binding"]
        if not isinstance(route, dict) or set(route) != {
            "model_id",
            "provider_instance_id",
            "catalog_provider_instance_id",
            "catalog_revision",
            "pricing_revision",
            "pricing",
        }:
            raise AuthorityDenied("reviewer approved route is incomplete")
        if not all(
            isinstance(route[k], str) and route[k]
            for k in {"model_id", "provider_instance_id", "catalog_revision", "pricing_revision"}
        ):
            raise AuthorityDenied("reviewer approved route identity is missing")
        return captured


@dataclass(frozen=True)
class ReviewerGenerateLease:
    """Independent Host session pinned by the production Broker composition."""

    dispatch: GenerateDispatch
    session_id: str
    caller_principal_id: str
    boundary_digest: str
    payload_digest: str
    parent_deadline_monotonic: float
    parent_cancellation: threading.Event
    parent_cancellation_proof: Any
    assert_current: Callable[[], None]
    before_dispatch: Callable[[], None]
    execution_guard: Callable[[], None]
    committed_transport_proof: Callable[[], Mapping[str, Any]]
    release: Callable[[], None]


class CanonicalReviewerGeneratePort:
    """Invoke the canonical Host gateway under fresh independent authorization.

    `bind_generate` must prepare/admit a fresh request against the dedicated
    signed reviewer edge and return its exact request-bound lease. It cannot
    simply copy the saved operation's Grant or session. The root supplies that
    binder; this adapter validates the binding and invokes the real dispatch.
    """

    def __init__(
        self,
        boundary: CapturedReviewerBoundary,
        bind_generate: Callable[[Mapping[str, Any], str, str], ReviewerGenerateLease],
        *,
        clock: Callable[[], float] = time.time,
        evidence_validity_seconds: int | None = None,
    ) -> None:
        if evidence_validity_seconds is not None and (
            type(evidence_validity_seconds) is not int or not 1 <= evidence_validity_seconds <= 120
        ):
            raise ValueError("reviewer evidence validity must be bounded to 120 seconds")
        self._evidence_validity_seconds = evidence_validity_seconds
        self._clock = clock
        self._boundary = boundary
        self._bind = bind_generate
        self._proof_lock = threading.Lock()
        self._proofs: dict[str, Mapping[str, Any]] = {}

    def __call__(self, request: dict[str, Any]) -> dict[str, Any]:
        """Route exact no-tools request and retain only committed Host evidence."""
        facts = self._boundary.snapshot()
        if (
            request.get("model_reference") != facts["model_reference"]
            or request.get("tools") != []
            or not isinstance(request.get("messages"), list)
            or not isinstance(request.get("request_id"), str)
            or not request["request_id"]
        ):
            raise AuthorityDenied("reviewer request differs from captured model or tools")
        deadline = request.get("deadline")
        if (
            not isinstance(deadline, (int, float))
            or isinstance(deadline, bool)
            or not time.time() < deadline <= time.time() + 60
        ):
            raise AuthorityDenied("reviewer deadline is missing or elapsed")
        # PreparedInvocationSnapshot admits bounded canonical JSON, which has
        # integer numbers only. Round the epoch deadline down, never extend it.
        deadline = int(deadline)
        if deadline <= time.time():
            raise AuthorityDenied("reviewer canonical deadline elapsed")
        route = facts["route_binding"]
        payload = {
            "request_id": request["request_id"],
            "deadline": deadline,
            "profile_id": facts["profile_id"],
            "model_reference": facts["model_reference"],
            "model_profile_id": facts["model_reference"],
            "messages": request["messages"],
            "tools": [],
            "parameters": {"temperature": 0, "max_tokens": 220},
            "allow_failover": False,
            "route_binding": route,
            "requirements": {
                "request_surface": REVIEW_SURFACE,
                "tool_calling": False,
                "preferred_model_id": route["model_id"],
                "preferred_provider_instance_id": route["provider_instance_id"],
            },
        }
        # Immutable serialized request avoids provider mutation of the capture.
        payload = json.loads(json.dumps(payload, allow_nan=False))
        digest = authority_digest(payload)
        # Establish a finite evidence ceiling BEFORE any dispatch. Generation
        # wait/deadline stays independent and is never extended.
        evidence_ceiling = (
            self._clock() + self._evidence_validity_seconds
            if self._evidence_validity_seconds is not None
            else deadline
        )
        lease = self._bind(payload, self._boundary.boundary_digest, digest)
        if not isinstance(lease, ReviewerGenerateLease):
            raise AuthorityDenied("independent reviewer Host lease unavailable")
        try:
            self._boundary.snapshot()
            lease.assert_current()
            if (
                lease.boundary_digest != self._boundary.boundary_digest
                or lease.payload_digest != digest
                or lease.caller_principal_id != facts["caller_principal_id"]
                or not lease.session_id.startswith("session.approval-review.")
                or type(lease.parent_cancellation) is not threading.Event
                or lease.parent_cancellation.is_set()
                or lease.parent_deadline_monotonic <= time.monotonic()
            ):
                raise AuthorityDenied("reviewer lease binding or cancellation invalid")
            result = lease.dispatch.invoke(
                *GENERATE_TARGET,
                {**payload, "_session_id": lease.session_id},
                parent_deadline_monotonic=lease.parent_deadline_monotonic,
                parent_cancellation=lease.parent_cancellation,
                parent_cancellation_proof=lease.parent_cancellation_proof,
                before_dispatch=lease.before_dispatch,
                execution_guard=lease.execution_guard,
            )
            lease.assert_current()
            self._boundary.snapshot()
            if lease.parent_cancellation.is_set() or time.time() >= deadline:
                raise AuthorityDenied("reviewer cancelled or expired after transport")
            if (
                not isinstance(result, Mapping)
                or result.get("status") != "ok"
                or result.get("request_id") != payload["request_id"]
                or result.get("tool_intents")
                or any(
                    result.get(k) != route[k]
                    for k in (
                        "model_id",
                        "provider_instance_id",
                        "catalog_provider_instance_id",
                        "catalog_revision",
                        "pricing_revision",
                    )
                )
            ):
                raise AuthorityDenied("reviewer returned rebound route or unusable result")
            proof = dict(lease.committed_transport_proof())
            if (
                proof.get("state") != "committed"
                or proof.get("payload_digest") != digest
                or not isinstance(proof.get("request_digest"), str)
                or not proof.get("request_digest")
                or not isinstance(proof.get("lease_id"), str)
                or not proof.get("lease_id")
                or proof.get("boundary_digest") != self._boundary.boundary_digest
                or proof.get("session_id") != lease.session_id
            ):
                raise AuthorityDenied("reviewer transport has no exact committed Host proof")
            if self._evidence_validity_seconds is not None and not isinstance(
                proof.get("expires_at"), (int, float)
            ):
                raise AuthorityDenied("reviewer native evidence expiry missing")
            if any(
                type(proof[key]) not in (int, float) or not math.isfinite(proof[key])
                for key in ("expires_at", "root_expires_at")
                if key in proof
            ):
                raise AuthorityDenied("reviewer evidence expiry is invalid")
            expires_at = min(
                evidence_ceiling,
                proof.get("expires_at", deadline),
                proof.get("root_expires_at", evidence_ceiling),
            )
            if expires_at <= self._clock():
                raise AuthorityDenied("reviewer committed evidence already expired")
            with self._proof_lock:
                # Expired evidence never prevents subsequent authorized reviews.
                now = self._clock()
                self._proofs = {k: v for k, v in self._proofs.items() if v["expires_at"] > now}
                # Capacity is bounded to unfinished evidence handed to root.
                if len(self._proofs) >= 64:
                    raise AuthorityDenied("reviewer transport evidence capacity exhausted")
                self._proofs[payload["request_id"]] = {
                    "reviewer_boundary_digest": self._boundary.boundary_digest,
                    "payload_digest": digest,
                    "transport_request_digest": proof["request_digest"],
                    "transport_proof": proof,
                    "model_id": route["model_id"],
                    "provider_instance_id": route["provider_instance_id"],
                    "catalog_revision": route["catalog_revision"],
                    "expires_at": expires_at,
                }
            return dict(result)
        finally:
            lease.release()

    def consume_transport_evidence(self, request_id: str) -> Mapping[str, Any]:
        """Consume exact committed reviewer provenance once, for root evidence."""
        self._boundary.snapshot()
        with self._proof_lock:
            evidence = self._proofs.pop(request_id, None)
        if evidence is None or evidence["expires_at"] <= self._clock():
            raise AuthorityDenied("reviewer evidence missing, expired, or already consumed")
        return evidence
