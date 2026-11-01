"""Bounded public explanations for Host saved-tool policy stops."""

from __future__ import annotations

import json
from typing import Any, Mapping

_REASONS = {
    "reviewer_unavailable": "承認用モデルを確認できないため、この操作を停止しました。",
    "native_denied": "承認されなかったため、この操作を停止しました。",
    "policy_revoked": "承認が取り消されたか期限が切れたため、この操作を停止しました。",
    "workspace_changed": "対象フォルダーの状態が変わったため、この操作を停止しました。",
    "unsupported_target": "この操作は選択した承認モードの対象外です。",
    "policy_unavailable": "承認ポリシーを確認できないため、この操作を停止しました。",
}


def saved_tool_policy_stop_result(error: BaseException) -> Mapping[str, Any]:
    """Return the existing tool error shape without exposing internal exception text."""
    authenticated_error_type: type[AuthenticatedPolicyReviewDenied] | None
    try:
        from tobkiri_host.policy_review_errors import AuthenticatedPolicyReviewDenied
    except ModuleNotFoundError as missing:
        if missing.name != "tobkiri_host.policy_review_errors":
            raise
        authenticated_error_type = None
    else:
        authenticated_error_type = AuthenticatedPolicyReviewDenied
    authenticated_error = None
    cursor: BaseException | None = error
    seen: set[int] = set()
    while cursor is not None and len(seen) < 8 and id(cursor) not in seen:
        seen.add(id(cursor))
        if authenticated_error_type is not None and isinstance(cursor, authenticated_error_type):
            authenticated_error = cursor
            break
        cursor = cursor.__cause__
    if authenticated_error is not None:
        # The exact kernel type is emitted only after native/transport/evidence
        # validation. Other exceptions cannot inject a public reviewer reason.
        reason = authenticated_error.safe_public_reason
        if not isinstance(reason, str) or not reason or len(reason) > 512:
            reason = "操作の安全性を確認できませんでした。"
        return {
            "result": json.dumps(
                {
                    "error": {
                        "code": "ACTION_APPROVAL_AGENT_REVIEW_DENIED",
                        "message": "承認用モデルが危険性を確認したため、操作を停止しました。"
                        + reason,
                    }
                },
                ensure_ascii=False,
            ),
            "is_error": True,
            "widget": None,
        }
    text = str(error).lower()
    if "reviewer" in text or "model" in text:
        reason = "reviewer_unavailable"
    elif "denied" in text or "stopped: denied" in text or "stopped: cancelled" in text:
        reason = "native_denied"
    elif any(word in text for word in ("revoked", "expired", "cancelled", "drained")):
        reason = "policy_revoked"
    elif "workspace" in text or "mount" in text:
        reason = "workspace_changed"
    elif "unsupported" in text or "outside" in text or "no exact native ceiling" in text:
        reason = "unsupported_target"
    else:
        reason = "policy_unavailable"
    return {
        "result": json.dumps(
            {"error": {"code": "ACTION_APPROVAL_" + reason.upper(), "message": _REASONS[reason]}},
            ensure_ascii=False,
        ),
        "is_error": True,
        "widget": None,
    }
