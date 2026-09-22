"""Production bridge for operating-profile finalization review gates."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from core_runtime.operating_profile import (
    AgentExecutionMode,
    AuthorityReviewConsumer,
    FinalizationAction,
    OperatingProfilePlanStore,
    ReviewGateDecision,
    ReviewGateContext,
    resolve_review_gate,
)
from core_runtime.operating_profile.provenance import stable_sha256
from domain.agent_runtime.run_store import AgentRunStore
from domain.tool_policy.internal_context import trusted_review_gate_context


REVIEW_CONSUMER_SERVICE = "operating_profile_review_consumer"


@dataclass(frozen=True)
class ReviewGateEnforcement:
    """Review decision made immediately before one finalization effect."""

    decision: ReviewGateDecision

    @property
    def blocked(self) -> bool:
        """Return whether the effect must stop."""

        return self.decision.blocked

    def to_dict(self) -> dict[str, Any]:
        """Return the audit-safe decision and retry request."""

        return self.decision.to_dict()


class _MissingAuthorityReviewConsumer:
    def consume_review(self, request: Any) -> None:
        del request
        return None


def enforce_finalization_review(
    action: FinalizationAction,
    artifact: Mapping[str, Any],
    context: dict[str, Any] | None,
    *,
    plan_store: OperatingProfilePlanStore | None = None,
    authority: AuthorityReviewConsumer | None = None,
    run_store: AgentRunStore | None = None,
) -> ReviewGateEnforcement | None:
    """Resolve and persist the configured review gate for an agent effect.

    The orchestration identity must carry the unforgeable marker installed by
    the server-side Agent or Chat runtime. Direct HTTP/tool callers cannot
    opt themselves into a mode or settle a review with request data.
    """

    trusted_mode = trusted_review_gate_context(context)
    if trusted_mode is None or not isinstance(context, dict):
        return None
    execution_mode_text, mode_context_id = trusted_mode
    try:
        execution_mode = AgentExecutionMode(execution_mode_text)
    except ValueError:
        return None

    profile_id = str(context.get("profile_id") or "").strip()
    if not profile_id:
        return None
    store = plan_store or OperatingProfilePlanStore()
    try:
        profile = store.load_active_profile(profile_id)
    except (OSError, TypeError, ValueError):
        profile = {
            "profile_id": profile_id,
            "review_topology": {"profile_load_failed": True},
        }
    if profile is None:
        return None

    run_id = str(context.get("agent_run_id") or context.get("run_id") or "").strip()
    conversation_id = str(
        context.get("conversation_id")
        or context.get("company_thread_id")
        or run_id
    ).strip()
    actor_principal_id = str(
        context.get("principal_id")
        or context.get("authority_principal_id")
        or f"profile:{profile_id}"
    ).strip()
    if not run_id or not conversation_id or not actor_principal_id:
        return None

    profile_payload = profile.to_dict() if hasattr(profile, "to_dict") else dict(profile)
    artifact_payload = dict(artifact)
    artifact_digest = stable_sha256(artifact_payload)
    artifact_revision = _artifact_revision(artifact_payload, artifact_digest)
    gate_context = ReviewGateContext(
        conversation_id=conversation_id,
        run_id=run_id,
        profile_id=profile_id,
        profile_version=stable_sha256(profile_payload),
        execution_mode=execution_mode,
        mode_context_id=mode_context_id,
        actor_principal_id=actor_principal_id,
        action=action,
        artifact_digest=artifact_digest,
        artifact_revision=artifact_revision,
    )
    decision = resolve_review_gate(
        profile,
        gate_context,
        authority or _authority_review_consumer(),
    )
    enforcement = ReviewGateEnforcement(decision)
    if decision.requires_review:
        _record_decision(run_store or AgentRunStore(), run_id, enforcement)
    return enforcement


def _authority_review_consumer() -> AuthorityReviewConsumer:
    try:
        from core_runtime.di_container import get_container

        candidate = get_container().get_or_none(REVIEW_CONSUMER_SERVICE)
    except Exception:
        candidate = None
    if callable(getattr(candidate, "consume_review", None)):
        return candidate
    return _MissingAuthorityReviewConsumer()


def _artifact_revision(artifact: Mapping[str, Any], digest: str) -> str:
    for key in (
        "expected_head",
        "expected_tree",
        "expected_status_hash",
        "revision",
        "version",
    ):
        value = str(artifact.get(key) or "").strip()
        if value:
            return value
    return digest


def _record_decision(
    store: AgentRunStore,
    run_id: str,
    enforcement: ReviewGateEnforcement,
) -> None:
    try:
        store.add_event(run_id, "review_gate_resolved", enforcement.to_dict())
    except Exception:
        # Review enforcement remains authoritative if optional run attachment
        # persistence is unavailable. The caller still receives the decision.
        return
