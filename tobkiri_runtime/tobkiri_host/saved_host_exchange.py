"""Independent Host checks for a signed saved-turn guest exchange.

Authentication is supplied by the supervisor; authority and provider readiness
are supplied by a separately bound captured Broker callback, never by this codec.
"""

from __future__ import annotations

from typing import Any, Mapping

from tobkiri_protocol.canonical import canonical_json

from .continuation_chain import ChainIdentity, ContinuationChains, ResumePermit
from .continuation_envelope import (
    ValidatedContinuation, validate_continuation_request, validate_continuation_result,
)
from .saved_guest_dispatch import PROTOCOL, TARGETS


class SavedHostExchange:
    """Match each guest hop against Host-captured identity, plan and predecessor."""

    def __init__(
        self, identity: ChainIdentity, *, request_digest: str, artifact_identity: str,
        deadline_text: str, chains: ContinuationChains,
    ) -> None:
        self.identity = identity
        self._request_digest = request_digest
        self._artifact_identity = artifact_identity
        self._deadline_text = deadline_text
        self._chains = chains
        self._hop = 0
        self._previous: str | None = None
        self._permit: ResumePermit | None = None
        self._pending: ValidatedContinuation | None = None

    def accept(self, wrapper: Mapping[str, Any]) -> ValidatedContinuation:
        """Validate and retain before dispatching even the first owner read."""
        expected = self._binding("host-request")
        expected["deadline_monotonic"] = self._deadline_text
        if (
            self._pending is not None or self._hop >= len(TARGETS)
            or set(wrapper) != set(expected) | {"bridge_request", "bridge_request_digest"}
            or any(wrapper[key] != value for key, value in expected.items())
            or type(wrapper.get("version")) is not int
        ):
            raise ValueError("saved Host guest wrapper binding is invalid")
        frame = validate_continuation_request(
            canonical_json(wrapper["bridge_request"]), identity=self.identity,
            hop=self._hop, previous_digest=self._previous, target=TARGETS[self._hop],
        )
        if frame.digest != wrapper["bridge_request_digest"]:
            raise ValueError("saved Host guest frame digest is invalid")
        if self._permit is None:
            self._chains.start(self.identity, frame=frame.frame, nonce=frame.nonce)
        else:
            self._chains.advance(self._permit, frame=frame.frame, nonce=frame.nonce)
        self._pending = frame
        self._permit = None
        return frame

    def result(self, outcome: Mapping[str, Any]) -> dict[str, Any]:
        """Bind one checked callback result; no retries or implicit owner success."""
        frame = self._pending
        if frame is None:
            raise ValueError("saved Host has no pending action")
        value = {
            "kind": "tobkiri.packvm.continuation.result.v2", "version": 2,
            "request_digest": frame.digest, "outcome": dict(outcome),
        }
        checked = validate_continuation_result(canonical_json(value), request=frame)
        self._permit = self._chains.take(self.identity, nonce=frame.nonce, result=checked.frame)
        self._previous = checked.digest
        self._pending = None
        self._hop += 1
        return {**self._binding("host-result"), "bridge_request_digest": frame.digest,
                "bridge_result": value}

    def finish(self, outcome: Mapping[str, Any]) -> None:
        """Charge terminal bytes and retain the identity against replay."""
        if self._permit is None or self._pending is not None:
            raise ValueError("saved Host terminal result is not expected")
        if outcome.get("status") not in {"ok", "error"} or (
            outcome.get("status") == "ok" and self._hop != len(TARGETS)
        ):
            raise ValueError("saved Host success requires all four actions")
        self._chains.finish(self._permit, result=canonical_json(dict(outcome)))
        self._permit = None

    def cancel(self) -> None:
        """Fence this exchange; the supervisor owns process cancellation."""
        try:
            self._chains.cancel(self.identity)
        except ValueError:
            pass  # No registration or expired registration cannot authorize a hop.
        self._pending = None
        self._permit = None

    def _binding(self, kind: str) -> dict[str, Any]:
        return {
            "kind": f"tobkiri.packvm.bridge.{kind}.v2", "protocol": PROTOCOL, "version": 2,
            "request_id": self.identity.request_id, "target_domain": self.identity.domain_id,
            "binding_digest": self.identity.binding_digest,
            "guest_artifact_identity": self._artifact_identity,
            "request_digest": self._request_digest,
        }
