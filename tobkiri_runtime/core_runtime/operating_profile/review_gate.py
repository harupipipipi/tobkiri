"""Fail-closed review gates for operating-profile finalization actions."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping, Protocol

from .models import OperatingProfile
from .provenance import stable_sha256


REVIEW_GATE_SPEC_VERSION = "tobkiri.operating_profile.review_gate.v1"
REVIEW_RESULT_SPEC_VERSION = "tobkiri.operating_profile.review_result.v1"


class ReviewGateMode(str, Enum):
    """Enforcement level declared by an operating profile."""

    OFF = "off"
    WARNING = "warning"
    BLOCKING = "blocking"


class AgentExecutionMode(str, Enum):
    """Agent orchestration modes that share the same review resolver."""

    MODE_AGENT = "mode_agent"
    FUSION_AGENT = "fusion_agent"
    TEAM_AGENT = "team_agent"


class FinalizationAction(str, Enum):
    """Finalization actions that may be guarded by a profile review."""

    MERGE = "merge"
    COMMIT = "commit"
    PUSH = "push"
    PUBLISH = "publish"
    DELIVERY = "delivery"


class ReviewVerdict(str, Enum):
    """Verdicts emitted by an Authority-owned reviewer run."""

    APPROVED = "approved"
    CHANGES_REQUESTED = "changes_requested"
    BLOCKED = "blocked"
    INCONCLUSIVE = "inconclusive"


_ACTION_ALIASES: dict[str, FinalizationAction] = {
    "git_merge": FinalizationAction.MERGE,
    "git_commit": FinalizationAction.COMMIT,
    "git_push": FinalizationAction.PUSH,
    "external_send": FinalizationAction.DELIVERY,
}
_POLICY_FIELDS = frozenset(
    {
        "mode",
        "reviewer_profile",
        "require_separate_run",
        "applies_to",
        "store_findings",
    }
)
_LEGACY_LOCAL_REVIEW_FIELDS = frozenset({"mode", "required_for", "reviewers"})


@dataclass(frozen=True)
class ReviewGatePolicy:
    """Normalized server-owned profile policy for review enforcement."""

    mode: ReviewGateMode = ReviewGateMode.OFF
    reviewer_profile: str | None = None
    require_separate_run: bool = True
    applies_to: tuple[FinalizationAction, ...] = ()
    store_findings: bool = True

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any] | None) -> "ReviewGatePolicy":
        """Normalize a stored policy and reject unknown or ambiguous fields."""

        data = dict(raw or {})
        if not data:
            return cls()
        if data.get("mode") == "local_review" and set(data) <= _LEGACY_LOCAL_REVIEW_FIELDS:
            # Existing local-user approval topology remains owned by the
            # normal ActionPolicy/Authority path. It is not an agent review.
            return cls()
        unknown = sorted(set(data) - _POLICY_FIELDS)
        if unknown:
            raise ValueError("review policy contains unknown fields: " + ", ".join(unknown))
        mode = _enum_value(ReviewGateMode, data.get("mode") or "off", "mode")
        reviewer_profile = _optional_identifier(data.get("reviewer_profile"))
        require_separate_run = _strict_bool(
            data.get("require_separate_run", True),
            "require_separate_run",
        )
        store_findings = _strict_bool(
            data.get("store_findings", True),
            "store_findings",
        )
        applies_to = _normalize_actions(data.get("applies_to"))
        if mode is not ReviewGateMode.OFF:
            if not reviewer_profile:
                raise ValueError("reviewer_profile is required when review mode is enabled")
            if not applies_to:
                raise ValueError("applies_to is required when review mode is enabled")
        return cls(
            mode=mode,
            reviewer_profile=reviewer_profile,
            require_separate_run=require_separate_run,
            applies_to=applies_to,
            store_findings=store_findings,
        )

    def to_dict(self) -> dict[str, Any]:
        """Return the canonical policy representation used for binding."""

        return {
            "mode": self.mode.value,
            "reviewer_profile": self.reviewer_profile,
            "require_separate_run": self.require_separate_run,
            "applies_to": [action.value for action in self.applies_to],
            "store_findings": self.store_findings,
        }


@dataclass(frozen=True)
class ReviewGateContext:
    """Server-owned revision context bound to one finalization attempt."""

    conversation_id: str
    run_id: str
    profile_id: str
    profile_version: str
    execution_mode: AgentExecutionMode
    mode_context_id: str
    actor_principal_id: str
    action: FinalizationAction
    artifact_digest: str
    artifact_revision: str

    def __post_init__(self) -> None:
        for name in (
            "conversation_id",
            "run_id",
            "profile_id",
            "profile_version",
            "mode_context_id",
            "actor_principal_id",
            "artifact_revision",
        ):
            if not str(getattr(self, name) or "").strip():
                raise ValueError(f"{name} must not be empty")
        if not isinstance(self.execution_mode, AgentExecutionMode):
            raise ValueError("execution_mode must be an AgentExecutionMode")
        if not isinstance(self.action, FinalizationAction):
            raise ValueError("action must be a FinalizationAction")
        normalized_digest = str(self.artifact_digest or "").strip().lower()
        if len(normalized_digest) != 64 or any(
            character not in "0123456789abcdef" for character in normalized_digest
        ):
            raise ValueError("artifact_digest must be a SHA-256 hex digest")
        object.__setattr__(self, "artifact_digest", normalized_digest)

    def to_dict(self) -> dict[str, str]:
        """Return the canonical Authority binding fields."""

        return {
            "conversation_id": self.conversation_id,
            "run_id": self.run_id,
            "profile_id": self.profile_id,
            "profile_version": self.profile_version,
            "execution_mode": self.execution_mode.value,
            "mode_context_id": self.mode_context_id,
            "actor_principal_id": self.actor_principal_id,
            "action": self.action.value,
            "artifact_digest": self.artifact_digest,
            "artifact_revision": self.artifact_revision,
        }


@dataclass(frozen=True)
class ReviewGateRequest:
    """Exact request an Authority adapter must settle once."""

    binding_digest: str
    context: ReviewGateContext
    policy: ReviewGatePolicy

    def to_dict(self) -> dict[str, Any]:
        """Return the request envelope used by Authority and audit adapters."""

        return {
            "schema": REVIEW_GATE_SPEC_VERSION,
            "binding_digest": self.binding_digest,
            "context": self.context.to_dict(),
            "policy": self.policy.to_dict(),
        }


@dataclass(frozen=True)
class AuthorityReviewResult:
    """Review result returned only after Host Authority one-shot consumption."""

    authority_record_id: str
    binding_digest: str
    review_id: str
    reviewer_profile: str
    reviewer_principal_id: str
    reviewer_run_id: str
    reviewer_model: str
    verdict: ReviewVerdict
    findings: tuple[str, ...] = ()
    missing_tests: tuple[str, ...] = ()
    security_concerns: tuple[str, ...] = ()
    residual_risk: str = ""
    reviewed_artifacts: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.verdict, ReviewVerdict):
            raise ValueError("verdict must be a ReviewVerdict")
        if not _is_sha256(self.binding_digest):
            raise ValueError("binding_digest must be a SHA-256 hex digest")
        for name in (
            "authority_record_id",
            "review_id",
            "reviewer_profile",
            "reviewer_principal_id",
            "reviewer_run_id",
            "reviewer_model",
        ):
            if not str(getattr(self, name) or "").strip():
                raise ValueError(f"{name} must not be empty")

    def to_dict(self, *, include_findings: bool = True) -> dict[str, Any]:
        """Return the review attachment without hidden reasoning."""

        output: dict[str, Any] = {
            "schema": REVIEW_RESULT_SPEC_VERSION,
            "authority_record_id": self.authority_record_id,
            "binding_digest": self.binding_digest,
            "review_id": self.review_id,
            "reviewer_profile": self.reviewer_profile,
            "reviewer_principal_id": self.reviewer_principal_id,
            "reviewer_run_id": self.reviewer_run_id,
            "reviewer_model": self.reviewer_model,
            "verdict": self.verdict.value,
            "residual_risk": self.residual_risk,
            "reviewed_artifacts": list(self.reviewed_artifacts),
        }
        if include_findings:
            output.update(
                {
                    "findings": list(self.findings),
                    "missing_tests": list(self.missing_tests),
                    "security_concerns": list(self.security_concerns),
                }
            )
        return output


class AuthorityReviewConsumer(Protocol):
    """Host adapter that verifies and consumes a review settlement once."""

    def consume_review(
        self,
        request: ReviewGateRequest,
    ) -> AuthorityReviewResult | None:
        """Consume a result bound to the exact request, or return no result."""


@dataclass(frozen=True)
class ReviewGateDecision:
    """Additional review condition and audit-safe result attachment.

    This decision never grants the underlying action. Callers must still pass
    the normal ActionPolicy, capability, approval, lease, and Authority checks.
    """

    blocked: bool
    review_satisfied: bool
    requires_review: bool
    reason: str
    request: ReviewGateRequest
    result: AuthorityReviewResult | None = None
    diagnostics: tuple[str, ...] = field(default_factory=tuple)

    def to_dict(self) -> dict[str, Any]:
        """Return a stable decision suitable for a run attachment."""

        return {
            "schema": REVIEW_GATE_SPEC_VERSION,
            "blocked": self.blocked,
            "review_satisfied": self.review_satisfied,
            "requires_review": self.requires_review,
            "reason": self.reason,
            "request": self.request.to_dict(),
            "review": (
                self.result.to_dict(include_findings=self.request.policy.store_findings)
                if self.result is not None
                else None
            ),
            "diagnostics": list(self.diagnostics),
        }


def resolve_review_gate(
    profile: OperatingProfile | Mapping[str, Any],
    context: ReviewGateContext,
    authority: AuthorityReviewConsumer,
) -> ReviewGateDecision:
    """Resolve one finalization gate using only a Host-owned review consumer.

    Client booleans, reviewer names, and review payloads are deliberately not
    accepted. The consumer must verify and one-shot-consume a result bound to
    the request immediately before the finalization effect.
    """

    operating_profile = _coerce_profile(profile)
    if operating_profile.profile_id != context.profile_id:
        policy = ReviewGatePolicy(
            mode=ReviewGateMode.BLOCKING,
            applies_to=tuple(FinalizationAction),
        )
        return ReviewGateDecision(
            blocked=True,
            review_satisfied=False,
            requires_review=True,
            reason="review_profile_binding_mismatch",
            request=_build_request(context, policy),
        )
    try:
        policy = ReviewGatePolicy.from_mapping(operating_profile.review_topology)
    except (TypeError, ValueError) as exc:
        policy = ReviewGatePolicy(
            mode=ReviewGateMode.BLOCKING,
            applies_to=tuple(FinalizationAction),
        )
        request = _build_request(context, policy)
        return ReviewGateDecision(
            blocked=True,
            review_satisfied=False,
            requires_review=True,
            reason="review_policy_invalid",
            request=request,
            diagnostics=(str(exc),),
        )

    request = _build_request(context, policy)
    if policy.mode is ReviewGateMode.OFF or context.action not in policy.applies_to:
        return ReviewGateDecision(
            blocked=False,
            review_satisfied=True,
            requires_review=False,
            reason="review_not_required",
            request=request,
        )
    if policy.reviewer_profile == context.profile_id:
        return ReviewGateDecision(
            blocked=True,
            review_satisfied=False,
            requires_review=True,
            reason="reviewer_profile_must_be_separate",
            request=request,
        )

    result: AuthorityReviewResult | None
    try:
        result = authority.consume_review(request)
    except Exception:
        return _missing_decision(
            request,
            "authority_review_unavailable",
            diagnostics=("authority_consumer_error",),
        )
    if not isinstance(result, AuthorityReviewResult):
        return _missing_decision(request, "authority_review_missing")

    mismatch = _result_mismatch(request, result)
    if mismatch:
        return _missing_decision(
            request,
            "authority_review_binding_mismatch",
            result=result,
            diagnostics=(mismatch,),
        )
    if result.verdict is ReviewVerdict.APPROVED:
        return ReviewGateDecision(
            blocked=False,
            review_satisfied=True,
            requires_review=True,
            reason="review_approved",
            request=request,
            result=result,
        )
    return ReviewGateDecision(
        blocked=policy.mode is ReviewGateMode.BLOCKING,
        review_satisfied=False,
        requires_review=True,
        reason=f"review_{result.verdict.value}",
        request=request,
        result=result,
    )


def attach_review_gate_decision(
    run: Mapping[str, Any],
    decision: ReviewGateDecision,
) -> dict[str, Any]:
    """Attach a review decision to a run without mutating caller data."""

    output = dict(run)
    existing = output.get("review_gate_results")
    attachments = list(existing) if isinstance(existing, list) else []
    attachments.append(decision.to_dict())
    output["review_gate_results"] = attachments
    return output


def _build_request(
    context: ReviewGateContext,
    policy: ReviewGatePolicy,
) -> ReviewGateRequest:
    payload = {
        "schema": REVIEW_GATE_SPEC_VERSION,
        "context": context.to_dict(),
        "policy": policy.to_dict(),
    }
    return ReviewGateRequest(
        binding_digest=stable_sha256(payload),
        context=context,
        policy=policy,
    )


def _missing_decision(
    request: ReviewGateRequest,
    reason: str,
    *,
    result: AuthorityReviewResult | None = None,
    diagnostics: tuple[str, ...] = (),
) -> ReviewGateDecision:
    return ReviewGateDecision(
        blocked=request.policy.mode is ReviewGateMode.BLOCKING,
        review_satisfied=False,
        requires_review=True,
        reason=reason,
        request=request,
        result=result,
        diagnostics=diagnostics,
    )


def _result_mismatch(
    request: ReviewGateRequest,
    result: AuthorityReviewResult,
) -> str:
    if result.binding_digest != request.binding_digest:
        return "binding_digest"
    if result.reviewer_profile != request.policy.reviewer_profile:
        return "reviewer_profile"
    if result.reviewer_profile == request.context.profile_id:
        return "self_review_profile"
    if result.reviewer_principal_id == request.context.actor_principal_id:
        return "self_review_principal"
    if request.policy.require_separate_run and result.reviewer_run_id == request.context.run_id:
        return "reviewer_run_id"
    if not result.authority_record_id:
        return "authority_record_id"
    if not result.review_id:
        return "review_id"
    if not result.reviewer_model:
        return "reviewer_model"
    if request.context.artifact_digest not in result.reviewed_artifacts:
        return "reviewed_artifacts"
    return ""


def _coerce_profile(
    profile: OperatingProfile | Mapping[str, Any],
) -> OperatingProfile:
    if isinstance(profile, OperatingProfile):
        return profile
    if isinstance(profile, Mapping):
        return OperatingProfile.from_dict(profile)
    raise TypeError("profile must be an OperatingProfile or server-owned mapping")


def _normalize_actions(value: Any) -> tuple[FinalizationAction, ...]:
    if value is None:
        return ()
    if not isinstance(value, (list, tuple)):
        raise ValueError("applies_to must be an array")
    actions: set[FinalizationAction] = set()
    for item in value:
        candidate = str(item or "").strip().lower()
        action = _ACTION_ALIASES.get(candidate)
        if action is None:
            action = _enum_value(
                FinalizationAction,
                candidate,
                "applies_to",
            )
        actions.add(action)
    return tuple(sorted(actions, key=lambda action: action.value))


def _optional_identifier(value: Any) -> str | None:
    candidate = str(value or "").strip()
    if not candidate:
        return None
    if len(candidate) > 128:
        raise ValueError("reviewer_profile is too long")
    allowed = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._:-"
    if any(character not in allowed for character in candidate):
        raise ValueError("reviewer_profile contains invalid characters")
    return candidate


def _is_sha256(value: Any) -> bool:
    candidate = str(value or "").strip().lower()
    return len(candidate) == 64 and all(character in "0123456789abcdef" for character in candidate)


def _strict_bool(value: Any, field_name: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"{field_name} must be a boolean")
    return value


def _enum_value(
    enum_type: type[Enum],
    value: Any,
    field_name: str,
) -> Any:
    try:
        return enum_type(str(value or "").strip().lower())
    except ValueError as exc:
        raise ValueError(f"invalid review policy {field_name}: {value!r}") from exc
