from __future__ import annotations

import hashlib
import unittest
from dataclasses import replace
from typing import Callable, cast

from core_runtime.operating_profile import (
    AgentExecutionMode,
    AuthorityReviewResult,
    FinalizationAction,
    ReviewGateContext,
    ReviewGatePolicy,
    ReviewGateRequest,
    ReviewVerdict,
    attach_review_gate_decision,
    compile_operating_profile,
    resolve_review_gate,
)


class RecordingAuthority:
    def __init__(
        self,
        result_factory: Callable[[ReviewGateRequest], AuthorityReviewResult | None],
    ) -> None:
        self.result_factory = result_factory
        self.requests: list[ReviewGateRequest] = []

    def consume_review(
        self,
        request: ReviewGateRequest,
    ) -> AuthorityReviewResult | None:
        self.requests.append(request)
        return self.result_factory(request)


def _profile(
    *,
    mode: str = "blocking",
    reviewer_profile: str = "reviewer_agent",
    applies_to: list[str] | None = None,
    require_separate_run: bool = True,
):
    return compile_operating_profile(
        {
            "profile_id": "coding_agent",
            "preset": "balanced_local",
            "review_topology": {
                "mode": mode,
                "reviewer_profile": reviewer_profile,
                "require_separate_run": require_separate_run,
                "applies_to": applies_to or ["commit", "push", "merge"],
                "store_findings": True,
            },
        }
    )


def _context(
    execution_mode: AgentExecutionMode = AgentExecutionMode.MODE_AGENT,
    *,
    action: FinalizationAction = FinalizationAction.PUSH,
    artifact: bytes = b"revision-one",
    artifact_revision: str = "revision-1",
) -> ReviewGateContext:
    return ReviewGateContext(
        conversation_id="conversation-1",
        run_id="run-actor-1",
        profile_id="coding_agent",
        profile_version="rumi.operating_profile.v1:42",
        execution_mode=execution_mode,
        mode_context_id=f"{execution_mode.value}:context-1",
        actor_principal_id="profile:coding_agent:run-actor-1",
        action=action,
        artifact_digest=hashlib.sha256(artifact).hexdigest(),
        artifact_revision=artifact_revision,
    )


def _result(
    request: ReviewGateRequest,
    *,
    verdict: ReviewVerdict = ReviewVerdict.APPROVED,
    binding_digest: str | None = None,
    reviewer_profile: str = "reviewer_agent",
    reviewer_principal_id: str = "profile:reviewer_agent:reviewer-1",
    reviewer_run_id: str = "run-reviewer-1",
) -> AuthorityReviewResult:
    return AuthorityReviewResult(
        authority_record_id="authority-review-record-1",
        binding_digest=binding_digest or request.binding_digest,
        review_id="review-1",
        reviewer_profile=reviewer_profile,
        reviewer_principal_id=reviewer_principal_id,
        reviewer_run_id=reviewer_run_id,
        reviewer_model="local/reviewer-model",
        verdict=verdict,
        findings=("missing rollback assertion",),
        missing_tests=("installer smoke",),
        security_concerns=("confirm one-shot consumption",),
        residual_risk="installer queue is still pending",
        reviewed_artifacts=(request.context.artifact_digest,),
    )


class TestOperatingProfileReviewGate(unittest.TestCase):
    def test_mode_fusion_and_team_use_the_same_blocking_resolver(self) -> None:
        profile = _profile()

        for execution_mode in AgentExecutionMode:
            with self.subTest(execution_mode=execution_mode.value):
                authority = RecordingAuthority(lambda _request: None)
                decision = resolve_review_gate(
                    profile,
                    _context(execution_mode),
                    authority,
                )

                self.assertTrue(decision.blocked)
                self.assertFalse(decision.review_satisfied)
                self.assertTrue(decision.requires_review)
                self.assertEqual(decision.reason, "authority_review_missing")
                self.assertEqual(len(authority.requests), 1)
                self.assertEqual(
                    authority.requests[0].context.execution_mode,
                    execution_mode,
                )

    def test_approved_authority_result_allows_and_attaches_review_evidence(self) -> None:
        authority = RecordingAuthority(_result)
        decision = resolve_review_gate(_profile(), _context(), authority)

        self.assertFalse(decision.blocked)
        self.assertTrue(decision.review_satisfied)
        self.assertEqual(decision.reason, "review_approved")
        run = {"run_id": "run-actor-1", "status": "finalizing"}
        attached = attach_review_gate_decision(run, decision)
        review = attached["review_gate_results"][0]["review"]

        self.assertNotIn("review_gate_results", run)
        self.assertEqual(review["verdict"], "approved")
        self.assertEqual(review["findings"], ["missing rollback assertion"])
        self.assertEqual(review["missing_tests"], ["installer smoke"])
        self.assertEqual(
            review["security_concerns"],
            ["confirm one-shot consumption"],
        )
        self.assertEqual(
            review["residual_risk"],
            "installer queue is still pending",
        )
        self.assertEqual(review["reviewer_profile"], "reviewer_agent")
        self.assertEqual(review["reviewer_model"], "local/reviewer-model")

    def test_non_approved_verdict_blocks_or_warns_by_profile_policy(self) -> None:
        for policy_mode, expected_blocked in (
            ("blocking", True),
            ("warning", False),
        ):
            with self.subTest(policy_mode=policy_mode):
                authority = RecordingAuthority(
                    lambda request: _result(
                        request,
                        verdict=ReviewVerdict.CHANGES_REQUESTED,
                    )
                )
                decision = resolve_review_gate(
                    _profile(mode=policy_mode),
                    _context(),
                    authority,
                )

                self.assertEqual(decision.blocked, expected_blocked)
                self.assertFalse(decision.review_satisfied)
                self.assertEqual(decision.reason, "review_changes_requested")

    def test_warning_mode_allows_missing_authority_result_with_diagnostic(self) -> None:
        authority = RecordingAuthority(lambda _request: None)
        decision = resolve_review_gate(
            _profile(mode="warning"),
            _context(),
            authority,
        )

        self.assertFalse(decision.blocked)
        self.assertFalse(decision.review_satisfied)
        self.assertEqual(decision.reason, "authority_review_missing")
        self.assertIsNone(decision.result)

    def test_off_or_unlisted_action_does_not_consume_authority_review(self) -> None:
        for profile, context in (
            (_profile(mode="off"), _context()),
            (
                compile_operating_profile(
                    {
                        "profile_id": "coding_agent",
                        "preset": "balanced_local",
                    }
                ),
                _context(),
            ),
            (
                _profile(applies_to=["merge"]),
                _context(action=FinalizationAction.PUSH),
            ),
        ):
            authority = RecordingAuthority(
                lambda _request: self.fail("review must not be consumed")
            )
            decision = resolve_review_gate(profile, context, authority)

            self.assertFalse(decision.blocked)
            self.assertTrue(decision.review_satisfied)
            self.assertFalse(decision.requires_review)
            self.assertEqual(decision.reason, "review_not_required")
            self.assertEqual(authority.requests, [])

    def test_stale_artifact_binding_and_self_review_fail_closed(self) -> None:
        cases = {
            "stale_binding": lambda request: _result(
                request,
                binding_digest="0" * 64,
            ),
            "same_profile": lambda request: _result(
                request,
                reviewer_profile="coding_agent",
            ),
            "same_principal": lambda request: _result(
                request,
                reviewer_principal_id=request.context.actor_principal_id,
            ),
            "same_run": lambda request: _result(
                request,
                reviewer_run_id=request.context.run_id,
            ),
            "missing_artifact": lambda request: replace(
                _result(request),
                reviewed_artifacts=(),
            ),
        }
        for name, factory in cases.items():
            with self.subTest(case=name):
                decision = resolve_review_gate(
                    _profile(),
                    _context(),
                    RecordingAuthority(factory),
                )

                self.assertTrue(decision.blocked)
                self.assertFalse(decision.review_satisfied)
                self.assertEqual(
                    decision.reason,
                    "authority_review_binding_mismatch",
                )
                self.assertTrue(decision.diagnostics)

    def test_binding_changes_with_artifact_revision_action_and_mode(self) -> None:
        requests: list[ReviewGateRequest] = []

        def record(request: ReviewGateRequest) -> None:
            requests.append(request)
            return None

        authority = RecordingAuthority(record)
        contexts = (
            _context(),
            _context(artifact=b"revision-two"),
            _context(artifact_revision="revision-2"),
            _context(action=FinalizationAction.COMMIT),
            _context(AgentExecutionMode.TEAM_AGENT),
        )
        for context in contexts:
            resolve_review_gate(_profile(), context, authority)

        self.assertEqual(
            len({request.binding_digest for request in requests}),
            len(contexts),
        )

    def test_client_shaped_result_and_invalid_policy_cannot_unlock_gate(self) -> None:
        forged = RecordingAuthority(
            cast(
                Callable[
                    [ReviewGateRequest],
                    AuthorityReviewResult | None,
                ],
                lambda _request: {
                    "approved": True,
                    "approved_by": "caller",
                },
            )
        )
        forged_decision = resolve_review_gate(_profile(), _context(), forged)

        raw = _profile().to_dict()
        raw["review_policy"] = {
            "mode": "blocking",
            "reviewer_profile": "reviewer_agent",
            "applies_to": ["push"],
            "approved": True,
        }
        raw["review_topology"] = raw.pop("review_policy")
        invalid_decision = resolve_review_gate(raw, _context(), forged)

        self.assertTrue(forged_decision.blocked)
        self.assertFalse(forged_decision.review_satisfied)
        self.assertEqual(forged_decision.reason, "authority_review_missing")
        self.assertTrue(invalid_decision.blocked)
        self.assertFalse(invalid_decision.review_satisfied)
        self.assertEqual(invalid_decision.reason, "review_policy_invalid")
        self.assertIn("unknown fields: approved", invalid_decision.diagnostics[0])

    def test_profile_binding_mismatch_and_authority_errors_fail_closed(self) -> None:
        context = replace(_context(), profile_id="other_profile")
        profile_mismatch = resolve_review_gate(
            _profile(),
            context,
            RecordingAuthority(lambda _request: None),
        )

        class FailingAuthority:
            @staticmethod
            def consume_review(_request: ReviewGateRequest) -> None:
                raise RuntimeError("secret internal path")

        unavailable = resolve_review_gate(
            _profile(),
            _context(),
            FailingAuthority(),
        )

        self.assertTrue(profile_mismatch.blocked)
        self.assertEqual(
            profile_mismatch.reason,
            "review_profile_binding_mismatch",
        )
        self.assertTrue(unavailable.blocked)
        self.assertEqual(unavailable.reason, "authority_review_unavailable")
        self.assertEqual(unavailable.diagnostics, ("authority_consumer_error",))
        self.assertNotIn("secret", unavailable.to_dict()["diagnostics"][0])

    def test_policy_normalization_accepts_git_aliases_and_rejects_strings(self) -> None:
        policy = ReviewGatePolicy.from_mapping(
            {
                "mode": "blocking",
                "reviewer_profile": "reviewer_agent",
                "require_separate_run": True,
                "applies_to": ["git_push", "commit", "git_merge"],
                "store_findings": True,
            }
        )

        self.assertEqual(
            [action.value for action in policy.applies_to],
            ["commit", "merge", "push"],
        )
        with self.assertRaisesRegex(ValueError, "applies_to must be an array"):
            ReviewGatePolicy.from_mapping(
                {
                    "mode": "blocking",
                    "reviewer_profile": "reviewer_agent",
                    "applies_to": "push",
                }
            )

    def test_context_requires_canonical_digest_and_typed_mode(self) -> None:
        with self.assertRaisesRegex(ValueError, "SHA-256"):
            replace(_context(), artifact_digest="not-a-digest")
        with self.assertRaisesRegex(ValueError, "AgentExecutionMode"):
            replace(_context(), execution_mode="team_agent")  # type: ignore[arg-type]


if __name__ == "__main__":
    unittest.main()
