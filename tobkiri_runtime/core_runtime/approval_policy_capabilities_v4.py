"""Host-owned read-only presentation of actual policy capture and applicability."""

from __future__ import annotations

import re
import time
from typing import Any, Callable, Mapping

from tobkiri_protocol.canonical import canonical_digest

_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}\Z")
_MODES = frozenset({"ask", "agent", "full"})


class HostApprovalPolicyCapabilitiesV4:
    """Project current Host availability; this object never authorizes a tool."""

    def __init__(
        self,
        *,
        profile_id: str,
        activation_id: str,
        capture: Mapping[str, Any],
        assert_current_capture: Callable[[], None],
        workspace_binding: Callable[[str, str], Any] | None,
        policy_projection: Callable[[Any, str | None, str, Any], Mapping[str, Any]] | None,
        conversation_membership: Callable[[Any, str, str], bool] | None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._profile_id, self._activation_id = profile_id, activation_id
        self._capture = dict(capture)
        self._assert_capture = assert_current_capture
        self._workspace_binding = workspace_binding
        self._policy_projection = policy_projection
        self._conversation_membership = conversation_membership
        self._clock = clock

    def __call__(self, invocation: Any, applicability: Mapping[str, Any]) -> Mapping[str, Any]:
        """Bind a bounded, redacted projection to the actual current activation."""
        invocation.assert_current()
        self._assert_capture()
        if (
            set(applicability) != {"conversation_id", "workspace_id"}
            or not isinstance(applicability["workspace_id"], str)
            or _ID.fullmatch(applicability["workspace_id"]) is None
            or (
                applicability["conversation_id"] is not None
                and (
                    not isinstance(applicability["conversation_id"], str)
                    or _ID.fullmatch(applicability["conversation_id"]) is None
                )
            )
        ):
            raise ValueError("approval policy applicability is invalid")
        context = invocation.envelope.context
        if context.profile_id != self._profile_id or context.activation_id != self._activation_id:
            raise PermissionError("approval policy capture changed")
        conversation, workspace = applicability["conversation_id"], applicability["workspace_id"]
        if conversation is not None and (
            self._conversation_membership is None
            or self._conversation_membership(invocation, conversation, workspace) is not True
        ):
            raise PermissionError("approval policy conversation is unavailable")
        projection: Mapping[str, Any] = {
            "available_modes": ["ask"],
            "active_mode": "ask",
            "reason": "approval-policy-not-captured",
        }
        binding = None
        if self._workspace_binding is not None:
            try:
                binding = self._workspace_binding(self._profile_id, workspace)
            except (PermissionError, ValueError, FileNotFoundError):
                projection = {**projection, "reason": "workspace-unavailable"}
        if binding is not None and self._policy_projection is not None:
            projection = self._policy_projection(invocation, conversation, workspace, binding)
        if conversation is None:
            projection = {**projection, "active_mode": "ask"}
        if set(projection) != {"available_modes", "active_mode", "reason"}:
            raise PermissionError("approval policy projection is invalid")
        modes = projection["available_modes"]
        if (
            not isinstance(modes, (list, tuple))
            or not modes
            or len(modes) != len(set(modes))
            or "ask" not in modes
            or any(mode not in _MODES for mode in modes)
            or projection["active_mode"] not in _MODES
            or not isinstance(projection["reason"], str)
            or not 1 <= len(projection["reason"]) <= 256
        ):
            raise PermissionError("approval policy projection is invalid")
        self._assert_capture()
        invocation.assert_current()
        result = {
            "available_modes": list(modes),
            "active_mode": projection["active_mode"],
            "reason": projection["reason"],
            "profile_id": self._profile_id,
            "activation_id": self._activation_id,
            "workspace_id": workspace,
            "conversation_id": conversation,
            "expires_at": int(self._clock()) + 60,
        }
        result["capture_digest"] = canonical_digest(
            {"capture": self._capture, "projection": result}
        )
        return result
