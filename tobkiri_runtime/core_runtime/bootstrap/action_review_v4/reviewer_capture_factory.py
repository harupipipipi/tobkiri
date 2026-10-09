"""Prepare exact native reviewer facts from authorized settings and route reads."""

from __future__ import annotations
from dataclasses import dataclass
from typing import Any, Callable, Mapping
from core_runtime.authority.v4 import AuthorityDenied, authority_digest
from .canonical_reviewer_port import (
    CapturedReviewerBoundary,
    CanonicalReviewerGeneratePort,
    ReviewerGenerateLease,
)

SETTINGS_TARGET = ("tobkiri.resource.ui.settings.v1", "tobkiri_ui_settings_pack.settings-read")
QUOTE_TARGET = ("tobkiri.resource.ai.route.quote.v1", "rumi_ai_gateway_pack.ai-gateway.route-quote")


@dataclass(frozen=True)
class PreparedNativeReviewerCapture:
    """Native-visible facts plus Host-owned ports, never a guest authorization."""

    native_facts: Mapping[str, Any]
    boundary: CapturedReviewerBoundary
    reviewer_port: CanonicalReviewerGeneratePort
    assert_current: Callable[[], None]
    refresh: Callable[[], None]


class CanonicalReviewerCaptureFactory:
    """Resolve configured reviewer under exact independently signed read edges.

    Production supplies one actual V4DispatchSession with authenticated dedicated
    policy caller session, current signed generate edge capture and currentness
    guard. Settings values are selection hints only; the canonical read-only
    registry-backed quote supplies pinned executable model/provider/pricing.
    Native approval must display and bind native_facts before using reviewer_port.
    """

    def __init__(
        self,
        read_dispatch: Any,
        *,
        session_id: str,
        profile_id: str,
        caller_principal_id: str,
        independent_capture: Mapping[str, Any],
        bind_generate: Callable[[Mapping[str, Any], str, str], ReviewerGenerateLease],
        assert_current: Callable[[], None],
        authenticate_read: Callable[[tuple[str, str], Mapping[str, Any]], None],
        local_configuration_revision: Callable[[], tuple[str, int]],
        local_route_is_current: Callable[[Mapping[str, Any]], bool],
        evidence_validity_seconds: int | None = None,
    ) -> None:
        self._evidence_validity_seconds = evidence_validity_seconds
        self._dispatch = read_dispatch
        self._session = session_id
        self._profile = profile_id
        self._caller = caller_principal_id
        self._independent = dict(independent_capture)
        self._bind = bind_generate
        self._guard = assert_current
        self._authenticate_read = authenticate_read
        self._local_revision = local_configuration_revision
        self._local_route_current = local_route_is_current

    def _settings(self) -> tuple[str, int]:
        self._guard()
        if not self._session or not self._profile or not self._caller:
            raise AuthorityDenied("authenticated reviewer configuration read unavailable")
        result = self._dispatch.invoke(
            *SETTINGS_TARGET, {"profile_id": self._profile, "_session_id": self._session}
        )
        self._authenticate_read(SETTINGS_TARGET, result)
        values = result.get("values") if isinstance(result, Mapping) else None
        tools = values.get("tools") if isinstance(values, Mapping) else None
        model = tools.get("approval_reviewer_model") if isinstance(tools, Mapping) else None
        revision = result.get("document_revision") if isinstance(result, Mapping) else None
        if (
            not isinstance(model, str)
            or not model.strip()
            or type(revision) is not int
            or revision < 0
        ):
            raise AuthorityDenied(
                "configured reviewer model is missing; select one before agent mode"
            )
        return model.strip(), revision

    def capture(self) -> PreparedNativeReviewerCapture:
        """Return actual configured route facts for the native approval summary."""
        model, revision = self._settings()
        quote_payload = {
            "model_profile_id": model,
            "messages": [
                {"role": "user", "content": "Tobkiri independent action review route quote."}
            ],
            "requirements": {"request_surface": "approval-review", "tool_calling": False},
            "delivery_mode": "buffered",
        }
        quoted = self._dispatch.invoke(
            *QUOTE_TARGET, {**quote_payload, "_session_id": self._session}
        )
        self._authenticate_read(QUOTE_TARGET, quoted)
        route = quoted.get("route_binding") if isinstance(quoted, Mapping) else None
        if not isinstance(route, Mapping) or quoted.get("ready") is not True:
            raise AuthorityDenied("configured reviewer has no authenticated executable route")
        facts = {
            "profile_id": self._profile,
            "model_reference": model,
            "caller_principal_id": self._caller,
            "authority_capture_digest": authority_digest(self._independent),
            "route_binding": dict(route),
        }

        def current() -> None:
            # Kernel calls this during atomic Store proof checks. These private
            # callbacks must read local immutable owner revisions only: never
            # dispatch, reserve a Lease, write a file, or enter another Broker.
            self._guard()
            selected, current_revision = self._local_revision()
            if (
                selected != model
                or current_revision != revision
                or self._local_route_current(route) is not True
            ):
                raise AuthorityDenied("configured reviewer changed after native capture")

        def refresh() -> None:
            # Explicit formal I/O phase, called before entering any transaction.
            selected, current_revision = self._settings()
            fresh_quote = self._dispatch.invoke(
                *QUOTE_TARGET, {**quote_payload, "_session_id": self._session}
            )
            self._authenticate_read(QUOTE_TARGET, fresh_quote)
            if (
                selected != model
                or current_revision != revision
                or not isinstance(fresh_quote, Mapping)
                or fresh_quote.get("ready") is not True
                or dict(fresh_quote.get("route_binding") or {}) != dict(route)
            ):
                raise AuthorityDenied("configured reviewer changed on formal refresh")
            current()

        boundary = CapturedReviewerBoundary(facts, authority_digest(facts), current)
        boundary.snapshot()
        return PreparedNativeReviewerCapture(
            facts,
            boundary,
            CanonicalReviewerGeneratePort(
                boundary, self._bind, evidence_validity_seconds=self._evidence_validity_seconds
            ),
            current,
            refresh,
        )
